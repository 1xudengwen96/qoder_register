"""路线 B：Hotmail / Outlook 独立主号池。

每个地址是独立主地址，不走 +tag，因此不存在归一化撞车。
凭证来源：``data/hotmail_accounts.csv``，列 ``email,password,imap_host[,region]``。
"""

from __future__ import annotations

import csv
from pathlib import Path
from typing import Callable, Iterable

from app.mail.base import MailIdentity, MailMessage, ProviderHealth, default_match
from app.mail.imap_common import ImapMailbox, wait_in_mailbox

DEFAULT_IMAP = "outlook.office365.com"
HOTMAIL_HINTS = ("@hotmail.", "@outlook.", "@live.", "@msn.")


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
                yield {
                    "email": addr,
                    "password": (row.get("password") or "").strip(),
                    "imap_host": (row.get("imap_host") or self.imap_host_default).strip(),
                    "region": (row.get("region") or "").strip() or None,
                }

    # ---- pool -------------------------------------------------------------
    def acquire(self, n: int = 1) -> list[MailIdentity]:
        out: list[MailIdentity] = []
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
                    meta={"region": row.get("region")},
                )
            )
        return out

    # ---- receive ----------------------------------------------------------
    def wait_for_mail(
        self,
        ident: MailIdentity,
        timeout: int = 600,
        match: Callable[[MailMessage], bool] | None = None,
        poll_interval: int = 8,
    ) -> MailMessage | None:
        match = match or default_match(ident)
        host = ident.imap_host or self.imap_host_default
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
