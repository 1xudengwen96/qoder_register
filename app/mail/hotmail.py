"""路线 B：Hotmail / Outlook 独立主号池。

每个地址是独立主地址，不走 +tag，因此不存在归一化撞车。
凭证来源：``data/hotmail_accounts.csv``，列 ``email,password,imap_host[,region]``。
"""

from __future__ import annotations

import csv
import logging
import threading
import time
from pathlib import Path
from typing import Callable, Iterable

from app.mail.base import MailIdentity, MailMessage, ProviderHealth, default_match
from app.mail.imap_common import ImapMailbox, wait_in_mailbox

DEFAULT_IMAP = "outlook.office365.com"
HOTMAIL_HINTS = ("@hotmail.", "@outlook.", "@live.", "@msn.")
log = logging.getLogger("mail.hotmail")


class HotmailProvider:
    name = "hotmail"

    def __init__(
        self,
        credentials_file: str | Path,
        taken_addresses: set[str] | None = None,
        imap_host_default: str = DEFAULT_IMAP,
    ) -> None:
        self.path = Path(credentials_file)
        self.imap_host_default = imap_host_default
        self.taken = {a.lower() for a in (taken_addresses or set())}
        self.cursor = 0
        self._lock = threading.Lock()   # 并发 worker 共享本 provider，cursor/taken 必须互斥
        self.rows: list[dict] = list(self._load())

    def _load(self) -> Iterable[dict]:
        if not self.path.exists():
            return []
        with self.path.open("r", encoding="utf-8-sig", newline="") as fh:
            for row in csv.DictReader(fh):
                addr = (row.get("email") or "").strip()
                if not addr or "@" not in addr:
                    continue
                if not any(h in addr.lower() for h in HOTMAIL_HINTS):
                    # 只收微软域名，其他来源进别的 provider
                    continue
                # webui 导入的 CSV 表头只有 4 列，OAuth 凭证落在匿名超额列
                # （DictReader restkey=None）；命名列表头也兼容。
                extra = row.get(None) or []
                cid = (row.get("client_id")
                       or (extra[0] if len(extra) > 0 else "") or "").strip()
                rtok = (row.get("refresh_token")
                        or (extra[1] if len(extra) > 1 else "") or "").strip()
                yield {
                    "email": addr,
                    "password": (row.get("password") or "").strip(),
                    "imap_host": (row.get("imap_host") or self.imap_host_default).strip(),
                    "region": (row.get("region") or "").strip() or None,
                    "client_id": cid,
                    "refresh_token": rtok,
                }

    # ---- pool -------------------------------------------------------------
    def acquire(self, n: int = 1) -> list[MailIdentity]:
        out: list[MailIdentity] = []
        with self._lock:
            while self.cursor < len(self.rows) and len(out) < n:
                row = self.rows[self.cursor]
                self.cursor += 1
                addr = row["email"].lower()
                if addr in self.taken:
                    continue
                self.taken.add(addr)
                out.append(
                    MailIdentity(
                        address=row["email"],
                        provider=self.name,
                        parent_account=None,
                        secret=row["password"],
                        imap_host=row["imap_host"],
                        meta={"region": row.get("region"),
                              "client_id": row.get("client_id", ""),
                              "refresh_token": row.get("refresh_token", "")},
                    )
                )
        return out

    # ---- receive ----------------------------------------------------------
    def wait_for_mail(
        self,
        ident: MailIdentity,
        timeout: int = 600,
        match: Callable[[MailMessage], bool] | None = None,
        poll_interval: int = 4,
    ) -> MailMessage | None:
        match = match or default_match(ident)
        host = ident.imap_host or self.imap_host_default
        meta = ident.meta or {}
        cid = str(meta.get("client_id") or "")
        rtok = str(meta.get("refresh_token") or "")
        if cid and rtok:
            # Outlook 已禁 Basic 认证：有 OAuth 凭证必须走 XOAUTH2，
            # 同时扫 INBOX+Junk（Qoder 验证邮件实测会进垃圾箱）。
            from app.mail.outlook_oauth import MailAccount, OutlookMailbox
            from app.mail.outlook_file import _to_message

            acc = MailAccount(email=ident.address,
                              password=ident.secret or "",
                              client_id=cid, refresh_token=rtok)
            box = OutlookMailbox(acc, host=host)
            deadline = time.time() + timeout
            baseline: set[str] = set()
            try:
                box.refresh_access_token()
                box.connect()
                box.select("INBOX")
                baseline = {u.decode() if isinstance(u, bytes) else str(u)
                            for u in box.list_uids()}
                while time.time() < deadline:
                    for folder in ("INBOX", "Junk"):
                        try:
                            box.select(folder)
                            uids = box.list_uids()
                        except Exception:  # noqa: BLE001
                            continue
                        is_inbox = folder == "INBOX"
                        for u in (uids if is_inbox else uids[-30:]):
                            key = u.decode() if isinstance(u, bytes) else str(u)
                            if key in baseline:
                                continue
                            try:
                                msg = box.fetch(u)
                            except Exception:  # noqa: BLE001
                                continue
                            if msg is None:
                                continue
                            baseline.add(key)
                            if match(_to_message(msg, folder)):
                                return _to_message(msg, folder)
                    time.sleep(poll_interval)
            except Exception as exc:  # noqa: BLE001
                log.warning("outlook OAuth 收信失败 %s：%s: %s",
                            ident.address, type(exc).__name__, exc)
            finally:
                try:
                    box.close()
                except Exception:  # noqa: BLE001
                    pass
            return None
        with ImapMailbox(host, ident.address, ident.secret or "") as probe:
            since = probe.max_uid()
        box = ImapMailbox(host, ident.address, ident.secret or "")
        return wait_in_mailbox(box, since, timeout, match, poll_interval)

    # ---- health -----------------------------------------------------------
    def health(self) -> ProviderHealth:
        remaining = max(0, len(self.rows) - self.cursor)
        return ProviderHealth(
            name=self.name,
            remaining=remaining,
            total=len(self.rows),
            ok=remaining > 0,
            detail=f"{self.path} 共 {len(self.rows)} 条",
        )
