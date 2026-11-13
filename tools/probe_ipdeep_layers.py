"""逐层验证 IPDEEP openApi 路径层级，定位 404 发生在哪一层。"""

from __future__ import annotations

import sys
import time
import uuid
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

PROXY = {"http": "http://127.0.0.1:7897", "https": "http://127.0.0.1:7897"}
BASE = "https://openapi.ipdeep.com"

# 逐层递进，看哪一层开始 404
LAYERS = [
    "/",
    "/openApi",
    "/openApi/",
    "/openApi/V2",
    "/openApi/V2/",
    "/openApi/V2/User",
    "/openApi/V2/User/",
    "/openApi/V2/User/GetBalance",
    "/openapi/V2/User/GetBalance",
    "/OpenApi/V2/User/GetBalance",
    "/openApi/v2/User/GetBalance",
    "/openApi/V2/Account/GetBalance",
    "/openApi/V2/User/QueryBalance",
    "/openApi/V2/User/Balance",
    "/openApi/V2/DynamicProxy/GetCountries",
    "/openApi/V2/Proxy/GetCountries",
    "/openApi/V2/Common/GetCountries",
]


def probe(path: str) -> str:
    url = BASE + path
    payload = {
        "key": "probe_key_test",
        "timestamp": int(time.time() * 1000),
        "nonce": str(uuid.uuid4()),
        "data": "",
        "language": "zh",
        "sign": "0" * 32,
    }
    try:
        r = requests.post(url, json=payload, proxies=PROXY, timeout=12)
    except Exception as exc:
        return f"{type(exc).__name__}: {str(exc)[:50]}"

    body = r.text[:100].replace("\n", " ")
    if "404" in str(r.status_code) or "NotFound" in body:
        return f"{r.status_code} 404"
    return f"{r.status_code} {body}"


def main() -> int:
    for p in LAYERS:
        print(f"{p:<45} {probe(p)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
