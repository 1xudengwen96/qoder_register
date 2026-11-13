"""暴力探测 IPDEEP /api/ 下的 Controller/Action，找出开放 API 入口。

判定：
  - nginx 404（text/plain）→ 路由不存在
  - JSON 响应 → 应用层可达
    - ErrCode=40004 "Invalid credential type" → 需要登录态，不是开放 API
    - ErrCode=10006 "签名无效" → **这就是开放 API**（认得 OpenApiRequest 结构）
    - ErrCode=10008 → 开放 API，凭据无效
    - 其它 → 记录

用法：
    python tools/probe_ipdeep_api.py --key X --secret Y
"""

from __future__ import annotations

import argparse
import hashlib
import itertools
import sys
import time
import uuid
from pathlib import Path

import requests
from cryptography.hazmat.primitives import padding as sym_padding
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

PROXY = {"http": "http://127.0.0.1:7897", "https": "http://127.0.0.1:7897"}
HOST = "https://api.ipdeep.cn"

CONTROLLERS = [
    "OpenApi", "Open", "Api", "Gateway", "V2", "ApiV2",
    "User", "Account", "DynamicIp", "DynamicProxy", "Proxy",
    "StaticIp", "BasicData", "Order", "IpWhitelist",
]

ACTIONS = [
    "GetBalance", "QueryBalance", "GetTraffic", "GetUserInfo",
    "GetCountries", "GetCountryList",
    "GenerateProxyInfo", "ExtractDynamicIP", "GenerateProxyLink",
]


def build_payload(key: str, secret: str) -> dict:
    aes_key = hashlib.sha256(f"{key}{secret}".encode()).hexdigest()[:32].encode()
    plain = b"{}"
    iv = uuid.uuid4().bytes
    padder = sym_padding.PKCS7(128).padder()
    padded = padder.update(plain) + padder.finalize()
    enc = Cipher(algorithms.AES(aes_key), modes.CBC(iv)).encryptor()
    ct = enc.update(padded) + enc.finalize()
    import base64
    payload = {
        "key": key,
        "timestamp": int(time.time() * 1000),
        "nonce": str(uuid.uuid4()),
        "data": base64.b64encode(iv + ct).decode(),
        "language": "zh",
    }
    concat = "".join(str(payload[k]) for k in sorted(payload))
    payload["sign"] = hashlib.md5((concat + key).encode()).hexdigest()
    return payload


def probe(path: str, payload: dict) -> tuple[str, str]:
    try:
        r = requests.post(HOST + path, json=payload, proxies=PROXY, timeout=12)
    except Exception as exc:
        return "ERR", type(exc).__name__

    ct = r.headers.get("Content-Type", "")
    if "json" not in ct:
        if r.status_code == 404:
            return "404", ""
        return str(r.status_code), r.text[:40].strip()

    try:
        j = r.json()
    except Exception:
        return "JSON?", r.text[:40]
    code = str(j.get("ErrCode", ""))
    msg = str(j.get("ErrMsg", ""))[:50]
    return code, msg


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--key", required=True)
    ap.add_argument("--secret", required=True)
    args = ap.parse_args()

    payload = build_payload(args.key, args.secret)

    interesting: list[tuple[str, str, str]] = []
    nginx404 = 0
    total = 0

    for ctrl, act in itertools.product(CONTROLLERS, ACTIONS):
        path = f"/api/{ctrl}/{act}"
        total += 1
        code, msg = probe(path, payload)

        if code == "404":
            nginx404 += 1
            continue

        if code in ("0",):
            print(f"[OK   ] {path:<42} code={code} {msg}")
            interesting.append((path, code, msg))
        elif code in ("10006", "10008", "10001", "10004"):
            print(f"[API? ] {path:<42} code={code} {msg}")
            interesting.append((path, code, msg))
        elif code == "40004":
            print(f"[AUTH ] {path:<42} code={code} {msg}")
            interesting.append((path, code, msg))
        else:
            print(f"[???  ] {path:<42} code={code} {msg}")
            interesting.append((path, code, msg))

    print(f"\n探测 {total} 个，nginx 404 {nginx404} 个，有响应 {len(interesting)} 个")

    print("\n=== 开放 API 候选（code 10001/10004/10006/10008）===")
    cands = [x for x in interesting if x[1] in ("10001", "10004", "10006", "10008")]
    if cands:
        for p, c, m in cands:
            print(f"  {HOST}{p}  code={c} {m}")
    else:
        print("  无")

    print("\n=== 成功（code 0）===")
    ok = [x for x in interesting if x[1] == "0"]
    for p, c, m in ok:
        print(f"  {HOST}{p}")
    if not ok:
        print("  无")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
