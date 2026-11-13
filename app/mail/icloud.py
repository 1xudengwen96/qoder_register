"""路线 C：iCloud「隐藏我的邮件」（Hide My Email）统一转发聚合。

机制：iCloud+ 为每个站点生成 ``xxxxx@privaterelay.appleid.com``，全部转发到同一个
Apple ID 主邮箱。所以：
  - 地址清单离线生成/导入（``data/icloud_relay.txt``，一行一个）；
  - 收信只登录**一个聚合邮箱**（把 Apple ID 主邮箱设成 Outlook/QQ 等可 IMAP 的地址），
    按邮件头的 ``To`` / ``Delivered-To`` 反查是哪个隐藏地址收的信；
  - 转发链对发件方 SPF/DMARC 敏感，丢信时该地址记 ``dropped``，不回池。
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Callable

from app.mail.base import MailIdentity, MailMessage, ProviderHealth, default_match
from app.mail.imap_common import ImapMailbox, wait_in_mailbox

RELAY_SUFFIX = "@privaterelay.appleid.com"
APPLE_IMAP = "imap.mail.me.com"
_ADDR_RE = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")


class ICloudRelayProvider:
    name = "icloud_relay"

    def __init__(
        self,
        relay_addresses_file: str | Path,
        aggregator_imap: dict,
        quota_total: int = 100,
        taken_addresses: set[str] | None = None,
        aggregator_mailbox: str = "INBOX",
    ) -> None:
        self.path = Path(relay_addresses_file)
        self.addresses: list[str] = self._load()
        self.agg_user = aggregator_imap.get("user", "")
        self.agg_password = aggregator_imap.get("password", "")
        self.agg_host = aggregator_imap.get("host", APPLE_IMAP)
        self.agg_port = int(aggregator_imap.get("port", 993))
        self.agg_mailbox = aggregator_mailbox
        self.quota_total = quota_total
        self.taken = {a.lower() for a in (taken_addresses or set())}
        self.cursor = 0

    def _load(self) -> list[str]:
        if not self.path.exists():
            return []
        out: list[str] = []
        for line in self.path.read_text(encoding="utf-8").splitlines():
            line = line.split("#", 1)[0].strip()
            if not line:
                continue
            m = _ADDR_RE.search(line)
            if m and m.group(0).lower().endswith(RELAY_SUFFIX):
                out.append(m.group(0))
        return out

    # ---- pool -------------------------------------------------------------
    def acquire(self, n: int = 1) -> list[MailIdentity]:
        out: list[MailIdentity] = []
        while self.cursor < len(self.addresses) and len(out) < n:
            addr = self.addresses[self.cursor]
            self.cursor += 1
            if addr.lower() in self.taken:
                continue
            self.taken.add(addr.lower())
            out.append(
                MailIdentity(
                    address=addr,
                    provider=self.name,
                    parent_account=self.agg_user,
                    secret=None,
                    imap_host=self.agg_host,
                    meta={"relay": True, "aggregator": self.agg_user},
                )
            )
        return out

    # ---- receive ----------------------------------------------------------
    def match_by_recipient(self, ident: MailIdentity) -> Callable[[MailMessage], bool]:
        """聚合邮箱里所有信混在一起，必须先按收件人过滤。"""
        target = ident.address.lower()

        def _m(msg: MailMessage) -> bool:
            if any(target in t.lower() for t in msg.to):
                return True
            # 转发链有时把原地址写进正文
            return target in (msg.body_text + msg.body_html).lower()

        return _m

    def wait_for_mail(
        self,
        ident: MailIdentity,
        timeout: int = 600,
        match: Callable[[MailMessage], bool] | None = None,
        poll_interval: int = 8,
    ) -> MailMessage | None:
        if match is not None:
            def _both(m: MailMessage) -> bool:
                return self.match_by_recipient(ident)(m) and match(m)
            effective = _both
        else:
            base = default_match(ident)
            effective = lambda m: self.match_by_recipient(ident)(m) and base(m)

        with ImapMailbox(
            self.agg_host, self.agg_user, self.agg_password,
            port=self.agg_port, mailbox=self.agg_mailbox,
        ) as probe:
            since = probe.max_uid()

        box = ImapMailbox(
            self.agg_host, self.agg_user, self.agg_password,
            port=self.agg_port, mailbox=self.agg_mailbox,
        )
        return wait_in_mailbox(box, since, timeout, effective, poll_interval)

    # ---- health -----------------------------------------------------------
    def health(self) -> ProviderHealth:
        remaining = max(0, len(self.addresses) - self.cursor)
        cap = min(self.quota_total, len(self.addresses)) if self.addresses else self.quota_total
        detail = f"清单 {len(self.addresses)} 个，配额上限 {self.quota_total}"
        if remaining and remaining / max(cap, 1) < 0.2:
            detail += "（剩余不足 20%，该补地址了）"
        return ProviderHealth(
            name=self.name,
            remaining=remaining,
            total=cap,
            ok=remaining > 0,
            detail=detail,
        )

    # ---- 辅助：从 iCloud 转发邮件里反查真实收件人 --------------------------
    @staticmethod
    def extract_relay_recipients(msg: MailMessage) -> list[str]:
        found = {a.lower() for a in _ADDR_RE.findall(" ".join(msg.to))}
        found |= {
            a.lower()
            for a in _ADDR_RE.findall(msg.body_text + " " + msg.body_html)
            if a.lower().endswith(RELAY_SUFFIX)
        }
        return sorted(found)
