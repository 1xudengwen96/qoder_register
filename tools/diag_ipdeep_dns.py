"""查 openapi.ipdeep.com 的真实 IP 并直连测试（绕过 Clash fake-ip）。

Clash 的 fake-ip 会把域名解析成 198.18.x.x，掩盖真实 IP。
用 DoH（DNS over HTTPS）查真实解析结果，再直连测试，
判断 404 是"路径不存在"还是"IP 不在白名单被 nginx 拒绝"。

用法：
    python tools/diag_ipdeep_dns.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

PROXY = {"http": "http://127.0.0.1:7897", "https": "http://127.0.0.1:7897"}
DOH = "https://dns.alidns.com/resolve"
DOH2 = "https://cloudflare-dns.com/dns-query"

HOSTS = ["openapi.ipdeep.com", "api.ipdeep.cn", "user.ipdeep.cn"]


def resolve_doh(host: str, endpoint: str = DOH) -> list[str]:
    try:
        r = requests.get(endpoint, params={"name": host, "type": "A"},
                         proxies=PROXY, timeout=12,
                         headers={"Accept": "application/dns-json"})
        j = r.json()
        return [a["data"] for a in j.get("Answer", []) if a.get("type") == 1]
    except Exception as exc:
        return [f"ERR {type(exc).__name__}"]


def try_direct(ip: str, host: str, path: str) -> str:
    """直连 IP + Host header，绕过 DNS。"""
    url = f"https://{ip}{path}"
    try:
        r = requests.get(url, headers={"Host": host}, verify=False,
                         proxies=None, timeout=12, allow_redirects=False)
        return f"{r.status_code} server={r.headers.get('Server','?')} " \
               f"len={len(r.content)} body={r.text[:60]!r}"
    except Exception as exc:
        return f"{type(exc).__name__}: {str(exc)[:70]}"


def main() -> int:
    import urllib3
    urllib3.disable_warnings()

    print("=== DoH 真实解析 ===")
    real: dict[str, list[str]] = {}
    for h in HOSTS:
        ips = resolve_doh(h)
        real[h] = ips
        print(f"  {h:<24} {ips}")

    print("\n=== 直连真实 IP（Host header 保持域名）===")
    for h, ips in real.items():
        for ip in ips[:2]:
            if "ERR" in ip or ":" in ip:
                continue
            for path in ("/", "/openApi/V2/User/GetBalance"):
                print(f"  {ip} {h}{path}")
                print(f"      {try_direct(ip, h, path)}")

    print("\n=== 对照：走代理访问 ===")
    for h in HOSTS:
        try:
            r = requests.get(f"https://{h}/", proxies=PROXY, timeout=12)
            print(f"  {h:<24} {r.status_code} server={r.headers.get('Server','?')}")
        except Exception as exc:
            print(f"  {h:<24} {type(exc).__name__}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
