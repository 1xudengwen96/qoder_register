"""测试 IPDEEP 提取链接能不能换国家（改用户名里的 -country-XX-）。

IPDEEP 的网关用「用户名编码配置」：``<uid>-res-country-JP-session-...``。
所以只要把国家段换掉，不用重新生成链接。

    python tools/ipdeep_country_test.py --url "https://api.ipdeep.com/..."
"""

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
    ap.add_argument("--countries", default="JP,US,SG,GB")
    ap.add_argument("--probe", default="https://api.ip.sb/geoip")
    args = ap.parse_args()

    r = requests.get(args.url, timeout=30)
    r.raise_for_status()
    got = parse_proxy_payload(r.text)
    if not got:
        print(f"解析不出代理: {r.text[:300]}")
        return 2
    p = got[0]
    print(f"原始: {p.host}:{p.port} user={p.username} pass={p.password}")
    if not re.search(r"country-[A-Za-z]{2}", p.username or ""):
        print("用户名里没有 country-XX 段，这个链接不能换国家")
        return 2

    for cc in [c.strip().upper() for c in args.countries.split(",") if c.strip()]:
        user = re.sub(r"country-[A-Za-z]{2}", f"country-{cc}", p.username)
        proxy = f"http://{user}:{p.password}@{p.host}:{p.port}"
        try:
            resp = requests.get(args.probe, timeout=30,
                                proxies={"http": proxy, "https": proxy})
            j = resp.json()
            print(f"{cc}: {j.get('ip'):16s} {j.get('country_code')} "
                  f"{j.get('city')} {j.get('timezone')}")
        except Exception as exc:
            print(f"{cc}: FAIL {type(exc).__name__}: {str(exc)[:80]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
