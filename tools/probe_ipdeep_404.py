"""对比 404 响应，判断是哪层服务器的返回，并找 openApi 真实部署位置。"""

from __future__ import annotations

import sys
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

PROXY = {"http": "http://127.0.0.1:7897", "https": "http://127.0.0.1:7897"}

TARGETS = [
    # 已知有效（对照）
    ("GET", "https://api.ipdeep.cn/api/Captcha/GetCaptcha"),
    # 已知返回 JSON 错误（说明应用层可达）
    ("POST", "https://api.ipdeep.cn/api/Pro/DynamicIp/ExtractDynamicIP"),
    # 疑似开放 API
    ("POST", "https://openapi.ipdeep.com/openApi/V2/User/GetBalance"),
    ("POST", "https://api.ipdeep.cn/openApi/V2/User/GetBalance"),
    ("GET", "https://openapi.ipdeep.com/"),
    ("GET", "https://api.ipdeep.cn/"),
]


def main() -> int:
    for method, url in TARGETS:
        print(f"\n=== {method} {url} ===")
        try:
            if method == "GET":
                r = requests.get(url, proxies=PROXY, timeout=12)
            else:
                r = requests.post(url, json={"key": "x"}, proxies=PROXY, timeout=12)
        except Exception as exc:
            print(f"  异常 {type(exc).__name__}: {str(exc)[:80]}")
            continue

        print(f"  status = {r.status_code}")
        print(f"  headers:")
        for k in ("Server", "Content-Type", "Content-Length", "X-Powered-By",
                  "Date", "Via", "X-Cache"):
            if k in r.headers:
                print(f"    {k}: {r.headers[k]}")
        body = r.text[:300]
        print(f"  body: {body!r}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
