"""探测 IPDEEP 设置动态代理凭据的接口，并尝试配置。

背景：
  /api/Pro/BasicData/GetCountryList 能通 → API 认证结构正确
  /api/Pro/DynamicIp/ExtractDynamicIP 报 "Invalid credential type"
  → 账号还没配置动态代理的认证方式（动态代理密码 / IP白名单）

需要找到设置接口：
  - 设置动态代理密码：UpdateDynamicPwd / SetDynamicPwd
  - 添加 IP 白名单：InsertIpWhitelist / AddIpWhitelist

用法：
    python tools/probe_ipdeep_pwd.py --key X --secret Y
    python tools/probe_ipdeep_pwd.py --key X --secret Y --set-pwd YourPwd123
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import itertools
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

CANDIDATES = [
    ("User", "UpdateDynamicPwd"),
    ("User", "SetDynamicPwd"),
    ("User", "GetApiKeyInfo"),
    ("User", "GetUserInfo"),
    ("User", "GetDynamicPwd"),
    ("Security", "UpdateDynamicPwd"),
    ("Security", "GetApiKeyInfo"),
    ("DynamicIp", "UpdateDynamicPwd"),
    ("DynamicIp", "GetCredentialType"),
    ("DynamicIp", "SetCredentialType"),
    ("IpWhitelist", "GetIpWhitelistList"),
    ("IpWhitelist", "InsertIpWhitelist"),
    ("IpWhitelist", "AddIpWhitelist"),
    ("BasicData", "GetCredentialType"),
    ("Account", "GetBalance"),
    ("Account", "GetUserInfo"),
]


def build(key: str, secret: str, biz: dict | None = None) -> dict:
    aes_key = hashlib.sha256(f"{key}{secret}".encode()).hexdigest()[:32].encode()
    plain = json.dumps(biz or {}, separators=(",", ":")).encode()
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
        r = requests.post(HOST + path, json=payload, proxies=PROXY, timeout=15)
    except Exception as exc:
        return "ERR", type(exc).__name__, ""
    ct = r.headers.get("Content-Type", "")
    if "json" not in ct:
        return ("404" if r.status_code == 404 else str(r.status_code)), "", ""
    j = r.json()
    return (str(j.get("ErrCode", "")), str(j.get("ErrMsg", ""))[:50],
            json.dumps(j.get("ResData"), ensure_ascii=False)[:150])


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--key", required=True)
    ap.add_argument("--secret", required=True)
    ap.add_argument("--set-pwd", default="", help="设置动态代理密码")
    args = ap.parse_args()

    print("=== 探测可用接口 ===\n")
    alive: list[tuple[str, str, str]] = []
    for ctrl, act in CANDIDATES:
        path = f"/api/Pro/{ctrl}/{act}"
        code, msg, data = probe(path, build(args.key, args.secret))
        if code == "404":
            continue
        print(f"[{code:<5}] {path:<44} {msg} {data[:60]}")
        alive.append((path, code, msg))

    print(f"\n有响应 {len(alive)} 个")

    # 尝试设置动态代理密码
    if args.set_pwd:
        print(f"\n=== 设置动态代理密码 ===")
        pwd_variants = [
            {"password": args.set_pwd},
            {"dynamicPwd": args.set_pwd},
            {"pwd": args.set_pwd},
            {"password": args.set_pwd, "confirmPassword": args.set_pwd},
        ]
        targets = [p for p, c, _ in alive if "Pwd" in p]
        for path in targets:
            for v in pwd_variants:
                code, msg, data = probe(path, build(args.key, args.secret, v))
                print(f"  {path:<40} {v} → code={code} {msg} {data[:50]}")
                if code == "0":
                    print(f"  ✓ 设置成功！")
                    return 0

        # 若没有 Pwd 接口，试白名单
        print("\n=== 尝试加 IP 白名单 ===")
        import urllib.request
        try:
            ip = urllib.request.urlopen("https://api.ipify.org", timeout=10).read().decode()
            print(f"  当前出口 IP: {ip}")
        except Exception as exc:
            ip = ""
            print(f"  取 IP 失败: {exc}")

        if ip:
            for path, _, _ in alive:
                if "Whitelist" in path and ("Insert" in path or "Add" in path):
                    for v in ({"ip": ip}, {"ipAddress": ip}, {"ip": ip, "remark": "auto"}):
                        code, msg, data = probe(path, build(args.key, args.secret, v))
                        print(f"  {path:<40} {v} → code={code} {msg}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
