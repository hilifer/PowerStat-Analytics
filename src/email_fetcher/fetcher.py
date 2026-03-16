"""QQ 邮箱 IMAP 邮件抓取模块。

通过 IMAP 连接 QQ 邮箱，根据配置的过滤规则筛选电费相关邮件，
下载附件到本地临时目录。
"""

import email
import email.header
import imaplib
import os
import re
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

    def connect(self):
        """连接到 IMAP 服务器。"""
        log.info("正在连接 IMAP 服务器 %s:%d ...", self.server, self.port)
        retries = 3
        for attempt in range(retries):
            try:
                if self.use_ssl:
                    self._conn = imaplib.IMAP4_SSL(self.server, self.port)
                else:
                    self._conn = imaplib.IMAP4(self.server, self.port)
                self._conn.login(self.account, self.auth_code)
                log.info("IMAP 登录成功: %s", self.account)
                return
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
        """抓取符合条件的邮件并下载附件，返回附件信息列表。"""
        if not self._conn:
            self.connect()

        self._conn.select("INBOX")

        # 构建搜索条件（仅用 ASCII 安全的条件做服务器端过滤）
        search_criteria = self._build_search_criteria()
        log.info("IMAP 搜索条件: %s", search_criteria)

        # 使用 charset=UTF-8 发送搜索以支持中文关键词
        status, msg_ids = self._imap_search_utf8(search_criteria)
        if status != "OK" or not msg_ids[0]:
            log.info("未找到匹配邮件")
            return []

        ids = msg_ids[0].split()
        log.info("找到 %d 封候选邮件，开始过滤和下载附件...", len(ids))

        self.temp_dir.mkdir(parents=True, exist_ok=True)
        attachments = []
        debug_logged = 0  # 打印前几封被过滤掉的邮件头，用于诊断

        for mid in ids:
            try:
                status, data = self._conn.fetch(mid, "(RFC822)")
                if status != "OK":
                    continue

                raw = data[0][1]
                msg = email.message_from_bytes(raw)

                if not _matches_filter(msg, self.filter_cfg):
                    if debug_logged < 5:
                        _subj = _decode_header_value(msg.get("Subject", ""))
                        _from = _decode_header_value(msg.get("From", ""))
                        _to = _decode_header_value(msg.get("To", ""))
                        log.debug("跳过邮件: Subject=[%s] From=[%s] To=[%s]", _subj, _from, _to)
                        debug_logged += 1
                    continue

                subject = _decode_header_value(msg.get("Subject", ""))
                sender = _decode_header_value(msg.get("From", ""))
                mail_date = _parse_date(msg)

                log.info("处理邮件: [%s] %s", mail_date, subject)

                date_prefix = mail_date.strftime("%Y%m%d") if mail_date else "unknown"
                body_html = ""
                body_text = ""
                has_real_attachment = False

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
                    filename = part.get_filename()
                    if not filename:
                        filename = part.get_param("name")
                    if not filename:
                        # 无名附件按 content-type 命名
                        content_id = part.get("Content-ID", "")
                        cid = content_id.strip("<>").split("@")[0] if content_id else ""
                        ext_map = {
                            "image/png": ".png", "image/jpeg": ".jpg", "image/gif": ".gif",
                            "image/bmp": ".bmp", "application/pdf": ".pdf",
                            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": ".xlsx",
                            "application/vnd.ms-excel": ".xls",
                        }
                        ext_guess = ext_map.get(content_type, "")
                        if cid:
                            filename = f"{cid}{ext_guess}"
                        elif ext_guess:
                            filename = f"unnamed{ext_guess}"
                        else:
                            # 完全未知的附件也下载
                            sub_type = content_type.split("/")[-1].split(";")[0]
                            filename = f"unnamed.{sub_type}" if sub_type != "octet-stream" else None

                    if not filename:
                        continue

                    filename = _decode_header_value(filename)
                    payload = part.get_payload(decode=True)
                    if not payload:
                        continue
                    if len(payload) > self.max_size:
                        log.warning("  附件过大跳过: %s (%d MB)", filename, len(payload) // (1024 * 1024))
                        continue

                    safe_name = f"{date_prefix}_{_safe_filename(filename)}"
                    filepath = self.temp_dir / safe_name
                    counter = 1
                    orig_stem = filepath.stem
                    while filepath.exists():
                        filepath = filepath.with_name(f"{orig_stem}_{counter}{filepath.suffix}")
                        counter += 1

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
                    has_real_attachment = True
                    log.info("  已下载附件: %s -> %s", filename, filepath.name)

                # ---- 保存邮件正文为 HTML/TXT（也作为可解析内容） ----
                body_content = body_html or body_text
                if body_content:
                    ext = ".html" if body_html else ".txt"
                    body_name = f"{date_prefix}_邮件正文_{_safe_filename(subject)}{ext}"
                    body_path = self.temp_dir / body_name
                    counter = 1
                    orig_stem = body_path.stem
                    while body_path.exists():
                        body_path = body_path.with_name(f"{orig_stem}_{counter}{body_path.suffix}")
                        counter += 1
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
                    log.info("  已保存邮件正文: %s", body_name)

            except Exception as e:
                log.error("处理邮件 %s 时出错: %s", mid, e, exc_info=True)

        # 解压 zip 文件，将内部文件展开为独立附件
        attachments = self._extract_archives(attachments)

        log.info("共获得 %d 个附件（含解压）", len(attachments))
        return attachments

    def _extract_archives(self, attachments: list[EmailAttachment]) -> list[EmailAttachment]:
        """解压 zip 等压缩包，将内部文件展开为独立附件返回。"""
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
                # 多个邮箱：用第一个
                email_addr = ascii_emails[0]
                criteria.extend(["OR", "FROM", email_addr, "TO", email_addr])

        if not criteria:
            criteria.append("ALL")

        return criteria

    def __enter__(self):
        self.connect()
        return self

    def __exit__(self, *args):
        self.disconnect()
