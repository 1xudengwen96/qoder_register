"""探测 IPDEEP openapi 域的真实路径前缀。

做法：对候选路径组合逐个打，看哪个不是 404。
带正确签名，避免被"签名无效"掩盖真实路径。

用法：
    python tools/probe_ipdeep_paths.py --key X --secret Y
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.net.ipdeep import IpdeepClient  # noqa: E402

BASES = [
    "https://openapi.ipdeep.com",
    "https://api.ipdeep.cn",
    "https://openapi.ipdeep.cn",
]

PREFIXES = ["", "/api", "/openapi", "/api/openapi", "/openapi/api", "/api/v1"]

ENDPOINTS = [
    "User/GetBalance", "user/balance", "User/Balance", "Balance",
    "Account/GetBalance", "User/GetTraffic",
    "DynamicProxy/GetCountries", "DynamicIp/GetCountries",
    "OpenApi/User/GetBalance",
]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--key", required=True)
    ap.add_argument("--secret", required=True)
    args = ap.parse_args()

    print("=== 探测 openapi 路径 ===\n")
    hits: list[str] = []

    for base in BASES:
        c = IpdeepClient(args.key, args.secret, base=base)
        for prefix in PREFIXES:
            for ep in ENDPOINTS:
                path = f"{prefix}/{ep}"
                payload = c._build_payload() if hasattr(c, "_build_payload") else None
                if payload is None:
                    # 手工构造
                    import time
                    import uuid
                    payload = {
                        "apiKey": args.key,
                        "timestamp": int(time.time() * 1000),
                        "nonce": str(uuid.uuid4()),
                        "data": c._encrypt({}),
                    }
                    payload["sign"] = c._sign(payload)

                try:
                    r = requests.post(base + path, json=payload, timeout=10)
                except Exception as exc:
                    print(f"[ERR ] {base}{path} → {type(exc).__name__}")
                    continue

                if r.status_code == 404:
                    continue
                body = r.text[:160].replace("\n", " ")
                mark = "HIT " if r.status_code == 200 else "EXIST"
                print(f"[{mark}] {base}{path} → {r.status_code} {body}")
                hits.append(f"{base}{path}")

    print(f"\n共命中 {len(hits)} 个")
    for h in hits:
        print(f"  {h}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
