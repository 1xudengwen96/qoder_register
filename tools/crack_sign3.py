"""精确破解 IPDEEP sign 算法。

上一轮的教训：用同一个 nonce 测多个签名，第二个会返回 10005（重复请求），
误以为签名通过。必须**每个变体都用全新 nonce + 全新 timestamp**。

判定标准：
  - 10006 → 签名错
  - 其它（0 / 10005 / 10008 / ...）→ 签名对，需要进一步看

用法：
    python tools/crack_sign3.py
"""

from __future__ import annotations

import os

import base64
import hashlib
import hmac
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


def fresh_payload(with_lang: bool = False) -> dict:
    p = {
        "key": KEY,
        "timestamp": int(time.time() * 1000),
        "nonce": str(uuid.uuid4()),
        "data": encrypt(b"{}"),
    }
    if with_lang:
        p["language"] = "zh"
    return p


def md5(s: str | bytes) -> str:
    b = s.encode() if isinstance(s, str) else s
    return hashlib.md5(b).hexdigest()


def sha256(s: str | bytes) -> str:
    b = s.encode() if isinstance(s, str) else s
    return hashlib.sha256(b).hexdigest()


def post(payload: dict) -> tuple[str, str]:
    try:
        r = requests.post(HOST + "/Account/balance", json=payload,
                          proxies=PROXY, timeout=15)
        j = r.json()
        return str(j.get("ErrCode", "")), str(j.get("ErrMsg", ""))[:45]
    except Exception as exc:
        return "ERR", type(exc).__name__


# ---------- 各种签名构造 ----------
def s_kv_tail(f: dict, tail: str, algo=md5) -> str:
    raw = "".join(f"{k}{f[k]}" for k in sorted(f))
    return algo(raw + tail)


def s_kvhead(f: dict, head: str) -> str:
    raw = "".join(f"{k}{f[k]}" for k in sorted(f))
    return md5(head + raw)


def s_urlenc(f: dict, tail: str) -> str:
    raw = "&".join(f"{k}={f[k]}" for k in sorted(f))
    return md5(raw + tail)


def s_urlenc_keyval(f: dict) -> str:
    raw = "&".join(f"{k}={f[k]}" for k in sorted(f)) + f"&key={KEY}"
    return md5(raw)


def s_hmac(f: dict, secret: str, algo=md5) -> str:
    raw = "".join(f"{k}{f[k]}" for k in sorted(f))
    return hmac.new(secret.encode(), raw.encode(), hashlib.md5).hexdigest()


def s_json(f: dict, tail: str) -> str:
    return md5(json.dumps(f, sort_keys=True, separators=(",", ":")) + tail)


def s_vonly(f: dict, tail: str) -> str:
    return md5("".join(str(f[k]) for k in sorted(f)) + tail)


VARIANTS = [
    ("kv+key", lambda f: s_kv_tail(f, KEY)),
    ("kv+secret", lambda f: s_kv_tail(f, SECRET)),
    ("kv+key 大写", lambda f: s_kv_tail(f, KEY).upper()),
    ("kv+secret 大写", lambda f: s_kv_tail(f, SECRET).upper()),
    ("key+kv", lambda f: s_kvhead(f, KEY)),
    ("secret+kv", lambda f: s_kvhead(f, SECRET)),
    ("k=v& + key", lambda f: s_urlenc(f, KEY)),
    ("k=v& + secret", lambda f: s_urlenc(f, SECRET)),
    ("k=v& &key=KEY", s_urlenc_keyval),
    ("HMAC-md5(secret)", lambda f: s_hmac(f, SECRET)),
    ("HMAC-md5(key)", lambda f: s_hmac(f, KEY)),
    ("json+key", lambda f: s_json(f, KEY)),
    ("json+secret", lambda f: s_json(f, SECRET)),
    ("vonly+key", lambda f: s_vonly(f, KEY)),
    ("vonly+secret", lambda f: s_vonly(f, SECRET)),
    ("kv+secret sha256", lambda f: s_kv_tail(f, SECRET, sha256)),
    ("kv+key sha256", lambda f: s_kv_tail(f, KEY, sha256)),
]

VARIANTS_LANG = [
    ("含lang kv+key", lambda f: s_kv_tail(f, KEY)),
    ("含lang kv+secret", lambda f: s_kv_tail(f, SECRET)),
]


def run(label: str, signer, with_lang: bool = False) -> bool:
    f = fresh_payload(with_lang)
    sign = signer(f)
    p = dict(f)
    p["sign"] = sign
    code, msg = post(p)
    hit = code != "10006" and code != "ERR"
    print(f"  [{'✅' if hit else '  '}] {label:<22} code={code:<6} {msg}")
    if hit:
        print(f"        sign={sign}")
        print(f"        payload={json.dumps(p, ensure_ascii=False)[:300]}")
    return hit


def main() -> int:
    print(f"=== 无 language 组（{len(VARIANTS)} 种，每个用新 nonce）===\n")
    for label, signer in VARIANTS:
        if run(label, signer):
            print(f"\n*** 找到：{label} ***")
            return 0
        time.sleep(1.4)

    print(f"\n=== 含 language 组 ===\n")
    for label, signer in VARIANTS_LANG:
        if run(label, signer, with_lang=True):
            print(f"\n*** 找到：{label} ***")
            return 0
        time.sleep(1.4)

    print("\n未找到。")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
