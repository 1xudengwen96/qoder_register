"""诊断 IPDEEP 域名到底通不通（走代理 vs 不走代理）。

背景：本机跑着 Clash Verge（127.0.0.1:7897，fake-ip 模式），
域名被解析成 198.18.x.x（RFC2544 保留段），Python requests 不自动走系统代理，
所以直连会 ConnectionError。必须显式挂代理。

本脚本对比两种方式，找出真实的 endpoint。

用法：
    python tools/diag_ipdeep_net.py
"""

from __future__ import annotations

import socket
import sys
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

PROXY = "http://127.0.0.1:7897"
HOSTS = [
    "openapi.ipdeep.cn",
    "openapi.ipdeep.com",
    "api.ipdeep.cn",
    "api.ipdeep.com",
    "user.ipdeep.cn",
]
PATHS = ["/", "/api/OpenApi/User/GetBalance", "/api/Pro/BasicData/GetCountryList"]


def dns_lookup(host: str) -> str:
    try:
        return socket.gethostbyname(host)
    except Exception as exc:
        return f"FAIL: {exc}"


def is_fake_ip(ip: str) -> bool:
    return ip.startswith("198.18.") or ip.startswith("198.19.")


def try_request(url: str, use_proxy: bool) -> str:
    proxies = {"http": PROXY, "https": PROXY} if use_proxy else None
    try:
        r = requests.get(url, proxies=proxies, timeout=15,
                         headers={"User-Agent": "Mozilla/5.0"})
        return f"HTTP {r.status_code} len={len(r.content)}"
    except Exception as exc:
        return f"{type(exc).__name__}: {str(exc)[:90]}"


def main() -> int:
    print("=== 1. DNS 解析 ===")
    for h in HOSTS:
        ip = dns_lookup(h)
        flag = "  <== fake-ip，被代理工具接管" if is_fake_ip(ip) else ""
        print(f"  {h:<24} -> {ip}{flag}")

    print(f"\n=== 2. 走代理（{PROXY}）访问 ===")
    for h in HOSTS:
        for p in PATHS[:2]:
            url = f"https://{h}{p}"
            print(f"  {url:<58} {try_request(url, True)}")

    print("\n=== 3. 不走代理直连 ===")
    for h in HOSTS[:3]:
        url = f"https://{h}/"
        print(f"  {url:<58} {try_request(url, False)}")

    print("\n=== 4. 用 curl（走系统代理）对照 ===")
    import subprocess
    for h in HOSTS:
        try:
            r = subprocess.run(
                ["curl", "-s", "-o", "NUL", "-w", "%{http_code}", "--max-time", "12",
                 "--proxy", PROXY, f"https://{h}/"],
                capture_output=True, text=True, timeout=20)
            print(f"  https://{h}/  -> {r.stdout.strip()}")
        except Exception as exc:
            print(f"  https://{h}/  -> {exc}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
