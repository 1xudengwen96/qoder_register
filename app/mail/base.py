"""邮箱源统一抽象。

下游注册器 / 验证器只依赖这里的 Protocol，切换 Outlook / Hotmail / iCloud
不改业务代码，只改配置（见 config/config.example.yaml 的 mail 段）。
"""

from __future__ import annotations

import dataclasses
import html
import re
from datetime import datetime
from typing import Callable, Protocol, runtime_checkable

LINK_RE = re.compile(r"""(?:https?://[^\s"'<>\)\]]+)""", re.IGNORECASE)

# 常见确认类邮件的关键词，用于默认匹配器
CONFIRM_KEYWORDS = (
    "confirm", "verify", "verification", "activate", "activation",
    "验证", "确认", "激活", "点击",
)


@dataclasses.dataclass(slots=True)
class MailIdentity:
    """一个可用于注册的邮箱身份。"""

    address: str
    provider: str                       # outlook_tag | hotmail | icloud_relay
    parent_account: str | None = None   # 主号 / Apple ID 主邮箱
    secret: str | None = None           # 该地址登录密码（路线 B 需要）
    imap_host: str | None = None        # 收信服务器
    meta: dict = dataclasses.field(default_factory=dict)


@dataclasses.dataclass(slots=True)
class MailMessage:
    """一封归一化后的邮件。"""

    uid: str
    mailbox: str
    to: list[str]
    sender: str
    subject: str
    body_text: str
    body_html: str
    received_at: datetime | None = None

    def links(self) -> list[str]:
        out: list[str] = []
        for raw in LINK_RE.findall(self.body_text) + LINK_RE.findall(self.body_html):
            link = html.unescape(raw).rstrip(".,;)]>\"'")
            if link not in out:
                out.append(link)
        return out

    def confirm_link(self, extra_hosts: tuple[str, ...] = ()) -> str | None:
        """挑出最像确认链接的一条：优先落在 extra_hosts 内且含确认关键词。"""
        candidates = self.links()
        if not candidates:
            return None

        def score(url: str) -> int:
            s = 0
            low = url.lower()
            if any(h and h in low for h in extra_hosts):
                s += 10
            if any(k in low for k in ("confirm", "verify", "activate", "token", "code")):
                s += 5
            if "unsubscribe" in low or "privacy" in low:
                s -= 10
            return s

        ranked = sorted(candidates, key=score, reverse=True)
        return ranked[0] if ranked else None


@dataclasses.dataclass(slots=True)
class ProviderHealth:
    name: str
    remaining: int
    total: int
    ok: bool
    detail: str = ""

    def ratio(self) -> float:
        return 0.0 if self.total <= 0 else self.remaining / self.total


@runtime_checkable
class MailProvider(Protocol):
    name: str

    def acquire(self, n: int = 1) -> list[MailIdentity]:
        """从池里取 n 个可用地址。"""

    def wait_for_mail(
        self,
        ident: MailIdentity,
        timeout: int = 600,
        match: Callable[[MailMessage], bool] | None = None,
        poll_interval: int = 8,
    ) -> MailMessage | None:
        """阻塞等待匹配邮件；超时返回 None。"""

    def health(self) -> ProviderHealth:
        """配额与健康状态，供 mail:pool 展示。"""


def default_match(ident: MailIdentity) -> Callable[[MailMessage], bool]:
    """默认匹配：收件人是该地址，或主题带确认关键词。"""

    target = ident.address.lower()

    def _m(msg: MailMessage) -> bool:
        tos = [t.lower() for t in msg.to]
        if any(target in t for t in tos):
            return True
        subj = msg.subject.lower()
        return any(k in subj for k in CONFIRM_KEYWORDS)

    return _m
