"""验证当前比特浏览器环境的出口：代理字段、页面内 IP、lastIp。

    python tools/bb_verify_ip.py                  # 用配置里的环境
    python tools/bb_verify_ip.py --env-id X
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import secrets  # noqa: E402
from app.register import cdp_raw  # noqa: E402
from app.register.bitbrowser import BitBrowserClient, BitBrowserError  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--env-id", default=secrets.get("bitbrowser.env_id", ""))
    ap.add_argument("--probe-url", default="https://api.ip.sb/geoip")
    args = ap.parse_args()

    client = BitBrowserClient()
    if not client.health():
        print("本地 API 不通")
        return 2

    e = client.get_env(args.env_id)
    print(f"环境 {e.id} {e.name}")
    print(f"  代理 proxyMethod={e.proxy_method} {e.host}:{e.port} "
          f"user={e.proxy_user}")
    print(f"  lastIp={e.raw.get('lastIp')} ({e.raw.get('lastCountry')})")

    try:
        launch = client.open_env(e.id)
    except BitBrowserError as exc:
        print(f"启动失败: {exc} code={exc.code}")
        return 1
    print(f"  CDP {launch.cdp_http}")

    # 先测代理本身通不通（不经浏览器，直连 gate）
    import requests

    proxy_url = f"http://{e.proxy_user}:{e.proxy_password}@{e.host}:{e.port}"
    try:
        r = requests.get("https://api.ip.sb/geoip", timeout=25,
                         proxies={"http": proxy_url, "https": proxy_url})
        j = r.json()
        print(f"  代理直测  {j.get('ip')} {j.get('country_code')} "
              f"{j.get('city')} {j.get('timezone')}")
    except Exception as exc:
        print(f"  代理直测失败: {type(exc).__name__}: {exc}")

    try:
        with cdp_raw.CDP(launch.cdp_http) as page:
            page.enable()
            page.navigate("about:blank", timeout=30)
            probe = page.evaluate(cdp_raw.PROBE_JS) or {}
            print("\n  浏览器内实测:")
            for k in ("ip", "ipCountry", "ipTz", "timezone", "languages",
                      "webdriver", "ua"):
                print(f"    {k:12s} {probe.get(k)}")
            if probe.get("ipError"):
                print(f"    ipError      {probe['ipError']}")
    except Exception as exc:
        print(f"  CDP 探测失败: {type(exc).__name__}: {exc}")

    e2 = client.get_env(e.id)
    print(f"\n  lastIp(启动后)={e2.raw.get('lastIp')} "
          f"({e2.raw.get('lastCountry')})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
