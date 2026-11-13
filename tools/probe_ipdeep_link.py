"""探测 IPDEEP 的 GET 提取链接方式（无需签名，直接用 key/secret 拼 URL）。

很多代理服务商提供"复制链接"功能，形如：
    https://api.xxx.com/api/getip?key=K&secret=S&num=1&country=US&format=txt

这种直接 GET 就返回代理，比特浏览器的"动态IP"功能可以直接填。

用法：
    python tools/probe_ipdeep_link.py --key X --secret Y
"""

from __future__ import annotations

import os

import sys
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

PROXY = {"http": "http://127.0.0.1:7897", "https": "http://127.0.0.1:7897"}

KEY = ""
SECRET = ""

# 常见路径
PATHS = [
    "/api/getip",
    "/api/getIp",
    "/getip",
    "/api/proxy/get",
    "/api/proxy/getip",
    "/api/ip/get",
    "/api/ip",
    "/api/dynamic/get",
    "/api/extract",
    "/api/DynamicIp/Get",
    "/openApi/getip",
    "/api/Proxy/GetIp",
    "/api/Pro/DynamicIp/GetIp",
]

HOSTS = ["https://api.ipdeep.cn", "https://openapi.ipdeep.com"]


def try_url(url: str) -> str:
    try:
        r = requests.get(url, proxies=PROXY, timeout=15)
    except Exception as exc:
        return f"{type(exc).__name__}"
    ct = r.headers.get("Content-Type", "")
    body = r.text[:150].replace("\n", " ")
    if r.status_code == 404:
        return "404"
    return f"{r.status_code} {ct[:22]} {body}"


def main() -> int:
    global KEY, SECRET
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--key", default=os.environ.get("IPDEEP_KEY", ""))
    ap.add_argument("--secret", default=os.environ.get("IPDEEP_SECRET", ""))
    args = ap.parse_args()
    KEY, SECRET = args.key, args.secret

    # 参数组合
    param_sets = [
        f"key={KEY}&secret={SECRET}",
        f"key={KEY}&secret={SECRET}&num=1",
        f"key={KEY}&secret={SECRET}&num=1&country=US&format=txt",
        f"key={KEY}&secret={SECRET}&num=1&proxyType=dynamicResidential&protocol=http",
        f"apiKey={KEY}&secret={SECRET}&num=1",
        f"key={KEY}&password={SECRET}&num=1",
    ]

    print("=== 探测 GET 提取链接 ===\n")
    hits: list[str] = []

    for host in HOSTS:
        for path in PATHS:
            for ps in param_sets[:2]:
                url = f"{host}{path}?{ps}"
                res = try_url(url)
                if res == "404":
                    continue
                print(f"[{host}{path}] {res}")
                if res.startswith("200"):
                    hits.append(url)

    print(f"\n命中 {len(hits)} 个")
    for h in hits:
        print(f"  {h}")

    if not hits:
        print("\n没有 GET 链接接口。")
        print("下一步建议：")
        print("  1. 登录 IPDEEP 后台，看「动态代理」页面有没有「复制链接 / API提取」按钮")
        print("  2. 如果有，把那条链接发我，我直接接进比特浏览器")
        print("  3. 或者提供后台账号密码，我用浏览器抓真实请求把 sign 算法看清")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
