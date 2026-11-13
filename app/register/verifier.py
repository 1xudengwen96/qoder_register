"""验证阶段：等确认邮件 → 抽链接 → 用同源 IP 访问。

为什么必须同源 IP：注册时用的代理出口和点确认时的出口跳洲，是明显的机器特征，
风控侧把这两个事件放在一条时间线上比较，最直接的关联信号就是 IP 归属变化。
"""

from __future__ import annotations

import dataclasses
from typing import Protocol

import requests

from app.mail.base import MailIdentity
from app.net.proxy import Proxy


class ConfirmSource(Protocol):
    def wait_confirm(self, ident: MailIdentity, timeout: int,
                     extra_hosts: tuple[str, ...], poll_interval: int) -> str | None: ...


@dataclasses.dataclass(slots=True)
class VerifyResult:
    ok: bool
    link: str | None = None
    status_code: int | None = None
    error: str = ""


class Verifier:
    def __init__(
        self,
        mail_layer: ConfirmSource,
        timeout: int = 600,
        extra_confirm_hosts: tuple[str, ...] = (),
        poll_interval: int = 8,
        user_agent: str | None = None,
    ) -> None:
        self.mail = mail_layer
        self.timeout = timeout
        self.extra_hosts = extra_confirm_hosts
        self.poll_interval = poll_interval
        self.user_agent = user_agent or (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
        )

    def wait_link(self, ident: MailIdentity) -> str | None:
        return self.mail.wait_confirm(
            ident,
            timeout=self.timeout,
            extra_hosts=self.extra_hosts,
            poll_interval=self.poll_interval,
        )

    def visit(self, link: str, proxy: Proxy | None = None) -> VerifyResult:
        headers = {
            "User-Agent": self.user_agent,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
            "Referer": "https://qoder.com/",
        }
        try:
            r = requests.get(
                link,
                headers=headers,
                proxies=proxy.requests_proxies() if proxy else None,
                timeout=30,
                allow_redirects=True,
            )
            ok = 200 <= r.status_code < 400
            return VerifyResult(ok=ok, link=link, status_code=r.status_code,
                                error="" if ok else f"http_{r.status_code}")
        except Exception as exc:
            return VerifyResult(ok=False, link=link, error=str(exc))

    def run(self, ident: MailIdentity, proxy: Proxy | None = None) -> VerifyResult:
        link = self.wait_link(ident)
        if not link:
            return VerifyResult(ok=False, error="mail_dropped_or_timeout")
        return self.visit(link, proxy=proxy)
