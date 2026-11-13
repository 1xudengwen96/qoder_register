"""尝试各种凭据放置方式，找出 ExtractDynamicIP 认可的形式。

错误 "Invalid credential type" 表明服务端在找凭据但没识别。
穷举：body 平铺、header 各种、query、以及 OpenApiRequest 结构。

用法：
    python tools/probe_ipdeep_auth.py --key X --secret Y --uid Z
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import sys
import time
import uuid
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

PROXY = {"http": "http://127.0.0.1:7897", "https": "http://127.0.0.1:7897"}
HOST = "https://api.ipdeep.cn"
PATH = "/api/Pro/DynamicIp/ExtractDynamicIP"

BIZ = {
    "format": "info",
    "proxyType": "dynamicResidential",
    "extractType": "authentication",
    "generateType": "routine",
    "countryCode": "US",
    "protocol": "http",
    "rotationType": "stickiness",
    "duration": 30,
    "num": 1,
}


def do(label: str, body: dict, headers: dict | None = None,
       params: dict | None = None) -> tuple[bool, str]:
    try:
        r = requests.post(HOST + PATH, json=body, headers=headers or {},
                          params=params, proxies=PROXY, timeout=15)
    except Exception as exc:
        return False, f"{type(exc).__name__}"
    ct = r.headers.get("Content-Type", "")
    if "json" not in ct:
        return False, f"{r.status_code} {r.text[:50].strip()}"
    j = r.json()
    code = str(j.get("ErrCode", ""))
    ok = j.get("Success") is True or code == "0"
    return ok, f"code={code} {str(j.get('ErrMsg'))[:60]}"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--key", required=True)
    ap.add_argument("--secret", required=True)
    ap.add_argument("--uid", default="")
    args = ap.parse_args()

    k, s, uid = args.key, args.secret, args.uid
    results: list[tuple[str, bool, str]] = []

    # 1. body 平铺
    results.append(("body: key+secret",
                    *do("", {**BIZ, "key": k, "secret": s})))
    results.append(("body: key+secret+userId",
                    *do("", {**BIZ, "key": k, "secret": s, "userId": uid})))
    results.append(("body: apiKey+apiSecret",
                    *do("", {**BIZ, "apiKey": k, "apiSecret": s})))

    # 2. Header
    results.append(("hdr: X-Api-Key + X-Api-Secret",
                    *do("", BIZ, {"X-Api-Key": k, "X-Api-Secret": s})))
    results.append(("hdr: Authorization Bearer key",
                    *do("", BIZ, {"Authorization": f"Bearer {k}"})))
    results.append(("hdr: Authorization Basic key:secret",
                    *do("", BIZ, {"Authorization": "Basic " +
                                  base64.b64encode(f"{k}:{s}".encode()).decode()})))
    results.append(("hdr: key + secret",
                    *do("", BIZ, {"key": k, "secret": s})))
    results.append(("hdr: ApiKey", *do("", BIZ, {"ApiKey": k})))
    results.append(("hdr: apikey+apisecret",
                    *do("", BIZ, {"apikey": k, "apisecret": s})))

    # 3. Query
    results.append(("query: key+secret",
                    *do("", BIZ, None, {"key": k, "secret": s})))

    # 4. OpenApiRequest 平铺但 data 是明文 JSON
    results.append(("OpenApiRequest, data=明文json",
                    *do("", {"key": k, "timestamp": int(time.time() * 1000),
                             "nonce": str(uuid.uuid4()),
                             "data": json.dumps(BIZ), "sign": "0" * 32})))

    # 5. 只传 key 看是否换错误
    results.append(("body: 只 key", *do("", {**BIZ, "key": k})))
    # 6. 只传 secret
    results.append(("body: 只 secret", *do("", {**BIZ, "secret": s})))

    # 7. uid 当 key
    if uid:
        results.append(("body: 用userId当key",
                        *do("", {**BIZ, "key": uid, "secret": s})))

    print("=== 凭据放置方式穷举 ===\n")
    for label, ok, detail in results:
        print(f"[{'OK  ' if ok else 'FAIL'}] {label:<34} {detail}")

    okc = sum(1 for _, ok, _ in results if ok)
    print(f"\n成功 {okc}/{len(results)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
