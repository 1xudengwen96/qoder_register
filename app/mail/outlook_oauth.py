"""Standalone Outlook OAuth2/IMAP reader.

imaplib.authenticate() Base64-encodes callback bytes itself: supply the RAW
XOAUTH2 SASL payload, never an already Base64-encoded value. Use the public
API so tagged responses and the AUTH state are managed by imaplib.

The optional proxy configures HTTPS token requests only, not the IMAP TCP
connection. Mail reads use BODY.PEEK[] and mailbox selection is read-only.
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


def _split_proxy(proxy: str) -> tuple[str, int]:
    """``http://user:pass@host:port`` → (host, port)。"""
    raw = proxy.strip()
    for scheme in ("http://", "https://", "socks5://"):
        if raw.startswith(scheme):
            raw = raw[len(scheme):]
    raw = raw.split("@")[-1]
    host, _, port = raw.partition(":")
    return host or "127.0.0.1", int(port or 8080)


def _ipv4_of(host: str, port: int) -> str:
    """只取 A 记录。微软的 IPv6 段（2603:1016::/32）在本机关 TUN 后 TLS 握手超时。"""
    try:
        infos = socket.getaddrinfo(host, port, socket.AF_INET,
                                  socket.SOCK_STREAM)
    except OSError:
        return host
    for info in infos:
        return info[4][0]
    return host


class ProxiedIMAP4SSL(imaplib.IMAP4_SSL):
    """强制 IPv4（可选走 HTTP CONNECT 隧道）的 IMAP4_SSL。

    为什么要动 socket 层：
    1. ``imaplib`` 不认 ``proxies``，永远直连。
    2. 本机 DNS 优先返回 IPv6，``outlook.office365.com`` 的 AAAA
       （2603:1016:...）TCP 能连上但 **TLS 握手必超时**；关闭 TUN 后 IPv6
       路径就是断的。实测直连/隧道都是 ``handshake operation timed out``。
    3. 所以这里一律解析 A 记录、按 IPv4 建立连接；隧道模式下把 IPv4 直接
       写进 CONNECT 目标，避免代理端又去走 IPv6，SNI 仍用域名。

    只覆盖 ``_create_socket()``，imaplib 的状态机与 XOAUTH2 手工认证逻辑不动。
    """

    def __init__(self, host: str, port: int = 993, *,
                 proxy: str = "", ssl_context=None, timeout: int = 60) -> None:
        self._proxy = proxy or ""
        if self._proxy:
            self._proxy_host, self._proxy_port = _split_proxy(self._proxy)
        super().__init__(host, port, ssl_context=ssl_context, timeout=timeout)

    def _create_socket(self, timeout):          # noqa: D102
        """建连。微软侧对同一出口的连接是抖的（前一次 0.5s 成功、下一次握手超时），
        所以这里自己重试 3 轮，每轮都重新 CONNECT，别把抖动交给上层。"""
        last: Exception | None = None
        for attempt in range(1, 4):
            try:
                return self._connect_once(timeout)
            except (OSError, TimeoutError) as exc:
                last = exc
                time.sleep(0.8 * attempt)
        raise last if last else OSError("IMAP 建连失败")

    def _connect_once(self, timeout):
        limit = timeout or 60
        if self._proxy:
            # 走代理用**域名**：让代理端自己解析。
            # 实测本机解析出的 40.104.x.x 在隧道里必被 EOF，而域名目标
            # 直接 TLSv1.3 成功（代理端解析到的是可用节点）。
            target = f"{self.host}:{self.port}"
            raw = socket.create_connection(
                (self._proxy_host, self._proxy_port), timeout=limit)
            raw.settimeout(limit)
            req = (f"CONNECT {target} HTTP/1.1\r\n"
                   f"Host: {target}\r\n"
                   f"Proxy-Connection: keep-alive\r\n\r\n")
            raw.sendall(req.encode())
            buf = b""
            while b"\r\n\r\n" not in buf:
                chunk = raw.recv(4096)
                if not chunk:
                    raw.close()
                    raise OSError(f"代理隧道被关闭（{target}）")
                buf += chunk
            status = buf.split(b"\r\n", 1)[0]
            if b" 200" not in status:
                raw.close()
                raise OSError(f"代理隧道失败 {target}: {status!r}")
            return self.ssl_context.wrap_socket(raw, server_hostname=self.host)

        # 直连：IPv6 到微软 TLS 必挂，只能强制 IPv4
        ipv4 = _ipv4_of(self.host, self.port)
        raw = socket.create_connection((ipv4, self.port), timeout=limit)
        raw.settimeout(limit)
        return self.ssl_context.wrap_socket(raw, server_hostname=self.host)



# ============================================================ 数据类
@dataclasses.dataclass(slots=True)
class MailAccount:
    email: str
    password: str = dataclasses.field(default="", repr=False)
    client_id: str = ""
    refresh_token: str = dataclasses.field(default="", repr=False)
    recovery_email: str = ""
    recovery_password: str = dataclasses.field(default="", repr=False)
    extra: dict = dataclasses.field(default_factory=dict)

    @property
    def has_oauth(self) -> bool:
        return bool(self.client_id and self.refresh_token)


@dataclasses.dataclass(slots=True)
class TokenBundle:
    access_token: str = dataclasses.field(repr=False)
    refresh_token: str = dataclasses.field(default="", repr=False)
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
        try:
            r = requests.post(TOKEN_URL, data=data, proxies=proxies, timeout=self.timeout)
            j = r.json()
        except (requests.RequestException, ValueError):
            raise RuntimeError("OAuth token endpoint transport/JSON failure") from None
        if not isinstance(j, dict):
            raise RuntimeError("OAuth token endpoint returned invalid response")
        if "access_token" not in j:
            raise RuntimeError(
                f"OAuth token rejected (HTTP {r.status_code})")
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
        """Supply RAW SASL bytes; imaplib handles Base64 and AUTH state."""
        if self.token is None or not self.token.access_token:
            raise RuntimeError("OAuth access token is missing")
        user, token = self.acc.email, self.token.access_token
        if any(c in user + token for c in ("\r", "\n", "\x01")):
            raise ValueError("Invalid control character in OAuth credentials")
        raw = f"user={user}\x01auth=Bearer {token}\x01\x01".encode("utf-8")
        sent = False

        def response(challenge: bytes) -> bytes:
            nonlocal sent
            if sent:
                return b""
            sent = True
            return raw

        try:
            conn.authenticate("XOAUTH2", response)
        except imaplib.IMAP4.error:
            raise RuntimeError("IMAP XOAUTH2 authentication rejected") from None

    def connect(self) -> "OutlookMailbox":
        if self.token is None or self.token.expired:
            self.refresh_access_token()
        # 隧道优先（代理端解析域名，实测 0.3s 建好）；没代理或隧道失败再直连 IPv4
        try:
            if self.proxy:
                conn = ProxiedIMAP4SSL(self.host, 993, proxy=self.proxy,
                                       timeout=self.timeout)
            else:
                conn = ProxiedIMAP4SSL(self.host, 993, proxy="",
                                       timeout=self.timeout)
        except Exception:
            if not self.proxy:
                raise
            conn = ProxiedIMAP4SSL(self.host, 993, proxy="",
                                   timeout=self.timeout)
        try:
            self._xoauth2(conn)
        except Exception:
            try:
                conn.logout()
            except Exception:
                conn.shutdown()
            raise
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
        typ, data = self._conn.select(box, readonly=True)
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
        typ, data = self._conn.uid("FETCH", uid, "(BODY.PEEK[])")
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
        count = box.select("INBOX")
        out["inbox_count"] = count
        uids = box.list_uids()
        out["imap_ok"] = True
        if uids:
            msg = box.fetch(uids[-1])
            if msg:
                out["latest_subject"] = msg.subject[:60]
    except Exception as exc:
        out["error"] = f"imap: {exc}"
    finally:
        box.close()
    return out
