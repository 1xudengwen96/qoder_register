"""统一收件箱聚合器：把三条邮件源的信都收口到一个读取入口。

用途：
1. 路线 C 的隐藏地址全部转发到一个聚合邮箱，这里负责按收件人分发；
2. 验收阶段（verify）只调 ``MailAggregator.wait_confirm``，不关心来自哪个 provider；
3. ``stats`` 命令统计各 provider 的丢信率，用于判断该不该降权某条路线。
"""

from __future__ import annotations

import threading
import time
from typing import Callable

from app.mail.base import MailIdentity, MailMessage, ProviderHealth


class MailAggregator:
    def __init__(self, providers: dict[str, object], strategy: str = "weighted",
                 weights: dict[str, int] | None = None,
                 strict_order: list[str] | None = None) -> None:
        self.providers = providers
        self.strategy = strategy
        self.weights = weights or {}
        self.strict_order = strict_order or list(providers)
        # 并发 worker 共享同一个聚合器：跨 provider 的调度决策也要互斥，
        # 否则两个线程可能同时判“某源还有货”而重复取同一地址。
        self._acquire_lock = threading.Lock()

    # ---- 取地址 -----------------------------------------------------------
    def _ordered_providers(self) -> list[str]:
        if self.strategy == "strict_priority":
            return [p for p in self.strict_order if p in self.providers]
        if self.strategy == "round_robin":
            return list(self.providers)
        # weighted：按权重降序，简单实现：每轮按权重比例分配
        return sorted(
            self.providers,
            key=lambda p: self.weights.get(p, 0),
            reverse=True,
        )

    def acquire(self, n: int = 1) -> list[MailIdentity]:
        with self._acquire_lock:
            return self._acquire_locked(n)

    def _acquire_locked(self, n: int) -> list[MailIdentity]:
        out: list[MailIdentity] = []
        order = self._ordered_providers()
        if self.strategy == "weighted" and self.weights:
            total_w = sum(self.weights.get(p, 0) for p in order) or 1
            for p in order:
                want = max(1, round(n * self.weights.get(p, 0) / total_w))
                got = self.providers[p].acquire(want)
                out.extend(got)
                if len(out) >= n:
                    return out[:n]
            return out

        for p in order:
            got = self.providers[p].acquire(n - len(out))
            out.extend(got)
            if len(out) >= n:
                break
        return out[:n]

    # ---- 收信 -------------------------------------------------------------
    def wait_for_mail(
        self,
        ident: MailIdentity,
        timeout: int = 600,
        match: Callable[[MailMessage], bool] | None = None,
        poll_interval: int = 8,
    ) -> MailMessage | None:
        """按地址所属 provider 转发，等待匹配邮件（otp_source 取码入口）。"""
        provider = self.providers.get(ident.provider)
        if provider is None:
            raise KeyError(f"未知邮件源：{ident.provider}")
        return provider.wait_for_mail(
            ident, timeout=timeout, match=match, poll_interval=poll_interval)

    def wait_confirm(
        self,
        ident: MailIdentity,
        timeout: int = 600,
        extra_hosts: tuple[str, ...] = (),
        poll_interval: int = 8,
    ) -> str | None:
        """等待确认邮件并返回确认链接。"""
        provider = self.providers.get(ident.provider)
        if provider is None:
            raise KeyError(f"未知邮件源：{ident.provider}")
        msg = provider.wait_for_mail(ident, timeout=timeout, poll_interval=poll_interval)
        if msg is None:
            return None
        return msg.confirm_link(extra_hosts=extra_hosts)

    # ---- 健康 -------------------------------------------------------------
    def health(self) -> list[ProviderHealth]:
        return [self.providers[p].health() for p in self._ordered_providers()]
