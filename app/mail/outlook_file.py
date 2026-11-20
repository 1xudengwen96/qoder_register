"""路线 D：Outlook 账号清单文件（``data/emails.txt``）。

文件格式（一行一个账号，字段用 ``----`` 分隔，与 ``tools/run_signup_cdp.py`` 一致）：

    email----password----clientId----refreshToken
    email----password----clientId----refreshToken----recoveryEmail----recoveryPassword

只支持 OAuth2 收信（``clientId`` + ``refreshToken`` 必须都有），
因为 Outlook 已经关掉了 IMAP 基础认证。缺 refreshToken 的行会被标成
``no_oauth``，仍在池里但 ``health()`` 会把它算成不可用。

为什么单独一个 provider：
- 清单是用户手工维护的，随时会改，所以每次 ``acquire`` 都重读文件（带 mtime 缓存）；
- 地址来自不同主号，不存在 ``+tag`` 归一化问题，比路线 A 干净；
- 收到验证码后由 ``otp_source`` 直接取用，不走数据库也能跑。
"""

from __future__ import annotations

import dataclasses
import logging
import threading
import time
from pathlib import Path
from typing import Callable

from app.mail.base import MailIdentity, MailMessage, ProviderHealth, default_match

OUTLOOK_IMAP = "outlook.office365.com"
log = logging.getLogger("mail.file")


class OutlookFileProvider:
    name = "outlook_file"

    def __init__(
        self,
        path: str | Path,
        taken_addresses: set[str] | None = None,
        imap_proxy: str | None = None,
        scan_folders: tuple[str, ...] = ("INBOX", "Junk"),
    ) -> None:
        self.path = Path(path)
        self.taken = {a.lower() for a in (taken_addresses or set())}
        self.imap_proxy = imap_proxy
        self.scan_folders = scan_folders
        self._accounts = []
        self._mtime = -1.0
        self._used: set[str] = set()
        self._lock = threading.Lock()   # 并发 worker 共享本 provider，_used/重读清单必须互斥
        self._load(force=True)

    # ---------------------------------------------------------- 清单
    def _load(self, force: bool = False) -> None:
        from app.mail.outlook_oauth import load_accounts

        if not self.path.exists():
            self._accounts = []
            return
        mtime = self.path.stat().st_mtime
        if not force and mtime == self._mtime:
            return
        self._accounts = load_accounts(self.path)
        self._mtime = mtime
        log.debug("邮箱清单 %s 载入 %d 条", self.path, len(self._accounts))

    def refresh(self) -> int:
        self._load(force=True)
        return len(self._accounts)

    def _account_for(self, address: str):
        target = address.strip().lower()
        self._load()
        for a in self._accounts:
            if a.email.strip().lower() == target:
                return a
        return None

    # ---------------------------------------------------------- 取地址
    def acquire(self, n: int = 1) -> list[MailIdentity]:
        out: list[MailIdentity] = []
        with self._lock:
            self._load()
            for a in self._accounts:
                if len(out) >= n:
                    break
                addr = a.email.strip().lower()
                if not addr or addr in self.taken or addr in self._used:
                    continue
                self._used.add(addr)
                out.append(MailIdentity(
                    address=a.email.strip(),
                    provider=self.name,
                    parent_account=a.email.strip(),
                    secret=a.password or None,
                    imap_host=OUTLOOK_IMAP,
                    meta={
                        "client_id": a.client_id,
                        "refresh_token": a.refresh_token,
                        "has_oauth": a.has_oauth,
                        "source": str(self.path),
                    },
                ))
        return out

    # ---------------------------------------------------------- 收信
    def wait_for_mail(
        self,
        ident: MailIdentity,
        timeout: int = 600,
        match: Callable[[MailMessage], bool] | None = None,
        poll_interval: int = 8,
    ) -> MailMessage | None:
        from app.mail.outlook_oauth import OutlookMailbox

        acc = self._account_for(ident.address)
        if acc is None or not acc.has_oauth:
            log.warning("清单里没有可用的 OAuth 账号：%s", ident.address)
            return None
        match = match or default_match(ident)
        box = OutlookMailbox(acc, host=ident.imap_host or OUTLOOK_IMAP,
                             proxy=self.imap_proxy)
        deadline = time.time() + timeout
        baseline: set[str] = set()
        try:
            box.refresh_access_token()
            box.connect()
            box.select("INBOX")
            baseline = {u.decode() if isinstance(u, bytes) else str(u)
                        for u in box.list_uids()}
            while time.time() < deadline:
                for folder in self.scan_folders:
                    try:
                        box.select(folder)
                        uids = box.list_uids()
                    except Exception:  # noqa: BLE001
                        continue
                    is_inbox = folder.upper().startswith("INBOX")
                    for u in (uids if is_inbox else uids[-30:]):
                        key = u.decode() if isinstance(u, bytes) else str(u)
                        if is_inbox and key in baseline:
                            continue
                        try:
                            msg = box.fetch(u)
                        except Exception:  # noqa: BLE001
                            continue
                        if msg is None:
                            continue
                        if is_inbox:
                            baseline.add(key)
                        norm = _to_message(msg, folder)
                        if match(norm):
                            return norm
                time.sleep(poll_interval)
        except Exception as exc:  # noqa: BLE001
            log.warning("收信失败 %s：%s: %s", ident.address,
                        type(exc).__name__, exc)
        finally:
            try:
                box.close()
            except Exception:  # noqa: BLE001
                pass
        return None

    # ---------------------------------------------------------- 健康
    def health(self) -> ProviderHealth:
        self._load()
        total = len(self._accounts)
        oauth = [a for a in self._accounts if a.has_oauth]
        remaining = sum(
            1 for a in oauth
            if a.email.strip().lower() not in self.taken
            and a.email.strip().lower() not in self._used
        )
        no_oauth = total - len(oauth)
        detail = f"清单 {self.path.name}：{total} 条，OAuth 可用 {len(oauth)} 条"
        if no_oauth:
            detail += f"，缺 refreshToken {no_oauth} 条"
        return ProviderHealth(
            name=self.name,
            remaining=remaining,
            total=total,
            ok=remaining > 0,
            detail=detail,
        )


def _to_message(msg, mailbox: str) -> MailMessage:
    """把 outlook_oauth.MailMsg 转成统一的 MailMessage。"""
    return MailMessage(
        uid=msg.uid,
        mailbox=mailbox,
        to=list(msg.to or []),
        sender=msg.sender,
        subject=msg.subject,
        body_text=msg.body_text,
        body_html=msg.body_html,
        received_at=msg.received_at,
    )


# ------------------------------------------------------------ 清单体检
@dataclasses.dataclass(slots=True)
class FileReport:
    path: str
    total: int
    usable: int
    no_oauth: int
    duplicates: list[str]
    bad_lines: list[int]
    addresses: list[str]

    def lines(self) -> list[str]:
        out = [
            f"文件      : {self.path}",
            f"账号      : {self.total} 条（可用 {self.usable}，缺 refreshToken {self.no_oauth}）",
        ]
        if self.duplicates:
            out.append(f"重复地址  : {', '.join(self.duplicates)}")
        if self.bad_lines:
            out.append(f"格式错误行: {', '.join(map(str, self.bad_lines))}")
        return out


def audit(path: str | Path) -> FileReport:
    """体检清单文件：重复 / 缺字段 / 格式错行。"""
    from app.mail.outlook_oauth import parse_line

    p = Path(path)
    if not p.exists():
        return FileReport(str(p), 0, 0, 0, [], [], [])
    seen: dict[str, int] = {}
    dupes: list[str] = []
    bad: list[int] = []
    usable = no_oauth = 0
    addrs: list[str] = []
    for i, raw in enumerate(p.read_text(encoding="utf-8", errors="ignore").splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        acc = parse_line(line)
        if acc is None:
            bad.append(i)
            continue
        addr = acc.email.strip().lower()
        addrs.append(acc.email.strip())
        if addr in seen:
            dupes.append(acc.email.strip())
        seen[addr] = i
        if acc.has_oauth:
            usable += 1
        else:
            no_oauth += 1
    return FileReport(str(p), len(addrs), usable, no_oauth,
                      sorted(set(dupes)), bad, addrs)
