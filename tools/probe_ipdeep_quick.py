"""快速验证 IPDEEP openApi/V2 基地址是否正确。"""

from __future__ import annotations

import sys
import time
import uuid
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

PROXY = {"http": "http://127.0.0.1:7897", "https": "http://127.0.0.1:7897"}
BASES = [
    "https://openapi.ipdeep.com/openApi/V2",
    "https://openapi.ipdeep.com/openApi/V1",
    "https://api.ipdeep.cn/openApi/V2",
]
PATHS = ["/User/GetBalance", "/Account/GetBalance", "/User/GetTraffic"]


def probe(url: str) -> str:
    payload = {
        "key": "probe_key_test",
        "timestamp": int(time.time() * 1000),
        "nonce": str(uuid.uuid4()),
        "data": "",
        "language": "zh",
        "sign": "0" * 32,
    }
    try:
        r = requests.post(url, json=payload, proxies=PROXY, timeout=15)
    except Exception as exc:
        return f"{type(exc).__name__}: {str(exc)[:70]}"

    try:
        j = r.json()
        return (f"status={r.status_code} ErrCode={j.get('ErrCode')} "
                f"ErrMsg={str(j.get('ErrMsg'))[:50]}")
    except Exception:
        return f"status={r.status_code} text={r.text[:80]}"


def main() -> int:
    for base in BASES:
        print(f"\n--- {base} ---")
        for p in PATHS:
            print(f"  {p:<26} {probe(base + p)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
