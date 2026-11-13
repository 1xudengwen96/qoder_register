"""看 IPDEEP 某个国家返回的原始内容（排查 US/GB 不可用）。"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import requests  # noqa: E402

from app.net.ipdeep import parse_proxy_payload  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", required=True)
    ap.add_argument("--country", default="US")
    ap.add_argument("--show", default="https://api.ip.sb/geoip")
    args = ap.parse_args()

    r = requests.get(args.url, timeout=30)
    p = parse_proxy_payload(r.text)[0]
    user = re.sub(r"country-[A-Za-z]{2}", f"country-{args.country.upper()}",
                  p.username)
    proxy = f"http://{user}:{p.password}@{p.host}:{p.port}"
    print(f"用户名: {user}")
    try:
        resp = requests.get(args.show, timeout=30,
                            proxies={"http": proxy, "https": proxy})
        print(f"HTTP {resp.status_code}")
        print(resp.text[:400])
    except Exception as exc:
        print(f"FAIL {type(exc).__name__}: {exc}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
