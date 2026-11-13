"""路线 A：Outlook 主号 + ``+tag`` 子地址。

原理：``main1+q007@outlook.com`` 的信直接落在 ``main1@outlook.com`` 收件箱，
收信只需登录主号。
风险：Qoder 若对 ``+tag`` 做去加号归一化，一个主号只能算一个身份。
框架对此的处置见 ``tag_risk_probe()``：首轮用 2 个 tag 试注册，若第二个报
"邮箱已注册"，直接给主号打 retired，调度器转向路线 B/C。
"""

from __future__ import annotations

import re
import string
from dataclasses import dataclass
from typing import Callable

from app.mail.base import MailIdentity, MailMessage, ProviderHealth, default_match
from app.mail.imap_common import ImapMailbox, wait_in_mailbox

OUTLOOK_IMAP = "outlook.office365.com"
TAG_ALPHABET = string.ascii_lowercase + string.digits


@dataclass(slots=True)
class OutlookMainAccount:
    address: str
    password: str
    imap_host: str = OUTLOOK_IMAP
    tags_used: int = 0
    retired: bool = False

    @property
    def local(self) -> str:
        return self.address.split("@", 1)[0]


def split_address(address: str) -> tuple[str, str]:
    """``a+b@x.com`` -> (``a``, ``b``)"""
    local, _, domain = address.partition("@")
    base, _, tag = local.partition("+")
    return base, tag


def strip_tag(address: str) -> str:
    base, tag = split_address(address)
    domain = address.split("@", 1)[1] if "@" in address else ""
    return f"{base}@{domain}"


class OutlookTagProvider:
    name = "outlook_tag"

    def __init__(
        self,
        main_accounts: list[dict],
        tag_prefix: str = "q",
        max_tags_per_account: int = 8,
        taken_addresses: set[str] | None = None,
    ) -> None:
        self.mains = [
            OutlookMainAccount(
                address=m["user"],
                password=m.get("password", ""),
                imap_host=m.get("imap", OUTLOOK_IMAP),
            )
            for m in main_accounts
        ]
        self.tag_prefix = tag_prefix
        self.max_tags = max_tags_per_account
        self.taken = {a.lower() for a in (taken_addresses or set())}

    # ---- pool -------------------------------------------------------------
    def _next_tag(self, main: OutlookMainAccount) -> str | None:
        for _ in range(main.tags_used, self.max_tags + 1):
            tag = f"{self.tag_prefix}{main.tags_used:03d}"
            main.tags_used += 1
            addr = f"{main.local}+{tag}@{main.address.split('@', 1)[1]}".lower()
            if addr not in self.taken:
                return addr
        return None

    def acquire(self, n: int = 1) -> list[MailIdentity]:
        out: list[MailIdentity] = []
        for main in self.mains:
            if main.retired:
                continue
            while len(out) < n and main.tags_used <= self.max_tags:
                addr = self._next_tag(main)
                if addr is None:
                    break
                self.taken.add(addr)
                out.append(
                    MailIdentity(
                        address=addr,
                        provider=self.name,
                        parent_account=main.address,
                        secret=main.password,
                        imap_host=main.imap_host,
                        meta={"tag": split_address(addr)[1]},
                    )
                )
            if len(out) >= n:
                break
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
        box = ImapMailbox(
            host=ident.imap_host or OUTLOOK_IMAP,
            user=ident.parent_account or strip_tag(ident.address),
            password=ident.secret or "",
        )
        # 先取当前最大 uid，只等之后的新信
        with ImapMailbox(
            host=ident.imap_host or OUTLOOK_IMAP,
            user=ident.parent_account or strip_tag(ident.address),
            password=ident.secret or "",
        ) as probe:
            since = probe.max_uid()
        return wait_in_mailbox(box, since, timeout, match, poll_interval)

    # ---- health -----------------------------------------------------------
    def health(self) -> ProviderHealth:
        total = len(self.mains) * (self.max_tags + 1)
        remaining = sum(
            max(0, self.max_tags + 1 - m.tags_used) for m in self.mains if not m.retired
        )
        return ProviderHealth(
            name=self.name,
            remaining=remaining,
            total=total,
            ok=remaining > 0,
            detail=f"主号 {len([m for m in self.mains if not m.retired])} 个可用",
        )

    # ---- 归一化探测 --------------------------------------------------------
    def retire_main(self, main_address: str) -> None:
        for m in self.mains:
            if m.address.lower() == main_address.lower():
                m.retired = True

    @staticmethod
    def looks_normalized_failure(error_text: str) -> bool:
        """注册返回里出现"该邮箱已注册"类提示，说明 +tag 被归一化。"""
        pat = re.compile(r"(already\s*(registered|exists)|已被注册|已注册|邮箱.*存在)", re.I)
        return bool(pat.search(error_text or ""))
