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
from datetime import datetime
from email.message import Message
from pathlib import Path
from typing import Optional

from src.config_loader import config
from src.logger import log


class EmailAttachment:
    """表示一个邮件附件的元信息。"""

    def __init__(self, filename: str, filepath: str, content_type: str,
                 email_date: Optional[datetime], email_subject: str,
                 email_sender: str):
        self.filename = filename
        self.filepath = filepath
        self.content_type = content_type
        self.email_date = email_date
        self.email_subject = email_subject
        self.email_sender = email_sender

    def __repr__(self):
        return f"<Attachment {self.filename} from '{self.email_subject}' @ {self.email_date}>"


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

                for part in msg.walk():
                    if part.get_content_maintype() == "multipart":
                        continue

                    filename = part.get_filename()
                    if not filename:
                        continue

                    filename = _decode_header_value(filename)
                    ext = os.path.splitext(filename)[1].lower()

                    if self.supported_formats and ext not in self.supported_formats:
                        log.debug("跳过不支持的附件格式: %s", filename)
                        continue

                    payload = part.get_payload(decode=True)
                    if not payload:
                        continue
                    if len(payload) > self.max_size:
                        log.warning("附件过大，跳过: %s (%d MB)", filename, len(payload) // (1024 * 1024))
                        continue

                    # 用日期前缀避免重名
                    date_prefix = mail_date.strftime("%Y%m%d") if mail_date else "unknown"
                    safe_name = f"{date_prefix}_{_safe_filename(filename)}"
                    filepath = self.temp_dir / safe_name

                    # 处理重名
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
                        content_type=part.get_content_type(),
                        email_date=mail_date,
                        email_subject=subject,
                        email_sender=sender,
                    )
                    attachments.append(att)
                    log.info("  已下载附件: %s -> %s", filename, filepath.name)

            except Exception as e:
                log.error("处理邮件 %s 时出错: %s", mid, e, exc_info=True)

        log.info("共下载 %d 个附件", len(attachments))
        return attachments

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

        注意：中文关键词无法通过标准 imaplib 发送，
        因此仅在服务器端使用日期范围等 ASCII 安全条件，
        中文关键词（主题/发件人）过滤在客户端完成。
        """
        criteria = []

        since = self.filter_cfg.get("since_date")
        if since:
            criteria.extend(["SINCE", since])

        before = self.filter_cfg.get("before_date")
        if before:
            criteria.extend(["BEFORE", before])

        if not criteria:
            criteria.append("ALL")

        return criteria

    def __enter__(self):
        self.connect()
        return self

    def __exit__(self, *args):
        self.disconnect()
