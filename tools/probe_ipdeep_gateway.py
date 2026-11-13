"""验证 IPDEEP key/secret 是否就是代理网关的账密。

思路：IPDEEP 这类服务商常见两种接入方式
  A) REST API：用 key/secret 调接口换代理列表（我们试了，ExtractDynamicIP 报凭据类型错）
  B) 代理网关：key/secret 直接当代理的 username/password

如果 B 成立，那根本不用调 API，直接把 key/secret 填进比特浏览器的代理配置即可。

本脚本：
1. 探测常见网关域名
2. 用 key/secret 做代理认证，测试能否连通
3. 换几种用户名格式（纯 key / key-region-us / key-session-xxx）

用法：
    python tools/probe_ipdeep_gateway.py --key X --secret Y
"""

from __future__ import annotations

import argparse
import socket
import sys
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# 常见网关域名候选
GATE_HOSTS = [
    "gate.ipdeep.cn",
    "gate.ipdeep.com",
    "proxy.ipdeep.cn",
    "gw.ipdeep.cn",
    "proxy.ipdeep.com",
    "d.ipdeep.cn",
    "dy.ipdeep.cn",
    "res.ipdeep.cn",
    "gateway.ipdeep.cn",
]

# 常见端口
PORTS = [10000, 1080, 8000, 9000, 3128, 8080, 5000, 3000]

TEST_URL = "https://api.ipify.org?format=json"


def resolve(host: str) -> list[str]:
    try:
        return list({x[4][0] for x in socket.getaddrinfo(host, None)})
    except Exception:
        return []


def try_proxy(host: str, port: int, user: str, pwd: str) -> tuple[bool, str]:
    proxy = f"http://{user}:{pwd}@{host}:{port}"
    try:
        r = requests.get(TEST_URL, proxies={"http": proxy, "https": proxy},
                         timeout=15)
        if r.status_code == 200:
            return True, r.text[:80]
        return False, f"HTTP {r.status_code}"
    except Exception as exc:
        return False, f"{type(exc).__name__}: {str(exc)[:60]}"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--key", required=True)
    ap.add_argument("--secret", required=True)
    args = ap.parse_args()

    print("=== 1. 网关域名解析 ===")
    live: list[str] = []
    for h in GATE_HOSTS:
        ips = resolve(h)
        if ips:
            print(f"  {h:<24} {ips}")
            live.append(h)
        else:
            print(f"  {h:<24} 解析失败")

    if not live:
        print("\n没有可用网关域名")
        return 2

    print("\n=== 2. 用 key/secret 做代理认证 ===")
    # 用户名格式候选
    user_formats = [
        ("纯 key", args.key),
        ("key+region", f"{args.key}-region-US"),
        ("key+country", f"{args.key}-country-US"),
        ("key_us", f"{args.key}_us"),
        ("userId", ""),
    ]

    hits: list[str] = []
    for h in live:
        for port in PORTS:
            # 先测纯 key
            ok, msg = try_proxy(h, port, args.key, args.secret)
            if ok:
                print(f"  [OK] {h}:{port} user={args.key}  → {msg}")
                hits.append(f"http://{args.key}:{args.secret}@{h}:{port}")
                continue
            # 端口不通就不试其它用户格式了
            if "Connection refused" in msg or "timed out" in msg.lower():
                continue
            print(f"  [? ] {h}:{port} user={args.key}  → {msg}")

            for label, user in user_formats[1:]:
                if not user:
                    continue
                ok2, msg2 = try_proxy(h, port, user, args.secret)
                if ok2:
                    print(f"  [OK] {h}:{port} user={user} ({label}) → {msg2}")
                    hits.append(f"http://{user}:{args.secret}@{h}:{port}")

    print("\n=== 结果 ===")
    if hits:
        print(f"找到 {len(hits)} 个可用代理：")
        for h in hits:
            print(f"  {h}")
    else:
        print("未找到可用组合。")
        print("说明 key/secret 不是代理网关账密，需要用 REST API 或后台生成的链接。")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
