"""探测 api.ipdeep.cn 上 openApi 的真实路径前缀。

判定依据（很可靠）：
  - nginx 未匹配路由 → 404 + text/plain + "Status Code: 404; Not Found"
  - 应用层收到请求 → 200 + application/json（哪怕是错误码）

所以只要能返回 JSON，就说明路径对。

用法：
    python tools/probe_ipdeep_prefix.py
"""

from __future__ import annotations

import sys
import time
import uuid
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

PROXY = {"http": "http://127.0.0.1:7897", "https": "http://127.0.0.1:7897"}
HOST = "https://api.ipdeep.cn"

PREFIXES = [
    "/api/OpenApi/V2",
    "/api/OpenApi",
    "/api/openapi/V2",
    "/api/v2",
    "/api/V2",
    "/openapi/V2",
    "/openapi",
    "/api/Open/V2",
    "/api/Gateway/V2",
    "/api/Api/V2",
]

ACTIONS = ["/User/GetBalance", "/Account/GetBalance", "/User/GetTraffic"]

# 已知能到应用层的对照路径
KNOWN = "/api/Pro/DynamicIp/ExtractDynamicIP"


def call(url: str) -> tuple[bool, str]:
    """返回 (是否到达应用层, 描述)"""
    payload = {
        "key": "probe_key_test",
        "timestamp": int(time.time() * 1000),
        "nonce": str(uuid.uuid4()),
        "data": "",
        "sign": "0" * 32,
    }
    try:
        r = requests.post(url, json=payload, proxies=PROXY, timeout=12)
    except Exception as exc:
        return False, f"{type(exc).__name__}"

    ct = r.headers.get("Content-Type", "")
    if "json" in ct:
        try:
            j = r.json()
            return True, f"JSON ErrCode={j.get('ErrCode')} {str(j.get('ErrMsg'))[:40]}"
        except Exception:
            return True, f"JSON(解析失败) {r.text[:60]}"
    return False, f"{r.status_code} {r.text[:40].strip()}"


def main() -> int:
    print("=== 对照组（已知到应用层）===")
    ok, desc = call(HOST + KNOWN)
    print(f"  {KNOWN:<42} {'应用层' if ok else '未到达'}  {desc}")

    print("\n=== 探测前缀 ===")
    hits: list[tuple[str, str]] = []
    for prefix in PREFIXES:
        for act in ACTIONS:
            path = prefix + act
            ok, desc = call(HOST + path)
            if ok:
                print(f"[HIT ] {path:<42} {desc}")
                hits.append((path, desc))
            else:
                print(f"[    ] {path:<42} {desc}")

    print(f"\n命中 {len(hits)} 个")
    for p, d in hits:
        print(f"  {HOST}{p}  →  {d}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
