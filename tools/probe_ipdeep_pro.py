"""在 /api/Pro/ 下探测完整接口清单。

已知能通：
  /api/Pro/BasicData/GetCountryList  → 返回 141 国（认证通过！）
  /api/Pro/DynamicIp/ExtractDynamicIP → 40004 Invalid credential type
  /api/Pro/StaticIp/GetIpNum          → 40004

说明认证正确，只是部分接口需要额外凭据配置。

用法：
    python tools/probe_ipdeep_pro.py --key X --secret Y
"""

from __future__ import annotations

import argparse
import base64
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
    "User", "Account", "BasicData", "DynamicIp", "DynamicProxy",
    "StaticIp", "Order", "IpWhitelist", "WhiteList", "OpenApi", "Open",
    "Proxy", "Setmeal", "Balance", "Traffic", "Common", "Info",
]

ACTIONS = [
    "GetBalance", "GetTraffic", "GetUserInfo", "GetAccountInfo",
    "GetCountryList", "GetStateList", "GetCityList",
    "GetIpNum", "ExtractDynamicIP", "GetDynamicIp",
    "GenerateProxyInfo", "GenerateProxyLink",
    "GetStaticIpList", "GetIpList",
    "GetIpWhitelistList", "GetOrderList",
]


def build(key: str, secret: str, biz: dict | None = None) -> dict:
    aes_key = hashlib.sha256(f"{key}{secret}".encode()).hexdigest()[:32].encode()
    plain = (base64.b64encode(b"{}") and __import__("json").dumps(
        biz or {}, separators=(",", ":")).encode())
    iv = uuid.uuid4().bytes
    padder = sym_padding.PKCS7(128).padder()
    padded = padder.update(plain) + padder.finalize()
    enc = Cipher(algorithms.AES(aes_key), modes.CBC(iv)).encryptor()
    ct = enc.update(padded) + enc.finalize()
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


def probe(path: str, payload: dict) -> tuple[str, str, str]:
    try:
        r = requests.post(HOST + path, json=payload, proxies=PROXY, timeout=12)
    except Exception as exc:
        return "ERR", type(exc).__name__, ""
    ct = r.headers.get("Content-Type", "")
    if "json" not in ct:
        return ("404" if r.status_code == 404 else str(r.status_code)), "", ""
    j = r.json()
    return (str(j.get("ErrCode", "")), str(j.get("ErrMsg", ""))[:45],
            str(j.get("ResData"))[:80])


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--key", required=True)
    ap.add_argument("--secret", required=True)
    args = ap.parse_args()

    ok_list: list[tuple[str, str]] = []
    auth_list: list[tuple[str, str]] = []
    total = 0
    n404 = 0

    for ctrl, act in itertools.product(CONTROLLERS, ACTIONS):
        path = f"/api/Pro/{ctrl}/{act}"
        total += 1
        code, msg, data = probe(path, build(args.key, args.secret))

        if code == "404":
            n404 += 1
            continue
        if code == "0":
            print(f"[OK  ] {path:<44} {data[:60]}")
            ok_list.append((path, data))
        elif code == "40004":
            print(f"[AUTH] {path:<44} {msg}")
            auth_list.append((path, msg))
        else:
            print(f"[{code:<5}] {path:<44} {msg}")

    print(f"\n探测 {total} 个：404 {n404}，成功 {len(ok_list)}，需额外凭据 {len(auth_list)}")

    print("\n=== 可直接用的接口 ===")
    for p, d in ok_list:
        print(f"  {HOST}{p}")
        print(f"      {d}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
