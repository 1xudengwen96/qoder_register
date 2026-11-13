"""探测 IPDEEP 开放 API 的真实 Controller/Action。

原理：路径不存在 → 404；路径存在但签名错 → ErrCode 10006。
所以用"故意错的签名"去探，能通过 404/非404 区分出真实路径，不依赖签名正确。

基地址：https://openapi.ipdeep.com/openApi/V2/{Controller}/{Action}

用法：
    python tools/probe_ipdeep_v2.py
"""

from __future__ import annotations

import itertools
import sys
import time
import uuid
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

BASE = "https://openapi.ipdeep.com/openApi/V2"
PROXY = {"http": "http://127.0.0.1:7897", "https": "http://127.0.0.1:7897"}

CONTROLLERS = [
    "User", "Account", "DynamicProxy", "DynamicIp", "StaticProxy",
    "Order", "WhiteList", "IpWhitelist", "Proxy", "OpenApi",
]

ACTIONS = [
    "GetBalance", "GetTraffic", "GetUserInfo",
    "GetCountries", "GetCountryList",
    "GetStates", "GetStateList",
    "GetCities", "GetCityList",
    "GenerateProxyInfo", "GenerateProxy", "GenerateLink", "GenerateProxyLink",
    "GetProxyInfo", "ExtractProxy", "Extract",
    "GetIpList", "GetStaticIpList",
    "GetWhitelist", "AddWhitelist",
]


def probe(path: str, api_key: str = "probe_key") -> tuple[int, str, str]:
    payload = {
        "key": api_key,
        "timestamp": int(time.time() * 1000),
        "nonce": str(uuid.uuid4()),
        "data": "",
        "language": "zh",
        "sign": "0" * 32,   # 故意错的签名
    }
    try:
        r = requests.post(BASE + path, json=payload, proxies=PROXY, timeout=12)
    except Exception as exc:
        return -1, "", f"{type(exc).__name__}"

    if r.status_code == 404:
        return 404, "", ""
    try:
        j = r.json()
        return r.status_code, str(j.get("ErrCode", "")), str(j.get("ErrMsg", ""))[:60]
    except Exception:
        return r.status_code, "", r.text[:60].replace("\n", " ")


def main() -> int:
    print(f"=== 探测 {BASE}/{{Controller}}/{{Action}} ===\n")

    hits: list[tuple[str, str, str]] = []
    total = len(CONTROLLERS) * len(ACTIONS)
    n = 0

    for ctrl, act in itertools.product(CONTROLLERS, ACTIONS):
        n += 1
        path = f"/{ctrl}/{act}"
        status, code, msg = probe(path)

        if status == 404:
            continue

        # 非 404 都是命中
        hits.append((path, code, msg))
        mark = "HIT "
        print(f"[{mark}] {path:<38} status={status} code={code} {msg}")

    print(f"\n探测 {n} 个组合，命中 {len(hits)} 个")
    if hits:
        print("\n=== 命中清单 ===")
        for p, code, msg in hits:
            print(f"  {BASE}{p}   code={code}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
