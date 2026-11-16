"""iCloud+ Hide My Email → 转发聚合（QQ 邮箱 IMAP）收信层。

机制
----
iCloud+ 可以给每个站点生成一个 ``xxxxx@privaterelay.appleid.com``，
所有转发都落到同一个「真实收件邮箱」。这里把这个真实邮箱换成
**QQ 邮箱 IMAP**（`imap.qq.com:993`，用授权码登录），于是：

- 几百个隐藏地址只用一个 IMAP 会话收信；
- 每封信靠**收件人**反查是哪个隐藏地址注册的；
- 验证码 / 确认链接从正文里按规则提取。

为什么要单独一层
----------------
1. QQ 邮箱要登录授权码，且部分服务端在 ``LOGIN`` 后要求 ``ID`` 命令，
   标准 ``imaplib`` 不会自动发；
2. 隐藏地址不一定只出现在 ``To`` —— Apple 转发时可能改写收件头，
   原地址会落在 ``Delivered-To`` / ``X-Original-To`` / ``Return-Path``
   或者正文页脚，所以匹配要扫**全部头 + 正文**；
3. 几百个地址混在一个箱子里，必须做**收件人收窄**再找验证码，
   否则会把别的地址的码认成自己的。
"""

from __future__ import annotations

import email
import imaplib
import re
import time
from dataclasses import dataclass, field
from email.header import decode_header, make_header
from pathlib import Path
from typing import Callable

from app.mail.base import MailIdentity, MailMessage, ProviderHealth
from app.mail.imap_common import _body_of, _decode, _strip_html

RELAY_SUFFIX = "@privaterelay.appleid.com"
ADDR_RE = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")
DEFAULT_CODE_RE = r"\b(\d{6})\b"

# 这些头里都可能藏着「原始收件地址」
RECIPIENT_HEADERS = (
    "To", "Cc", "Delivered-To", "X-Original-To", "Envelope-To",
    "X-Envelope-To", "Apparently-To", "Return-Path", "X-Apple-Original-To",
    "Reply-To",
)


# ============================================================ 配置
@dataclass(slots=True)
class RelayMatchRules:
    """匹配规则（全部来自配置，方便按邮件服务商微调）。"""

    sender_allow: tuple[str, ...] = ("qoder.com",)
    subject_contains: tuple[str, ...] = ("qoder",)
    code_regex: str = DEFAULT_CODE_RE
    scan_body_for_recipient: bool = True
    lookback: int = 60                  # 每次轮询只看最近 N 封
    extra_recipient_headers: tuple[str, ...] = ()

    @classmethod
    def from_config(cls, d: dict | None) -> "RelayMatchRules":
        d = d or {}
        def _tup(key, default):
            val = d.get(key, default)
            if isinstance(val, str):
                return tuple(x.strip() for x in val.split(",") if x.strip())
            return tuple(val or default)
        return cls(
            sender_allow=_tup("sender_allow", ("qoder.com",)),
            subject_contains=_tup("subject_contains", ("qoder",)),
            code_regex=d.get("code_regex") or DEFAULT_CODE_RE,
            scan_body_for_recipient=bool(d.get("scan_body_for_recipient", True)),
            lookback=int(d.get("lookback", 60) or 60),
            extra_recipient_headers=_tup("extra_recipient_headers", ()),
        )


# ============================================================ 收件人解析
def recipient_candidates(msg: MailMessage, raw_headers: str = "",
                         rules: RelayMatchRules | None = None) -> set[str]:
    """一封聚合邮件里所有「可能的原始收件地址」。"""
    rules = rules or RelayMatchRules()
    blob_parts = [" ".join(msg.to), raw_headers]
    if rules.scan_body_for_recipient:
        blob_parts.extend([msg.body_text or "", msg.body_html or ""])
    blob = " ".join(blob_parts)
    found = {a.lower() for a in ADDR_RE.findall(blob)}
    return found


def is_relay_address(addr: str) -> bool:
    return addr.lower().endswith(RELAY_SUFFIX)


# ============================================================ IMAP 会话
class RelayInbox:
    """QQ 邮箱（或任何标准 IMAP）聚合收件箱。"""

    def __init__(self, host: str, user: str, password: str,
                 port: int = 993, folder: str = "INBOX",
                 timeout: int = 30, imap_id: str = "") -> None:
        self.host = host
        self.user = user
        self.password = password
        self.port = int(port or 993)
        self.folder = folder or "INBOX"
        self.timeout = timeout
        self.imap_id = imap_id or '("name" "qoder-register" "version" "1.0.0")'
        self._conn: imaplib.IMAP4_SSL | None = None

    # ---------------------------------------------------------- 连接
    def connect(self) -> "RelayInbox":
        self._conn = imaplib.IMAP4_SSL(self.host, self.port,
                                       timeout=self.timeout)
        self._conn.login(self.user, self.password)
        self._send_id()
        typ, _ = self._conn.select(self.folder)
        if typ != "OK":
            raise RuntimeError(f"打不开文件夹 {self.folder}")
        return self

    def _send_id(self) -> None:
        """QQ / 163 等要求在 LOGIN 后发 ID，否则 SELECT 可能被拒。"""
        try:
            self._conn._simple_command("ID", self.imap_id)
            self._conn._untagged_response("OK", [None], "ID")
        except Exception:
            pass

    def close(self) -> None:
        if not self._conn:
            return
        for fn in ("close", "logout"):
            try:
                getattr(self._conn, fn)()
            except Exception:
                pass
        self._conn = None

    def __enter__(self) -> "RelayInbox":
        return self.connect()

    def __exit__(self, *exc) -> None:
        self.close()

    # ---------------------------------------------------------- 取信
    def uid_list(self) -> list[int]:
        assert self._conn is not None
        typ, data = self._conn.uid("SEARCH", None, "ALL")
        if typ != "OK" or not data or not data[0]:
            return []
        return [int(x) for x in data[0].split() if x.isdigit()]

    def max_uid(self) -> int:
        uids = self.uid_list()
        return max(uids) if uids else 0

    def fetch(self, uid: int) -> MailMessage | None:
        assert self._conn is not None
        typ, data = self._conn.uid("FETCH", str(uid), "(RFC822)")
        if typ != "OK" or not data:
            return None
        raw = next((p[1] for p in data
                    if isinstance(p, tuple) and len(p) >= 2), None)
        if raw is None:
            return None
        parsed = email.message_from_bytes(raw)
        text, html = _body_of(parsed)

        to_list: list[str] = []
        header_blob: list[str] = []
        for name in RECIPIENT_HEADERS + ("From", "Subject"):
            vals = parsed.get_all(name) or []
            for v in vals:
                header_blob.append(_decode(v))
        to_raw = _decode(parsed.get("To", ""))
        to_list = [a.strip() for a in to_raw.split(",") if a.strip()]
        for name in ("Delivered-To", "X-Original-To", "Envelope-To"):
            v = _decode(parsed.get(name, ""))
            if v:
                to_list.append(v)

        msg = MailMessage(
            uid=str(uid),
            mailbox=self.folder,
            to=to_list,
            sender=_decode(parsed.get("From", "")),
            subject=_decode(parsed.get("Subject", "")),
            body_text=text or _strip_html(html),
            body_html=html,
            received_at=None,
            # 原文头单独留一份，供收件人匹配用（不塞进 to，避免污染语义）
            raw_headers="\n".join(header_blob),
        )
        return msg

    def recent(self, limit: int = 60) -> list[MailMessage]:
        uids = self.uid_list()[-limit:]
        out: list[MailMessage] = []
        for u in uids:
            msg = self.fetch(u)
            if msg is not None:
                out.append(msg)
        return out


# ============================================================ 判定
def guess_code(msg: MailMessage, rules: RelayMatchRules) -> str:
    try:
        pattern = re.compile(rules.code_regex)
    except re.error:
        pattern = re.compile(DEFAULT_CODE_RE)
    for blob in (msg.body_text or "", msg.body_html or "",
                 msg.subject or ""):
        m = pattern.search(blob)
        if m:
            return m.group(1) if m.groups() else m.group(0)
    return ""


def looks_like_code_mail(msg: MailMessage, rules: RelayMatchRules) -> bool:
    sender = (msg.sender or "").lower()
    if rules.sender_allow and not any(d in sender for d in rules.sender_allow):
        return False
    if rules.subject_contains:
        subject = (msg.subject or "").lower()
        body = (msg.body_text or "").lower()
        if not any(k in subject or k in body for k in rules.subject_contains):
            return False
    return bool(guess_code(msg, rules))


def recipient_hits(msg: MailMessage, address: str,
                   rules: RelayMatchRules) -> bool:
    target = address.lower()
    return target in recipient_candidates(msg, msg.raw_headers or "", rules)


# ============================================================ Provider
@dataclass(slots=True)
class RelayPool:
    """隐藏地址池：文件里一行一个，支持 ``#`` 注释与行内标签。"""

    path: Path
    addresses: list[str] = field(default_factory=list)

    def load(self) -> list[str]:
        if not self.path.exists():
            return []
        out: list[str] = []
        for line in self.path.read_text(encoding="utf-8").splitlines():
            line = line.split("#", 1)[0].strip()
            if not line:
                continue
            m = ADDR_RE.search(line)
            if m:
                addr = m.group(0)
            else:
                # 允许只写用户名（iCloud 导出的就是一串随机串）
                addr = line.split()[0]
            if "@" not in addr:
                addr = addr + RELAY_SUFFIX
            if addr.lower() not in {a.lower() for a in out}:
                out.append(addr)
        self.addresses = out
        return out


class ICloudRelayQQProvider:
    """路线 C（QQ 聚合版）：iCloud 隐藏地址池 + QQ IMAP 收码。"""

    name = "icloud_relay"

    def __init__(self, relay_addresses_file: str | Path,
                 imap_host: str = "imap.qq.com", imap_port: int = 993,
                 imap_user: str = "", imap_password: str = "",
                 folder: str = "INBOX", quota_total: int = 500,
                 taken_addresses: set[str] | None = None,
                 rules: RelayMatchRules | None = None,
                 imap_id: str = "", timeout: int = 30) -> None:
        self.pool = RelayPool(Path(relay_addresses_file))
        self.pool.load()
        self.imap_host = imap_host
        self.imap_port = int(imap_port or 993)
        self.imap_user = imap_user
        self.imap_password = imap_password
        self.folder = folder or "INBOX"
        self.quota_total = int(quota_total or 500)
        self.rules = rules or RelayMatchRules()
        self.imap_id = imap_id
        self.timeout = timeout
        self.taken = {a.lower() for a in (taken_addresses or set())}
        self.cursor = 0

    # ---------------------------------------------------------- 池
    def acquire(self, n: int = 1) -> list[MailIdentity]:
        out: list[MailIdentity] = []
        while self.cursor < len(self.pool.addresses) and len(out) < n:
            addr = self.pool.addresses[self.cursor]
            self.cursor += 1
            if addr.lower() in self.taken:
                continue
            self.taken.add(addr.lower())
            out.append(MailIdentity(
                address=addr, provider=self.name,
                parent_account=self.imap_user, secret=None,
                imap_host=self.imap_host,
                meta={"relay": True, "aggregator": self.imap_user}))
        return out

    def identities(self, limit: int = 0) -> list[str]:
        return self.pool.addresses[:limit] if limit else list(self.pool.addresses)

    # ---------------------------------------------------------- 收信
    def inbox(self) -> RelayInbox:
        return RelayInbox(self.imap_host, self.imap_user, self.imap_password,
                          port=self.imap_port, folder=self.folder,
                          timeout=self.timeout, imap_id=self.imap_id)

    def find_code(self, address: str) -> str:
        """在整个聚合箱里找这个隐藏地址的验证码。"""
        with self.inbox() as box:
            for msg in reversed(box.recent(self.rules.lookback)):
                if not recipient_hits(msg, address, self.rules):
                    continue
                if not looks_like_code_mail(msg, self.rules):
                    continue
                code = guess_code(msg, self.rules)
                if code:
                    return code
        return ""

    def wait_for_mail(self, ident: MailIdentity, timeout: int = 600,
                      match: Callable[[MailMessage], bool] | None = None,
                      poll_interval: int = 8) -> MailMessage | None:
        """阻塞等待该地址的验证码邮件，返回 MailMessage（正文含码）。"""
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                with self.inbox() as box:
                    for msg in reversed(box.recent(self.rules.lookback)):
                        if not recipient_hits(msg, ident.address, self.rules):
                            continue
                        if match is not None and not match(msg):
                            continue
                        if looks_like_code_mail(msg, self.rules):
                            return msg
            except Exception:
                pass
            time.sleep(poll_interval)
        return None

    def wait_code(self, ident: MailIdentity, timeout: int = 600,
                  poll_interval: int = 8) -> str:
        msg = self.wait_for_mail(ident, timeout=timeout,
                                 poll_interval=poll_interval)
        if msg is None:
            return ""
        return guess_code(msg, self.rules)

    # ---------------------------------------------------------- 健康
    def health(self) -> ProviderHealth:
        total = len(self.pool.addresses)
        remaining = max(0, total - self.cursor)
        cap = min(self.quota_total, total) if total else self.quota_total
        ok = bool(total) and bool(self.imap_user)
        detail = (f"隐藏地址 {total} 个，聚合箱 {self.imap_user or '(未配置)'}"
                  f"@{self.imap_host}:{self.imap_port}/{self.folder}")
        return ProviderHealth(name=self.name, remaining=remaining, total=cap,
                              ok=ok, detail=detail)
