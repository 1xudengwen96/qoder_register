"""穷举 IPDEEP sign 签名算法。

已知：路径对了（/openApi/V2/Account/balance 返回 10006 Sign Is Invalid），
只需找到正确的签名算法。

文档：sign = OpenApiSignHelper.GenerateSign(parameters, key)
      参数含 key / timestamp / nonce / data / language，排除 sign

穷举维度：
1. 参与字段：是否含 language
2. 拼接形式：k+v / k=v& / 仅v / k:v / JSON
3. 排序：字典序 / 原序
4. 摘要：MD5 / SHA1 / SHA256
5. key 位置：末尾 / 开头 / 不加
6. 大小写：小写 / 大写

用法：
    python tools/crack_sign.py
"""

from __future__ import annotations

import os

import base64
import hashlib
import itertools
import json
import sys
import time
import uuid
from pathlib import Path
from urllib.parse import quote_plus

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
    padded = padder.add_padding = None  # placeholder
    padder = sym_padding.PKCS7(128).padder()
    padded = padder.update(plain) + padder.finalize()
    enc = Cipher(algorithms.AES(AES_KEY), modes.CBC(iv)).encryptor()
    ct = enc.update(padded) + enc.finalize()
    return base64.b64encode(iv + ct).decode()


def digest(algo: str, s: str) -> str:
    h = algo.lower()
    if h == "md5":
        return hashlib.md5(s.encode()).hexdigest()
    if h == "sha1":
        return hashlib.sha1(s.encode()).hexdigest()
    if h == "sha256":
        return hashlib.sha256(s.encode()).hexdigest()
    return ""


def build_string(fields: dict, order: list[str], style: str) -> str:
    parts = []
    for k in order:
        v = str(fields[k])
        if style == "kv":
            parts.append(f"{k}{v}")
        elif style == "k_eq_v":
            parts.append(f"{k}={v}")
        elif style == "k_eq_v_amp":
            parts.append(f"{k}={v}")
        elif style == "k_colon_v":
            parts.append(f"{k}:{v}")
        elif style == "v_only":
            parts.append(v)
        elif style == "kv_lower":
            parts.append(f"{k.lower()}{v}")
    if style == "k_eq_v_amp":
        return "&".join(parts)
    return "".join(parts)


def try_sign(sign_value: str, payload: dict) -> tuple[bool, str]:
    payload = dict(payload)
    payload["sign"] = sign_value
    try:
        r = requests.post(HOST + "/Account/balance", json=payload,
                          proxies=PROXY, timeout=12)
    except Exception as exc:
        return False, f"ERR {type(exc).__name__}"
    try:
        j = r.json()
    except Exception:
        return False, f"non-json {r.status_code}"
    code = str(j.get("ErrCode", ""))
    if code != "10006":
        return True, f"code={code} {str(j.get('ErrMsg'))[:50]}"
    return False, "10006"


def main() -> int:
    biz = {}
    data_str = encrypt(json.dumps(biz, separators=(",", ":")).encode())
    base = {
        "key": KEY,
        "timestamp": int(time.time() * 1000),
        "nonce": str(uuid.uuid4()),
        "data": data_str,
    }

    tried = 0
    found: list[tuple[str, str]] = []

    field_sets = [
        ("no_lang", ["key", "timestamp", "nonce", "data"]),
        ("with_lang", ["key", "timestamp", "nonce", "data", "language"]),
    ]
    styles = ["kv", "k_eq_v", "k_eq_v_amp", "k_colon_v", "v_only", "kv_lower"]
    algos = ["md5", "sha1", "sha256"]
    keypos = ["suffix", "prefix", "none"]

    for set_name, fields in field_sets:
        for with_lang in ([False] if set_name == "no_lang" else [True]):
            f = dict(base)
            if with_lang:
                f["language"] = "zh"

            for order_name, order in (
                ("sorted", sorted(f.keys())),
                ("natural", list(f.keys())),
            ):
                for style in styles:
                    for algo in algos:
                        for kp in keypos:
                            raw = build_string(f, order, style)
                            if kp == "suffix":
                                s = raw + KEY
                            elif kp == "prefix":
                                s = KEY + raw
                            else:
                                s = raw
                            sign = digest(algo, s)
                            if not sign:
                                continue

                            tried += 1
                            ok, info = try_sign(sign, f)
                            if ok:
                                found.append((
                                    f"{set_name}/{order_name}/{style}/{algo}/{kp}",
                                    info))
                                print(f"[FOUND] {set_name}/{order_name}/{style}/"
                                      f"{algo}/{kp} → {info}")
                                print(f"        sign={sign}")
                                return 0
                            if tried % 40 == 0:
                                print(f"  已试 {tried} 种...")

    print(f"\n试了 {tried} 种，未找到")
    print("试过的组合：", f"{set_name}/{order_name}/{style}/{algo}/{kp}")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
