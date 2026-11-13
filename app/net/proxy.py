"""代理池：从文件加载、按地区挑、健康检查、自动摘除。

代理文件格式（data/proxies.txt）：
    http://user:pass@1.2.3.4:8080#us
    socks5://user:pass@5.6.7.8:1080#jp
    http://9.9.9.9:3128#uk
``#`` 后面是地区标签，用于和浏览器时区/语言对齐。
"""

from __future__ import annotations

import dataclasses
import random
import time
from pathlib import Path
from urllib.parse import urlparse

import requests


@dataclasses.dataclass(slots=True)
class Proxy:
    raw: str
    scheme: str
    host: str
    port: int
    username: str | None = None
    password: str | None = None
    region: str | None = None
    health: str = "ok"
    use_count: int = 0
    fail_count: int = 0
    last_used: float = 0.0

    def requests_proxies(self) -> dict[str, str]:
        return {"http": self.raw, "https": self.raw}

    def playwright_proxy(self) -> dict[str, str]:
        out = {"server": f"{self.scheme}://{self.host}:{self.port}"}
        if self.username:
            out["username"] = self.username
        if self.password:
            out["password"] = self.password
        return out

    @property
    def exit_ip_hint(self) -> str:
        return f"{self.host}:{self.port}"


def parse_line(line: str) -> Proxy | None:
    line = line.split("#", 1)
    body = line[0].strip()
    region = line[1].strip().lower() if len(line) > 1 else None
    if not body or body.startswith("//"):
        return None
    if "://" not in body:
        body = "http://" + body
    u = urlparse(body)
    if not u.hostname or not u.port:
        return None
    return Proxy(
        raw=body,
        scheme=u.scheme,
        host=u.hostname,
        port=u.port,
        username=u.username,
        password=u.password,
        region=region,
    )


class ProxyPool:
    CHECK_URL = "https://api.ipify.org?format=json"

    def __init__(self, proxies: list[Proxy]) -> None:
        self.proxies = proxies
        self._by_region: dict[str, list[Proxy]] = {}
        for p in proxies:
            self._by_region.setdefault(p.region or "any", []).append(p)

    @classmethod
    def from_file(cls, path: str | Path) -> "ProxyPool":
        p = Path(path)
        if not p.exists():
            return cls([])
        out: list[Proxy] = []
        for line in p.read_text(encoding="utf-8").splitlines():
            px = parse_line(line)
            if px:
                out.append(px)
        return cls(out)

    # ---- 分配 -------------------------------------------------------------
    def acquire(self, region: str | None = None, exclude: set[str] | None = None) -> Proxy | None:
        exclude = exclude or set()
        pool = [
            p
            for p in (self._by_region.get(region, []) if region else self.proxies)
            if p.health == "ok" and p.raw not in exclude
        ]
        if not pool:
            pool = [p for p in self.proxies if p.health == "ok"]
        if not pool:
            return None
        # 优先用最久没被用过的，降低同一 IP 复用
        proxy = min(pool, key=lambda p: p.last_used)
        proxy.last_used = time.time()
        proxy.use_count += 1
        return proxy

    # ---- 健康检查 ---------------------------------------------------------
    def check(self, proxy: Proxy, timeout: int = 10) -> bool:
        try:
            r = requests.get(self.CHECK_URL, proxies=proxy.requests_proxies(), timeout=timeout)
            ok = r.status_code == 200
        except Exception:
            ok = False
        if ok:
            proxy.health = "ok"
        else:
            proxy.fail_count += 1
            if proxy.fail_count >= 3:
                proxy.health = "banned"
        return ok

    def mark_banned(self, proxy: Proxy) -> None:
        proxy.health = "banned"

    def summary(self) -> str:
        total = len(self.proxies)
        ok = sum(1 for p in self.proxies if p.health == "ok")
        regions = sorted({p.region for p in self.proxies if p.region})
        return f"代理 {total} 个，可用 {ok}，地区 {','.join(regions) or '未标注'}"


def random_identity_for_region(region: str | None) -> dict[str, str]:
    """让浏览器时区/语言与代理地区对齐，避免明显失配。"""
    table = {
        "us": {"timezone": "America/New_York", "locale": "en-US"},
        "uk": {"timezone": "Europe/London", "locale": "en-GB"},
        "jp": {"timezone": "Asia/Tokyo", "locale": "ja-JP"},
        "sg": {"timezone": "Asia/Singapore", "locale": "en-SG"},
        "de": {"timezone": "Europe/Berlin", "locale": "de-DE"},
        "hk": {"timezone": "Asia/Hong_Kong", "locale": "zh-HK"},
    }
    return table.get((region or "us").lower(), table["us"])
