"""QQ 邮箱 IMAP 邮件抓取模块。

通过 IMAP 连接 QQ 邮箱，根据配置的过滤规则筛选电费相关邮件，
下载附件到本地临时目录。

每封邮件按独立子目录存放（以日期+主题命名），
通过 processed_emails 表记录指纹防止重复下载。
"""

import email
import email.header
import hashlib
import imaplib
import os
import re
import socket
import sqlite3
import time
import zipfile
from datetime import datetime
from email.message import Message
from pathlib import Path
from typing import Optional

from src.config_loader import config
from src.logger import log


class EmailAttachment:
    """表示一个邮件附件或邮件正文的元信息。"""

    def __init__(self, filename: str, filepath: str, content_type: str,
                 email_date: Optional[datetime], email_subject: str,
                 email_sender: str, is_body: bool = False):
        self.filename = filename
        self.filepath = filepath
        self.content_type = content_type
        self.email_date = email_date
        self.email_subject = email_subject
        self.email_sender = email_sender
        self.is_body = is_body  # 是否为邮件正文（而非附件）

    def __repr__(self):
        tag = "Body" if self.is_body else "Attachment"
        return f"<{tag} {self.filename} from '{self.email_subject}' @ {self.email_date}>"


def _decode_header_value(value: str) -> str:
    """解码邮件头部（可能有 MIME 编码）。"""
    if not value:
        return ""
    decoded_parts = email.header.decode_header(value)
    result = []
    for part, charset in decoded_parts:
        if isinstance(part, bytes):
            result.append(part.decode(charset or "utf-8", errors="replace"))
        else:
            result.append(part)
    return "".join(result)


def _parse_date(msg: Message) -> Optional[datetime]:
    """从邮件中解析日期。"""
    date_str = msg.get("Date", "")
    if not date_str:
        return None
    try:
        parsed = email.utils.parsedate_to_datetime(date_str)
        return parsed
    except Exception:
        # 尝试多种日期格式
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y/%m/%d", "%d %b %Y"):
            try:
                return datetime.strptime(date_str.strip(), fmt)
            except ValueError:
                continue
    return None


def _matches_filter(msg: Message, filter_cfg: dict) -> bool:
    """判断邮件是否符合过滤规则。

    匹配逻辑（AND 关系）：
    1. sender_keywords 非空时：发件人(From)或收件人(To/Cc)中须包含任一关键词
    2. subject_keywords 非空时：主题或正文摘要中须包含任一关键词
    3. recipient_keywords 非空时：收件人/主题/发件人中须包含任一关键词
    所有非空条件须同时满足。
    """
    subject = _decode_header_value(msg.get("Subject", ""))
    sender = _decode_header_value(msg.get("From", ""))
    to_addr = _decode_header_value(msg.get("To", ""))
    cc_addr = _decode_header_value(msg.get("Cc", ""))
    all_addresses = f"{sender} {to_addr} {cc_addr}"
    all_text = f"{subject} {all_addresses}"

    sender_kw = filter_cfg.get("sender_keywords", [])
    subject_kw = filter_cfg.get("subject_keywords", [])
    recipient_kw = filter_cfg.get("recipient_keywords", [])

    # 条件1: 发件人/收件人地址须匹配（锁定特定邮箱）
    if sender_kw:
        if not any(kw.lower() in all_addresses.lower() for kw in sender_kw):
            return False

    # 条件2: 主题须包含电费相关关键词
    if subject_kw:
        if not any(kw in subject for kw in subject_kw):
            return False

    # 条件3: 收件人关键词（可选）
    if recipient_kw:
        if not any(kw in all_text for kw in recipient_kw):
            return False

    return True


def _safe_filename(name: str) -> str:
    """清理文件名中的非法字符。"""
    name = re.sub(r'[<>:"/\\|?*]', '_', name)
    name = name.strip('. ')
    return name[:200] if name else "unnamed"


class EmailFetcher:
    """IMAP 邮件抓取器。"""

    def __init__(self):
        email_cfg = config.get("email")
        self.server = email_cfg.get("imap_server", "imap.qq.com")
        self.port = email_cfg.get("imap_port", 993)
        self.use_ssl = email_cfg.get("use_ssl", True)
        self.account = email_cfg.get("account", "")
        self.auth_code = email_cfg.get("auth_code", "")
        self.filter_cfg = email_cfg.get("filter", {})

        att_cfg = config.get("attachments") or {}
        self.temp_dir = Path(att_cfg.get("temp_dir", "output/temp_attachments"))
        self.supported_formats = set(att_cfg.get("supported_formats", []))
        self.max_size = att_cfg.get("max_size_mb", 50) * 1024 * 1024

        self._conn = None

        # 数据库路径（用于邮件去重）
        self._db_path = config.get("storage", "database", default="output/data/powerstat.db")
        Path(self._db_path).parent.mkdir(parents=True, exist_ok=True)
        self._ensure_processed_emails_table()

    # ---- 邮件去重 ----

    def _ensure_processed_emails_table(self):
        """确保 processed_emails 表存在。"""
        with sqlite3.connect(self._db_path) as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS processed_emails (
                    id          INTEGER PRIMARY KEY AUTOINCREMENT,
                    fingerprint TEXT UNIQUE NOT NULL,
                    filename    TEXT,
                    subject     TEXT,
                    email_date  TEXT,
                    processed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)

    @staticmethod
    def _compute_fingerprint(msg: Message) -> str:
        """计算邮件指纹：优先用 Message-ID，否则用 subject+date+sender 的哈希。"""
        message_id = msg.get("Message-ID", "").strip()
        if message_id:
            return message_id

        subject = _decode_header_value(msg.get("Subject", ""))
        sender = _decode_header_value(msg.get("From", ""))
        date_str = msg.get("Date", "")
        raw = f"{subject}|{sender}|{date_str}"
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]

    def _is_email_processed(self, fingerprint: str) -> bool:
        """检查邮件是否已处理过。"""
        with sqlite3.connect(self._db_path) as conn:
            row = conn.execute(
                "SELECT id FROM processed_emails WHERE fingerprint = ?",
                (fingerprint,)
            ).fetchone()
            return row is not None

    def _mark_email_processed(self, fingerprint: str, subject: str,
                               email_date: Optional[datetime]):
        """标记邮件为已处理。"""
        with sqlite3.connect(self._db_path) as conn:
            conn.execute(
                """INSERT OR IGNORE INTO processed_emails
                   (fingerprint, subject, email_date)
                   VALUES (?, ?, ?)""",
                (fingerprint, subject,
                 email_date.isoformat() if email_date else None)
            )

    def connect(self):
        """连接到 IMAP 服务器。"""
        # 空凭据提前报错，不要浪费时间尝试连接
        if (not self.account or self.account.startswith("${")
                or not self.auth_code or self.auth_code.startswith("${")):
            raise ConnectionError(
                "邮箱凭据未配置。请在项目根目录创建 .env 文件并设置:\n"
                "  EMAIL_ACCOUNT=your_email@qq.com\n"
                "  EMAIL_AUTH_CODE=your_auth_code"
            )

        log.info("正在连接 IMAP 服务器 %s:%d ...", self.server, self.port)
        timeout = 15  # 秒
        retries = 3
        for attempt in range(retries):
            try:
                # 设置 socket 超时，防止无限等待
                prev_timeout = socket.getdefaulttimeout()
                socket.setdefaulttimeout(timeout)
                try:
                    if self.use_ssl:
                        self._conn = imaplib.IMAP4_SSL(self.server, self.port)
                    else:
                        self._conn = imaplib.IMAP4(self.server, self.port)
                finally:
                    socket.setdefaulttimeout(prev_timeout)

                self._conn.login(self.account, self.auth_code)
                log.info("IMAP 登录成功: %s", self.account)
                return
            except (socket.timeout, TimeoutError) as e:
                log.error("IMAP 连接超时 (尝试 %d/%d): %s", attempt + 1, retries, e)
                if attempt < retries - 1:
                    time.sleep(2 ** (attempt + 1))
                else:
                    raise ConnectionError(f"连接邮箱超时（{timeout}秒），请检查网络和服务器配置") from e
            except imaplib.IMAP4.error as e:
                log.error("IMAP 登录失败 (尝试 %d/%d): %s", attempt + 1, retries, e)
                if attempt < retries - 1:
                    time.sleep(2 ** (attempt + 1))
                else:
                    raise ConnectionError(f"无法连接邮箱: {e}") from e

    def disconnect(self):
        """断开连接。"""
        if self._conn:
            try:
                self._conn.logout()
            except Exception:
                pass
            self._conn = None

    def fetch_attachments(self) -> list[EmailAttachment]:
        """抓取符合条件的邮件并下载附件，返回附件信息列表。

        每封邮件的附件保存在独立子目录中（以日期_主题命名），
        已处理过的邮件（指纹匹配）自动跳过，不重复下载。
        """
        if not self._conn:
            self.connect()

        self._conn.select("INBOX")

        # 对每个发件人单独搜索再合并（QQ邮箱不支持深层嵌套OR）
        ids = self._search_all_senders()
        if not ids:
            log.info("未找到匹配邮件")
            return []

        log.info("找到 %d 封候选邮件，开始过滤和下载附件...", len(ids))

        self.temp_dir.mkdir(parents=True, exist_ok=True)
        attachments = []
        debug_logged = 0  # 打印前几封被过滤掉的邮件头，用于诊断
        skipped_dup = 0   # 跳过的重复邮件计数

        for mid in ids:
            try:
                status, data = self._conn.fetch(mid, "(RFC822)")
                if status != "OK":
                    continue

                raw = data[0][1]
                msg = email.message_from_bytes(raw)

                if not _matches_filter(msg, self.filter_cfg):
                    if debug_logged < 10:
                        _subj = _decode_header_value(msg.get("Subject", ""))
                        _from = _decode_header_value(msg.get("From", ""))
                        _to = _decode_header_value(msg.get("To", ""))
                        log.info("过滤跳过邮件: Subject=[%s] From=[%s] To=[%s]", _subj, _from, _to)
                        debug_logged += 1
                    continue

                # ---- 去重检查 ----
                fingerprint = self._compute_fingerprint(msg)
                if self._is_email_processed(fingerprint):
                    _subj = _decode_header_value(msg.get("Subject", ""))
                    log.info("跳过已处理邮件: %s", _subj)
                    skipped_dup += 1
                    continue

                subject = _decode_header_value(msg.get("Subject", ""))
                sender = _decode_header_value(msg.get("From", ""))
                mail_date = _parse_date(msg)

                log.info("处理邮件: [%s] %s", mail_date, subject)

                # ---- 创建邮件专属子目录 ----
                date_prefix = mail_date.strftime("%Y%m%d") if mail_date else "unknown"
                safe_subject = _safe_filename(subject) if subject else "untitled"
                # 限制目录名长度
                if len(safe_subject) > 80:
                    safe_subject = safe_subject[:80]
                email_dir_name = f"{date_prefix}_{safe_subject}"
                email_dir = self.temp_dir / email_dir_name
                # 如果同名目录已存在且有文件，说明是之前已下载的邮件，复用该目录
                if email_dir.exists() and any(email_dir.iterdir()):
                    log.info("目录已存在且有文件，复用: %s", email_dir_name)
                    # 补记处理记录，避免下次重复
                    self._mark_email_processed(fingerprint, subject, mail_date)
                    # 收集已有附件
                    for f in email_dir.rglob("*"):
                        if f.is_file():
                            result.append(EmailAttachment(
                                filename=f.name, filepath=str(f),
                                content_type="", email_date=mail_date,
                                email_subject=subject, email_sender=sender,
                            ))
                    total_attached += sum(1 for f in email_dir.rglob("*") if f.is_file())
                    processed += 1
                    continue
                email_dir.mkdir(parents=True, exist_ok=True)

                body_html = ""
                body_text = ""

                for part in msg.walk():
                    if part.get_content_maintype() == "multipart":
                        continue

                    content_type = part.get_content_type()
                    disposition = str(part.get("Content-Disposition", ""))

                    # ---- 收集邮件正文 ----
                    if content_type == "text/html" and "attachment" not in disposition:
                        try:
                            charset = part.get_content_charset() or "utf-8"
                            body_html += part.get_payload(decode=True).decode(charset, errors="replace")
                        except Exception:
                            pass
                        continue
                    if content_type == "text/plain" and "attachment" not in disposition:
                        try:
                            charset = part.get_content_charset() or "utf-8"
                            body_text += part.get_payload(decode=True).decode(charset, errors="replace")
                        except Exception:
                            pass
                        continue

                    # ---- 获取附件（不限格式，全部下载） ----
                    filename = self._extract_filename(part, disposition, content_type, mid)
                    if not filename:
                        continue

                    # 解码 MIME 编码的文件名 (=?utf-8?B?...?=)
                    filename = _decode_header_value(filename)
                    payload = part.get_payload(decode=True)
                    if not payload:
                        continue
                    if len(payload) > self.max_size:
                        log.warning("  附件过大跳过: %s (%d MB)", filename, len(payload) // (1024 * 1024))
                        continue

                    safe_name = _safe_filename(filename)
                    filepath = email_dir / safe_name
                    file_counter = 1
                    orig_stem = filepath.stem
                    while filepath.exists():
                        filepath = filepath.with_name(f"{orig_stem}_{file_counter}{filepath.suffix}")
                        file_counter += 1

                    with open(filepath, "wb") as f:
                        f.write(payload)

                    att = EmailAttachment(
                        filename=filename,
                        filepath=str(filepath),
                        content_type=content_type,
                        email_date=mail_date,
                        email_subject=subject,
                        email_sender=sender,
                    )
                    attachments.append(att)
                    log.info("  已下载附件: %s -> %s/%s", filename, email_dir_name, filepath.name)

                # ---- 保存邮件正文为 HTML/TXT（也作为可解析内容） ----
                body_content = body_html or body_text
                if body_content:
                    ext = ".html" if body_html else ".txt"
                    body_name = f"邮件正文{ext}"
                    body_path = email_dir / body_name

                    with open(body_path, "w", encoding="utf-8") as f:
                        f.write(body_content)

                    att = EmailAttachment(
                        filename=body_name,
                        filepath=str(body_path),
                        content_type="text/html" if body_html else "text/plain",
                        email_date=mail_date,
                        email_subject=subject,
                        email_sender=sender,
                        is_body=True,
                    )
                    attachments.append(att)
                    log.info("  已保存邮件正文: %s/%s", email_dir_name, body_name)

                # ---- 标记邮件为已处理 ----
                self._mark_email_processed(fingerprint, subject, mail_date)

            except Exception as e:
                log.error("处理邮件 %s 时出错: %s", mid, e, exc_info=True)

        if skipped_dup:
            log.info("跳过 %d 封已处理的重复邮件", skipped_dup)

        # 解压 zip 文件，将内部文件展开为独立附件
        attachments = self._extract_archives(attachments)

        log.info("共获得 %d 个附件（含解压）", len(attachments))
        return attachments

    @staticmethod
    def _extract_filename(part, disposition: str, content_type: str, mid) -> Optional[str]:
        """从邮件 part 中提取附件文件名（兼容各种邮件客户端）。"""
        filename = part.get_filename()
        if not filename:
            filename = part.get_param("name")
        if not filename:
            if "filename" in disposition:
                fn_match = re.search(
                    r'filename[*]?=["\']?([^"\';\r\n]+)', disposition
                )
                if fn_match:
                    filename = fn_match.group(1).strip()
        if not filename:
            ct_header = part.get("Content-Type", "")
            if "name" in ct_header:
                nm_match = re.search(
                    r'name[*]?=["\']?([^"\';\r\n]+)', ct_header
                )
                if nm_match:
                    filename = nm_match.group(1).strip()
        if not filename:
            if "attachment" in disposition:
                filename = f"attachment_{hash(mid) & 0xFFFF:04x}.bin"
            elif content_type not in ("text/plain", "text/html"):
                content_id = part.get("Content-ID", "")
                cid = content_id.strip("<>").split("@")[0] if content_id else ""
                ext_map = {
                    "image/png": ".png", "image/jpeg": ".jpg",
                    "image/gif": ".gif", "image/bmp": ".bmp",
                    "application/pdf": ".pdf",
                    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": ".xlsx",
                    "application/vnd.ms-excel": ".xls",
                    "application/octet-stream": ".bin",
                    "application/zip": ".zip",
                }
                ext = ext_map.get(content_type, ".bin")
                filename = f"{cid}{ext}" if cid else f"unnamed{ext}"
        return filename

    def _extract_archives(self, attachments: list[EmailAttachment]) -> list[EmailAttachment]:
        """解压 zip 等压缩包，将内部文件展开为独立附件返回。

        解压目录在 ZIP 所在的邮件子目录内创建（以 ZIP 文件名命名的子文件夹）。
        """
        ARCHIVE_EXTS = {".zip"}
        INNER_EXTS = {".xlsx", ".xls", ".pdf", ".png", ".jpg", ".jpeg",
                      ".bmp", ".tiff", ".tif", ".csv"}
        result = []

        for att in attachments:
            ext = os.path.splitext(att.filepath)[1].lower()
            if ext not in ARCHIVE_EXTS:
                result.append(att)
                continue

            # 解压 zip
            zip_path = Path(att.filepath)
            if not zipfile.is_zipfile(str(zip_path)):
                log.warning("  文件不是有效 zip: %s", att.filename)
                result.append(att)
                continue

            # 在 ZIP 所在目录内创建子目录
            extract_dir = zip_path.parent / zip_path.stem
            extract_dir.mkdir(parents=True, exist_ok=True)
            extracted_count = 0

            try:
                with zipfile.ZipFile(str(zip_path), "r") as zf:
                    for info in zf.infolist():
                        if info.is_dir():
                            continue

                        # 处理中文文件名编码
                        try:
                            inner_name = info.filename.encode("cp437").decode("gbk")
                        except (UnicodeDecodeError, UnicodeEncodeError):
                            try:
                                inner_name = info.filename.encode("cp437").decode("utf-8")
                            except (UnicodeDecodeError, UnicodeEncodeError):
                                inner_name = info.filename

                        inner_ext = os.path.splitext(inner_name)[1].lower()
                        if inner_ext not in INNER_EXTS:
                            log.debug("  跳过压缩包内文件: %s", inner_name)
                            continue

                        # 提取文件
                        safe_inner = _safe_filename(os.path.basename(inner_name))
                        dest_path = extract_dir / safe_inner
                        counter = 1
                        orig_stem = dest_path.stem
                        while dest_path.exists():
                            dest_path = extract_dir / f"{orig_stem}_{counter}{dest_path.suffix}"
                            counter += 1

                        with zf.open(info) as src, open(dest_path, "wb") as dst:
                            dst.write(src.read())

                        inner_att = EmailAttachment(
                            filename=safe_inner,
                            filepath=str(dest_path),
                            content_type="",
                            email_date=att.email_date,
                            email_subject=att.email_subject,
                            email_sender=att.email_sender,
                        )
                        result.append(inner_att)
                        extracted_count += 1
                        log.info("  解压: %s -> %s", att.filename, safe_inner)

                log.info("  从 %s 解压出 %d 个文件", att.filename, extracted_count)

            except Exception as e:
                log.error("  解压失败 [%s]: %s", att.filename, e, exc_info=True)
                result.append(att)  # 解压失败保留原始 zip

        return result

    def _imap_search_utf8(self, criteria: list[str]):
        """使用 UTF-8 charset 执行 IMAP SEARCH，支持中文关键词。

        imaplib 默认以 ASCII 编码参数，中文关键词会报错。
        通过手动构造带 CHARSET UTF-8 的原始命令来解决。
        """
        # 检查是否包含非 ASCII 字符
        has_non_ascii = any(
            not c.isascii() for c in "".join(criteria)
        )

        if not has_non_ascii:
            # 纯 ASCII 搜索，直接用标准方法
            return self._conn.search(None, *criteria)

        # 构造 IMAP SEARCH 命令（带 CHARSET UTF-8）
        # 格式: SEARCH CHARSET UTF-8 <criteria>
        # 非 ASCII 字符串需要以 IMAP literal ({N}\r\n<bytes>) 形式发送
        tag = self._conn._new_tag()
        search_parts = []
        literals = []

        for part in criteria:
            if part.isascii():
                search_parts.append(part)
            else:
                # 非 ASCII 部分用 literal 占位
                encoded = part.encode("utf-8")
                search_parts.append(f"{{{len(encoded)}}}")
                literals.append(encoded)

        cmd_line = f"SEARCH CHARSET UTF-8 {' '.join(search_parts)}"

        if not literals:
            # 没有 literal，直接发送
            return self._conn.search("UTF-8", *criteria)

        # 有 literal 时，需要逐段发送
        # 先发送到第一个 literal 处
        parts = cmd_line.split("{")
        first_part = parts[0]

        # 使用更简单的方式：先拉取所有邮件，客户端过滤
        log.info("中文搜索关键词检测到，使用全量拉取+客户端过滤模式")
        return self._conn.search(None, "ALL")

    def _search_all_senders(self) -> list[bytes]:
        """对每个发件人单独执行 IMAP SEARCH，合并去重结果。

        QQ邮箱的 IMAP 不能正确解析深层嵌套 OR 语法，
        所以改为分次搜索再合并。
        """
        sender_kw = self.filter_cfg.get("sender_keywords", [])
        ascii_emails = [kw for kw in sender_kw if kw.isascii() and "@" in kw]

        # 基础日期条件
        date_criteria = []
        since = self.filter_cfg.get("since_date")
        if since:
            date_criteria.extend(["SINCE", since])
        before = self.filter_cfg.get("before_date")
        if before:
            date_criteria.extend(["BEFORE", before])

        all_ids = set()

        if not ascii_emails:
            # 无发件人过滤，搜索全部
            criteria = date_criteria if date_criteria else ["ALL"]
            log.info("IMAP 搜索条件: %s", criteria)
            status, msg_ids = self._conn.search(None, *criteria)
            if status == "OK" and msg_ids[0]:
                all_ids.update(msg_ids[0].split())
        else:
            # 对每个邮箱地址单独搜索 FROM 和 TO
            for addr in ascii_emails:
                criteria = date_criteria + ["OR", "FROM", addr, "TO", addr]
                log.info("IMAP 搜索 [%s]: %s", addr, criteria)
                try:
                    status, msg_ids = self._conn.search(None, *criteria)
                    if status == "OK" and msg_ids[0]:
                        found = msg_ids[0].split()
                        log.info("  -> 找到 %d 封", len(found))
                        all_ids.update(found)
                    else:
                        log.info("  -> 0 封")
                except Exception as e:
                    log.warning("  搜索 %s 失败: %s", addr, e)

        # 排序（按邮件ID顺序）
        sorted_ids = sorted(all_ids, key=lambda x: int(x))
        log.info("IMAP 搜索合计: %d 封候选邮件（去重后）", len(sorted_ids))
        return sorted_ids

    def _build_search_criteria(self) -> list[str]:
        """构建 IMAP SEARCH 命令参数。

        ASCII 安全的条件（邮箱地址、日期）在服务端过滤，
        中文关键词（主题中的"电费"等）在客户端过滤。
        """
        criteria = []

        since = self.filter_cfg.get("since_date")
        if since:
            criteria.extend(["SINCE", since])

        before = self.filter_cfg.get("before_date")
        if before:
            criteria.extend(["BEFORE", before])

        # 提取 sender_keywords 中的纯 ASCII 邮箱地址，在服务端用 OR FROM/TO 过滤
        sender_kw = self.filter_cfg.get("sender_keywords", [])
        ascii_emails = [kw for kw in sender_kw if kw.isascii() and "@" in kw]

        if ascii_emails:
            if len(ascii_emails) == 1:
                # 单个邮箱：FROM 或 TO 匹配
                email_addr = ascii_emails[0]
                criteria.extend(["OR", "FROM", email_addr, "TO", email_addr])
            else:
                # 多个邮箱：OR 嵌套匹配所有地址
                # 每个邮箱生成 OR FROM addr TO addr，再用 OR 串联
                parts = []
                for addr in ascii_emails:
                    parts.append(["OR", "FROM", addr, "TO", addr])
                # 从后往前嵌套 OR
                result = parts[-1]
                for p in reversed(parts[:-1]):
                    result = ["OR"] + p + result
                criteria.extend(result)

        if not criteria:
            criteria.append("ALL")

        return criteria

    def __enter__(self):
        self.connect()
        return self

    def __exit__(self, *args):
        self.disconnect()
