"""精确定位 IPDEEP sign 算法（小范围、带节流）。

已知：
- 路径正确：/openApi/V2/Account/balance → 10006 Sign Is Invalid
- 文档示例 sign 是 32 位 hex → MD5
- 文档：sign = OpenApiSignHelper.GenerateSign(parameters, key)

只试最可能的 20 种，每次间隔 1.2 秒，避免 10007 限流。

用法：
    python tools/crack_sign2.py
"""

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


def md5(s: str) -> str:
    return hashlib.md5(s.encode()).hexdigest()


def sha256(s: str) -> str:
    return hashlib.sha256(s.encode()).hexdigest()


def test(label: str, sign: str, payload: dict) -> bool:
    p = dict(payload)
    p["sign"] = sign
    try:
        r = requests.post(HOST + "/Account/balance", json=p,
                          proxies=PROXY, timeout=15)
        j = r.json()
    except Exception as exc:
        print(f"  [{label:<38}] 请求异常 {type(exc).__name__}")
        return False

    code = str(j.get("ErrCode", ""))
    msg = str(j.get("ErrMsg", ""))[:40]
    if code != "10006":
        print(f"  [{label:<38}] ✅ code={code} {msg}")
        print(f"        sign={sign}")
        return True
    print(f"  [{label:<38}] 10006")
    return False


def main() -> int:
    data_str = encrypt(b"{}")
    ts = int(time.time() * 1000)
    nonce = str(uuid.uuid4())

    base = {"key": KEY, "timestamp": ts, "nonce": nonce, "data": data_str}
    with_lang = {**base, "language": "zh"}

    variants: list[tuple[str, str, dict]] = []

    def add(label: str, fields: dict, build, algo=md5, tail=KEY):
        order = sorted(fields.keys())
        raw = build(fields, order)
        variants.append((label, algo(raw + tail), fields))

    # --- 仅 MD5，各种拼接 ---
    add("sorted kv+key(md5)", base, lambda f, o: "".join(f"{k}{f[k]}" for k in o))
    add("sorted kv+secret(md5)", base,
        lambda f, o: "".join(f"{k}{f[k]}" for k in o), tail=SECRET)
    add("sorted k=v&+key(md5)", base,
        lambda f, o: "&".join(f"{k}={f[k]}" for k in o))
    add("sorted k=v&+secret(md5)", base,
        lambda f, o: "&".join(f"{k}={f[k]}" for k in o), tail=SECRET)
    add("sorted v-only+key(md5)", base,
        lambda f, o: "".join(str(f[k]) for k in o))
    add("sorted kv+key(lower)", base,
        lambda f, o: "".join(f"{k.lower()}{f[k]}" for k in o))
    add("sorted kv, key前缀(md5)", base,
        lambda f, o: "".join(f"{k}{f[k]}" for k in o), tail="")

    # 前缀式
    raw = "".join(f"{k}{base[k]}" for k in sorted(base))
    variants.append(("key+sorted kv(md5)", md5(KEY + raw), base))

    # --- 含 language ---
    add("含lang sorted kv+key(md5)", with_lang,
        lambda f, o: "".join(f"{k}{f[k]}" for k in o))
    add("含lang sorted k=v&+key", with_lang,
        lambda f, o: "&".join(f"{k}={f[k]}" for k in o))

    # --- SHA256 ---
    add("sorted kv+key(sha256)", base,
        lambda f, o: "".join(f"{k}{f[k]}" for k in o), algo=sha256)

    # --- JSON 序列化后 MD5 ---
    variants.append((
        "json(sorted)+key(md5)",
        md5(json.dumps(base, sort_keys=True, separators=(",", ":")) + KEY),
        base,
    ))

    # --- sign 本身用 AES ---
    raw_sorted = "".join(f"{k}{base[k]}" for k in sorted(base))
    variants.append(("raw AES加密(不md5)", encrypt((raw_sorted + KEY).encode()), base))

    # --- 大写 MD5 ---
    variants.append((
        "sorted kv+key(md5 大写)",
        md5("".join(f"{k}{base[k]}" for k in sorted(base)) + KEY).upper(),
        base,
    ))

    print(f"=== 试 {len(variants)} 种签名 ===\n")
    for i, (label, sign, payload) in enumerate(variants, 1):
        if test(f"{i:02d} {label}", sign, payload):
            return 0
        time.sleep(1.3)

    print("\n未找到。下一步方向：")
    print("  1. sign 可能包含 URL 编码后的 data")
    print("  2. timestamp 可能按秒而非毫秒")
    print("  3. 可能需要先用 secret 而非 key 参与")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
