"""IMAP 收信公共层。

Outlook / Hotmail 走 office365.com（现代认证开 OAuth2 时用 XOAUTH2，这里先实现
应用密码 / 普通口令的 LOGIN 路径，OAuth2 留了钩子），iCloud 聚合邮箱走 imap.mail.me.com。

收信策略：记录每个邮箱的 last_uid，增量只取新信，避免整箱扫描。
"""

from __future__ import annotations

import email
import imaplib
import re
import time
from datetime import datetime
from email.header import decode_header, make_header
from typing import Iterable

from app.mail.base import MailMessage


def _decode(value: str | None) -> str:
    if not value:
        return ""
    try:
        return str(make_header(decode_header(value)))
    except Exception:
        return value


def _strip_html(text: str) -> str:
    return re.sub(r"<[^>]+>", " ", text)


def _body_of(msg: email.message.Message) -> tuple[str, str]:
    """返回 (text, html)。"""
    text_parts: list[str] = []
    html_parts: list[str] = []
    if msg.is_multipart():
        for part in msg.walk():
            ctype = part.get_content_type()
            if part.get("Content-Disposition", "").startswith("attachment"):
                continue
            try:
                payload = part.get_payload(decode=True) or b""
                charset = part.get_content_charset() or "utf-8"
                content = payload.decode(charset, errors="replace")
            except Exception:
                continue
            if ctype == "text/plain":
                text_parts.append(content)
            elif ctype == "text/html":
                html_parts.append(content)
    else:
        try:
            payload = msg.get_payload(decode=True) or b""
            content = payload.decode(msg.get_content_charset() or "utf-8", errors="replace")
        except Exception:
            content = ""
        if msg.get_content_type() == "text/html":
            html_parts.append(content)
        else:
            text_parts.append(content)
    return "\n".join(text_parts), "\n".join(html_parts)


class ImapMailbox:
    """单个 IMAP 邮箱会话封装。"""

    def __init__(
        self,
        host: str,
        user: str,
        password: str,
        port: int = 993,
        mailbox: str = "INBOX",
        use_ssl: bool = True,
        timeout: int = 30,
    ) -> None:
        self.host = host
        self.user = user
        self.password = password
        self.port = port
        self.mailbox = mailbox
        self.timeout = timeout
        self._conn: imaplib.IMAP4_SSL | imaplib.IMAP4 | None = None

    # ---- lifecycle --------------------------------------------------------
    def connect(self) -> "ImapMailbox":
        cls = imaplib.IMAP4_SSL if self.port == 993 else imaplib.IMAP4
        self._conn = cls(self.host, self.port, timeout=self.timeout)
        self._conn.login(self.user, self.password)
        self._conn.select(self.mailbox)
        return self

    def close(self) -> None:
        if self._conn is None:
            return
        try:
            self._conn.close()
        except Exception:
            pass
        try:
            self._conn.logout()
        except Exception:
            pass
        self._conn = None

    def __enter__(self) -> "ImapMailbox":
        return self.connect()

    def __exit__(self, *exc) -> None:
        self.close()

    # ---- fetch ------------------------------------------------------------
    def fetch_uid_list(self, since_uid: int = 0) -> list[int]:
        assert self._conn is not None
        typ, data = self._conn.uid("SEARCH", None, "ALL")
        if typ != "OK" or not data or data[0] is None:
            return []
        uids = [int(x) for x in data[0].split() if x.isdigit()]
        return [u for u in uids if u > since_uid]

    def fetch_message(self, uid: int) -> MailMessage | None:
        assert self._conn is not None
        typ, data = self._conn.uid("FETCH", str(uid), "(RFC822)")
        if typ != "OK" or not data:
            return None
        raw = None
        for part in data:
            if isinstance(part, tuple) and len(part) >= 2:
                raw = part[1]
                break
        if raw is None:
            return None
        parsed = email.message_from_bytes(raw)
        text, html = _body_of(parsed)
        to_raw = _decode(parsed.get("To", ""))
        to_list = [a.strip() for a in to_raw.split(",") if a.strip()]
        delivered = _decode(parsed.get("Delivered-To", ""))
        if delivered:
            to_list.append(delivered)
        return MailMessage(
            uid=str(uid),
            mailbox=self.mailbox,
            to=to_list,
            sender=_decode(parsed.get("From", "")),
            subject=_decode(parsed.get("Subject", "")),
            body_text=text or _strip_html(html),
            body_html=html,
            received_at=email.utils.parsedate_to_datetime(parsed.get("Date", ""))
            if parsed.get("Date")
            else None,
        )

    def fetch_all(self, since_uid: int = 0) -> Iterable[MailMessage]:
        for uid in self.fetch_uid_list(since_uid):
            msg = self.fetch_message(uid)
            if msg is not None:
                yield msg

    def max_uid(self) -> int:
        uids = self.fetch_uid_list(0)
        return max(uids) if uids else 0


def wait_in_mailbox(
    mailbox: ImapMailbox,
    since_uid: int,
    timeout: int,
    match,
    poll_interval: int = 8,
) -> MailMessage | None:
    """轮询一个 IMAP 邮箱，直到命中匹配邮件或超时。"""
    deadline = time.time() + timeout
    cursor = since_uid
    with mailbox:
        while time.time() < deadline:
            for msg in mailbox.fetch_all(cursor):
                cursor = max(cursor, int(msg.uid))
                if match(msg):
                    return msg
            time.sleep(poll_interval)
    return None
