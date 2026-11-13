"""逐字段比对 crack_sign2 与 ipdeep.py 的请求，找出差异。"""

from __future__ import annotations

import os

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

from app.net.ipdeep import IpdeepClient  # noqa: E402

KEY = os.environ.get("IPDEEP_KEY", "")
SECRET = os.environ.get("IPDEEP_SECRET", "")
HOST = "https://openapi.ipdeep.com/openApi/V2"
PROXY = {"http": "http://127.0.0.1:7897", "https": "http://127.0.0.1:7897"}
AES_KEY = hashlib.sha256(f"{KEY}{SECRET}".encode()).hexdigest()[:32].encode()


def encrypt(plain: bytes) -> str:
    iv = uuid.uuid4().bytes
    padder = sym_padding.PKCS7(128).padder()
    padded = padder.update(plain) + padder.finalize()
    enc = Cipher(algorithms.AES(AES_KEY), modes.CBC(iv)).encryptor()
    ct = enc.update(padded) + enc.finalize()
    return base64.b64encode(iv + ct).decode()


def sign_of(payload: dict, tail: str) -> str:
    raw = "".join(f"{k}{payload[k]}" for k in sorted(payload) if k != "sign")
    return hashlib.md5((raw + tail).encode()).hexdigest()


def post(payload: dict) -> str:
    try:
        r = requests.post(HOST + "/Account/balance", json=payload,
                          proxies=PROXY, timeout=15)
        j = r.json()
        return f"code={j.get('ErrCode')} msg={j.get('ErrMsg')}"
    except Exception as exc:
        return f"ERR {type(exc).__name__}"


def main() -> int:
    print("=== A. 手工构造（与 crack_sign2 完全一致）===")
    data_str = encrypt(b"{}")
    p1 = {
        "key": KEY,
        "timestamp": int(time.time() * 1000),
        "nonce": str(uuid.uuid4()),
        "data": data_str,
    }
    for tail_name, tail in (("key", KEY), ("secret", SECRET)):
        p = dict(p1)
        p["sign"] = sign_of(p, tail)
        raw = "".join(f"{k}{p[k]}" for k in sorted(p) if k != "sign")
        print(f"  tail={tail_name}")
        print(f"    raw   = {raw[:120]}...")
        print(f"    sign  = {p['sign']}")
        print(f"    → {post(p)}")
        time.sleep(1.5)

    print("\n=== B. 用 IpdeepClient 构造 ===")
    c = IpdeepClient(KEY, SECRET)
    data2 = c._aes_key
    payload = {
        "key": KEY,
        "timestamp": int(time.time() * 1000),
        "nonce": str(uuid.uuid4()),
        "data": encrypt(b"{}"),
    }
    s = c._sign(payload)
    payload["sign"] = s
    raw2 = "".join(f"{k}{payload[k]}" for k in sorted(payload) if k != "sign")
    print(f"  client._sign → {s}")
    print(f"  raw = {raw2[:120]}...")
    print(f"  → {post(payload)}")

    print("\n=== C. 校验两者 raw 是否等价 ===")
    print(f"  A(tail=secret) sign = {sign_of(p1, SECRET)}")
    print(f"  B              sign = {s}")
    print(f"  相同? {sign_of(p1, SECRET) == s}（不同则说明 timestamp/nonce/data 有差异）")

    print("\n=== D. 固定 timestamp+nonce+data 再比对 ===")
    fixed = {
        "key": KEY,
        "timestamp": 1731306000000,
        "nonce": "3f4a5b6c7d8e9f0a",
        "data": "U2FsdGVkX19EncryptedBase64",
    }
    print(f"  手算(key尾)    = {sign_of(fixed, KEY)}")
    print(f"  手算(secret尾) = {sign_of(fixed, SECRET)}")
    print(f"  文档示例 sign   = 1ba75a9f7e5bea0cc06ca732f163630b")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
