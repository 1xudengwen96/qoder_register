"""用完整 OpenApiRequest 结构测 /api/Pro/... 路径，确认是不是开放 API 入口。

判定：返回 10006（签名无效）说明路径对、结构对，只是签名算法待定；
      返回 40004（Invalid credential type）说明结构没被识别。

用法：
    python tools/probe_ipdeep_proto.py --key X --secret Y
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
from cryptography.hazmat.primitives import padding as sym_padding
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

PROXY = {"http": "http://127.0.0.1:7897", "https": "http://127.0.0.1:7897"}
HOST = "https://api.ipdeep.cn"

PATHS = [
    "/api/Pro/DynamicIp/ExtractDynamicIP",
    "/api/Pro/BasicData/GetCountryList",
    "/api/Pro/StaticIp/GetIpNum",
    "/api/User/GetBalance",
    "/api/Pro/User/GetBalance",
]


def make_aes_key(key: str, secret: str) -> bytes:
    """aesKey = SHA256(key+secret).Substring(0,32)
    Substring 取的是 hex 字符串前 32 个字符 → 16 字节 → AES-128
    """
    hex_str = hashlib.sha256(f"{key}{secret}".encode()).hexdigest()
    return hex_str[:32].encode()   # 32 个 ASCII 字符 = 32 字节 → AES-256

def make_aes_key_bytes(key: str, secret: str) -> bytes:
    """另一种理解：取 SHA256 摘要的前 16 字节"""
    return hashlib.sha256(f"{key}{secret}".encode()).digest()[:16]


def aes_encrypt(aes_key: bytes, plain: bytes) -> str:
    iv = uuid.uuid4().bytes
    padder = sym_padding.PKCS7(128).padder()
    padded = padder.update(plain) + padder.finalize()
    enc = Cipher(algorithms.AES(aes_key), modes.CBC(iv)).encryptor()
    ct = enc.update(padded) + enc.finalize()
    return base64.b64encode(iv + ct).decode()


def build(key: str, secret: str, biz: dict, aes_key: bytes) -> dict:
    data = aes_encrypt(aes_key, json.dumps(biz, separators=(",", ":")).encode())
    payload = {
        "key": key,
        "timestamp": int(time.time() * 1000),
        "nonce": str(uuid.uuid4()),
        "data": data,
        "language": "zh",
    }
    # 几种签名尝试
    concat = "".join(str(payload[k]) for k in sorted(payload))
    payload["sign"] = hashlib.md5((concat + key).encode()).hexdigest()
    return payload


def call(path: str, payload: dict) -> str:
    try:
        r = requests.post(HOST + path, json=payload, proxies=PROXY, timeout=15)
    except Exception as exc:
        return f"{type(exc).__name__}: {str(exc)[:60]}"
    ct = r.headers.get("Content-Type", "")
    if "json" in ct:
        try:
            j = r.json()
            return f"JSON ErrCode={j.get('ErrCode')} Msg={str(j.get('ErrMsg'))[:70]}"
        except Exception:
            return f"JSON? {r.text[:80]}"
    return f"{r.status_code} {r.text[:60].strip()}"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--key", required=True)
    ap.add_argument("--secret", required=True)
    args = ap.parse_args()

    for label, aes_key in (
        ("hex前32字符(32B,AES-256)", make_aes_key(args.key, args.secret)),
        ("digest前16字节(AES-128)", make_aes_key_bytes(args.key, args.secret)),
    ):
        print(f"\n=== AES key 方案：{label} ===")
        for p in PATHS:
            payload = build(args.key, args.secret, {}, aes_key)
            print(f"  {p:<42} {call(p, payload)}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
