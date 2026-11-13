"""破解 IPDEEP sign：试固定字段顺序（C# Dictionary 插入序）与更多变体。

之前试的都是 sorted 字典序，但 C# Dictionary 默认遍历顺序是插入顺序，
文档给的顺序是 key, timestamp, nonce, data, language。

用法：
    python tools/crack_sign4.py
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


def fresh(with_lang: bool = False) -> dict:
    p = {
        "key": KEY,
        "timestamp": int(time.time() * 1000),
        "nonce": str(uuid.uuid4()),
        "data": encrypt(b"{}"),
    }
    if with_lang:
        p["language"] = "zh"
    return p


def md5(s) -> str:
    b = s.encode() if isinstance(s, str) else s
    return hashlib.md5(b).hexdigest()


def post(p: dict) -> tuple[str, str]:
    try:
        r = requests.post(HOST + "/Account/balance", json=p, proxies=PROXY, timeout=15)
        j = r.json()
        return str(j.get("ErrCode", "")), str(j.get("ErrMsg", ""))[:45]
    except Exception as exc:
        return "ERR", type(exc).__name__


def build(f: dict, order: list[str], style: str) -> str:
    if style == "kv":
        return "".join(f"{k}{f[k]}" for k in order)
    if style == "k_eq_v":
        return "".join(f"{k}={f[k]}" for k in order)
    if style == "k_eq_v_amp":
        return "&".join(f"{k}={f[k]}" for k in order)
    if style == "v":
        return "".join(str(f[k]) for k in order)
    return ""


def run(label: str, f: dict, order: list[str], style: str,
        head: str, tail: str) -> bool:
    raw = build(f, order, style)
    sign = md5(head + raw + tail)
    p = dict(f)
    p["sign"] = sign
    code, msg = post(p)
    hit = code not in ("10006", "ERR")
    print(f"  [{'✅' if hit else '  '}] {label:<44} code={code:<6} {msg}")
    if hit:
        print(f"        sign={sign}")
        print(f"        {json.dumps(p, ensure_ascii=False)[:280]}")
    return hit


def main() -> int:
    orders_no_lang = [
        ("插入序", ["key", "timestamp", "nonce", "data"]),
        ("字典序", ["data", "key", "nonce", "timestamp"]),
        ("逆插入", ["data", "nonce", "timestamp", "key"]),
    ]
    orders_lang = [
        ("插入序+lang", ["key", "timestamp", "nonce", "data", "language"]),
        ("字典序+lang", ["data", "key", "language", "nonce", "timestamp"]),
    ]
    styles = ["kv", "k_eq_v", "k_eq_v_amp"]
    tails = [("+key", "", KEY), ("+secret", "", SECRET),
             ("key+", KEY, ""), ("secret+", SECRET, "")]

    print("=== 固定顺序组 ===\n")
    for oname, order in orders_no_lang:
        f = fresh(False)
        for style in styles[:2]:
            for tname, head, tail in tails:
                label = f"{oname}/{style}/{tname}"
                if run(label, f, order, style, head, tail):
                    print(f"\n*** 找到：{label} ***")
                    return 0
                time.sleep(1.4)
                f = fresh(False)   # 每次新 nonce

    print("\n=== 含 language 组 ===\n")
    for oname, order in orders_lang:
        f = fresh(True)
        for style in styles[:2]:
            for tname, head, tail in tails:
                label = f"{oname}/{style}/{tname}"
                if run(label, f, order, style, head, tail):
                    print(f"\n*** 找到：{label} ***")
                    return 0
                time.sleep(1.4)
                f = fresh(True)

    print("\n未找到。")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
