"""Outlook/Outlook.com 邮箱客户端（OAuth2 + IMAP XOAUTH2）。

为什么不用 imaplib.authenticate()
--------------------------------
``imaplib.authenticate()`` 会把回调返回值当 **literal** 发送：

    AUTHENTICATE XOAUTH2 {123}
    <base64>

而 Microsoft 的 XOAUTH2 要求裸 base64 字符串：

    AUTHENTICATE XOAUTH2
    + （服务器要求继续）
    <base64>

格式不符会报 ``AUTHENTICATE command error: BAD [b'Command Argument Error. 12']``。

所以这里手工控制协议流程：发 AUTHENTICATE → 等 ``+`` → 发 base64 → 读结果。

邮箱清单格式（发货格式）
------------------------
    邮箱----密码----clientid----refresh_token----辅助邮箱----辅助邮箱密码
"""

from __future__ import annotations

import base64
import dataclasses
import email
import imaplib
import re
import socket
import time
from datetime import datetime
from email.header import decode_header, make_header
from pathlib import Path

import requests

TOKEN_URL = "https://login.microsoftonline.com/consumers/oauth2/v2.0/token"
DEFAULT_SCOPE = ("https://outlook.office.com/IMAP.AccessAsUser.All "
                 "https://outlook.office.com/SMTP.Send offline_access")
IMAP_HOST = "outlook.office365.com"


# ============================================================ 数据类
@dataclasses.dataclass(slots=True)
class MailAccount:
    email: str
    password: str = ""
    client_id: str = ""
    refresh_token: str = ""
    recovery_email: str = ""
    recovery_password: str = ""
    extra: dict = dataclasses.field(default_factory=dict)

    @property
    def has_oauth(self) -> bool:
        return bool(self.client_id and self.refresh_token)


@dataclasses.dataclass(slots=True)
class TokenBundle:
    access_token: str
    refresh_token: str = ""
    expires_in: int = 3600
    token_type: str = "Bearer"
    scope: str = ""
    obtained_at: float = dataclasses.field(default_factory=time.time)

    @property
    def expired(self) -> bool:
        return time.time() > self.obtained_at + self.expires_in - 120


@dataclasses.dataclass(slots=True)
class MailMsg:
    uid: str
    subject: str
    sender: str
    to: list[str]
    body_text: str
    body_html: str
    received_at: datetime | None = None

    LINK_RE = re.compile(r"""https?://[^\s"'<>\)\]]+""", re.IGNORECASE)

    def links(self) -> list[str]:
        import html as _html
        out: list[str] = []
        for raw in self.LINK_RE.findall(self.body_text) + self.LINK_RE.findall(self.body_html):
            link = _html.unescape(raw).rstrip(".,;)]>\"'")
            if link not in out:
                out.append(link)
        return out

    def confirm_link(self, hosts: tuple[str, ...] = ("qoder.com", "qoder.sh")) -> str | None:
        cands = self.links()
        if not cands:
            return None

        def score(u: str) -> int:
            low = u.lower()
            s = 0
            if any(h in low for h in hosts):
                s += 10
            if any(k in low for k in ("confirm", "verify", "activate", "token", "code", "validation")):
                s += 6
            if any(k in low for k in ("unsubscribe", "privacy", "terms", "help")):
                s -= 12
            return s

        best = max(cands, key=score)
        return best if score(best) > 0 else None


# ============================================================ 解析
def parse_line(line: str) -> MailAccount | None:
    p = [x.strip() for x in line.strip().split("----")]
    if len(p) < 2 or "@" not in p[0]:
        return None
    return MailAccount(
        email=p[0],
        password=p[1] if len(p) > 1 else "",
        client_id=p[2] if len(p) > 2 else "",
        refresh_token=p[3] if len(p) > 3 else "",
        recovery_email=p[4] if len(p) > 4 else "",
        recovery_password=p[5] if len(p) > 5 else "",
    )


def load_accounts(path: str | Path) -> list[MailAccount]:
    f = Path(path)
    if not f.exists():
        return []
    out: list[MailAccount] = []
    for line in f.read_text(encoding="utf-8", errors="ignore").splitlines():
        a = parse_line(line)
        if a:
            out.append(a)
    return out


# ============================================================ 解码
def _dec(v: str | None) -> str:
    if not v:
        return ""
    try:
        return str(make_header(decode_header(v)))
    except Exception:
        return v


def _body(msg: email.message.Message) -> tuple[str, str]:
    text, html = [], []
    if msg.is_multipart():
        for part in msg.walk():
            if part.get("Content-Disposition", "").startswith("attachment"):
                continue
            ctype = part.get_content_type()
            try:
                content = (part.get_payload(decode=True) or b"").decode(
                    part.get_content_charset() or "utf-8", "replace")
            except Exception:
                continue
            if ctype == "text/plain":
                text.append(content)
            elif ctype == "text/html":
                html.append(content)
    else:
        try:
            content = (msg.get_payload(decode=True) or b"").decode(
                msg.get_content_charset() or "utf-8", "replace")
        except Exception:
            content = ""
        (html if msg.get_content_type() == "text/html" else text).append(content)
    return "\n".join(text), "\n".join(html)


# ============================================================ 客户端
class OutlookMailbox:
    """一个 Outlook 邮箱的 OAuth2 + IMAP 封装。"""

    def __init__(self, account: MailAccount, host: str = IMAP_HOST,
                 scope: str = DEFAULT_SCOPE, proxy: str | None = None,
                 timeout: int = 30) -> None:
        self.acc = account
        self.host = host
        self.scope = scope
        self.proxy = proxy
        self.timeout = timeout
        self.token: TokenBundle | None = None
        self._conn: imaplib.IMAP4_SSL | None = None

    # ---------------------------------------------------------- token
    def refresh_access_token(self) -> TokenBundle:
        data = {
            "client_id": self.acc.client_id,
            "grant_type": "refresh_token",
            "refresh_token": self.acc.refresh_token,
            "scope": self.scope,
        }
        proxies = {"http": self.proxy, "https": self.proxy} if self.proxy else None
        r = requests.post(TOKEN_URL, data=data, proxies=proxies, timeout=self.timeout)
        j = r.json()
        if "access_token" not in j:
            raise RuntimeError(
                f"换 token 失败：{j.get('error')} - {j.get('error_description', '')[:150]}")
        tb = TokenBundle(
            access_token=j["access_token"],
            refresh_token=j.get("refresh_token", ""),
            expires_in=int(j.get("expires_in", 3600)),
            token_type=j.get("token_type", "Bearer"),
            scope=j.get("scope", ""),
        )
        self.token = tb
        # refresh_token 可能轮换，回写
        if tb.refresh_token and tb.refresh_token != self.acc.refresh_token:
            self.acc.refresh_token = tb.refresh_token
        return tb

    # ---------------------------------------------------------- IMAP
    def _xoauth2(self, conn: imaplib.IMAP4_SSL) -> None:
        """手工 XOAUTH2 认证（避开 imaplib 的 literal 格式问题）。"""
        token = self.token.access_token if self.token else ""
        auth_str = f"user={self.acc.email}\x01auth=Bearer {token}\x01\x01"
        b64 = base64.b64encode(auth_str.encode()).decode()

        tag = conn._new_tag().decode() if isinstance(conn._new_tag(), bytes) else conn._new_tag()
        conn.send(f"{tag} AUTHENTICATE XOAUTH2\r\n".encode())

        # 读 continuation
        line = conn.readline()
        if not line.startswith(b"+"):
            raise RuntimeError(f"服务器未要求继续：{line[:120]}")

        conn.send(f"{b64}\r\n".encode())
        resp = conn.readline()

        if b"OK" in resp.upper():
            self._authed_tag = tag
            # 关键：手工认证绕过了 imaplib 的 authenticate()，
            # 它内部状态机还停在 NONAUTH，会导致后续 SELECT 报
            # "command SELECT illegal in state NONAUTH"。
            # 认证成功后手动推进状态。
            conn.state = "AUTH"
            return
        # 解析错误
        err = resp.decode("utf-8", "ignore")[:200]
        try:
            import base64 as b2
            payload = b2.b64decode(err.split()[-1])
            err = payload.decode("utf-8", "ignore")
        except Exception:
            pass
        raise RuntimeError(f"XOAUTH2 认证失败：{err}")

    def connect(self) -> "OutlookMailbox":
        if self.token is None or self.token.expired:
            self.refresh_access_token()
        conn = imaplib.IMAP4_SSL(self.host, 993, timeout=self.timeout)
        self._xoauth2(conn)
        self._conn = conn
        return self

    def close(self) -> None:
        if self._conn is None:
            return
        try:
            self._conn.logout()
        except Exception:
            pass
        self._conn = None

    def __enter__(self) -> "OutlookMailbox":
        return self.connect()

    def __exit__(self, *exc) -> None:
        self.close()

    # ---------------------------------------------------------- 读信
    def select(self, box: str = "INBOX") -> int:
        assert self._conn is not None
        typ, data = self._conn.select(box)
        if typ != "OK":
            raise RuntimeError(f"选择邮箱失败：{data}")
        return int(data[0])

    def list_uids(self, criteria: str = "ALL") -> list[bytes]:
        assert self._conn is not None
        typ, data = self._conn.uid("SEARCH", None, criteria)
        if typ != "OK" or not data or not data[0]:
            return []
        return data[0].split()

    def fetch(self, uid: bytes) -> MailMsg | None:
        assert self._conn is not None
        typ, data = self._conn.uid("FETCH", uid, "(RFC822)")
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
        text, htm = _body(parsed)
        to_raw = _dec(parsed.get("To", ""))
        to_list = [x.strip() for x in to_raw.split(",") if x.strip()]
        dt_raw = parsed.get("Date", "")
        dt = None
        if dt_raw:
            try:
                dt = email.utils.parsedate_to_datetime(dt_raw)
            except Exception:
                dt = None
        return MailMsg(
            uid=uid.decode() if isinstance(uid, bytes) else str(uid),
            subject=_dec(parsed.get("Subject", "")),
            sender=_dec(parsed.get("From", "")),
            to=to_list,
            body_text=text,
            body_html=htm,
            received_at=dt,
        )

    def wait_confirm(self, timeout: int = 300, poll: int = 6,
                     hosts: tuple[str, ...] = ("qoder.com", "qoder.sh"),
                     skip_uids: set[str] | None = None) -> str | None:
        """等一封含确认链接的邮件。"""
        skip_uids = skip_uids or set()
        deadline = time.time() + timeout
        with self:
            self.select("INBOX")
            baseline = {u.decode() if isinstance(u, bytes) else str(u)
                        for u in self.list_uids()}
            seen = set(baseline) | skip_uids
            while time.time() < deadline:
                for u in self.list_uids():
                    key = u.decode() if isinstance(u, bytes) else str(u)
                    if key in seen:
                        continue
                    seen.add(key)
                    msg = self.fetch(u)
                    if msg is None:
                        continue
                    link = msg.confirm_link(hosts)
                    if link:
                        return link
                time.sleep(poll)
        return None


# ============================================================ 便捷
def check_account(acc: MailAccount, proxy: str | None = None) -> dict:
    """连通性检查：换 token + IMAP 登录 + 读收件箱数量。"""
    out: dict = {
        "email": acc.email,
        "token_ok": False,
        "imap_ok": False,
        "inbox_count": 0,
        "latest_subject": "",
        "error": "",
    }
    box = OutlookMailbox(acc, proxy=proxy)
    try:
        box.refresh_access_token()
        out["token_ok"] = True
    except Exception as exc:
        out["error"] = f"token: {exc}"
        return out
    try:
        box.connect()
        out["imap_ok"] = True
        count = box.select("INBOX")
        out["inbox_count"] = count
        uids = box.list_uids()
        if uids:
            msg = box.fetch(uids[-1])
            if msg:
                out["latest_subject"] = msg.subject[:60]
    except Exception as exc:
        out["error"] = f"imap: {exc}"
    finally:
        box.close()
    return out
