"""比特浏览器环境与页面状态速查。

    python tools/bb_status.py                # 列环境 + 运行中环境的页面
    python tools/bb_status.py --env-id X     # 只看某个环境
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import secrets  # noqa: E402
from app.register import cdp_raw  # noqa: E402
from app.register.bitbrowser import BitBrowserClient, BitBrowserError  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--env-id", default="")
    ap.add_argument("--pages", action="store_true", help="顺带打开并列出页面")
    args = ap.parse_args()

    client = BitBrowserClient()
    if not client.health():
        print("本地 API 不通（54345）")
        return 2

    envs = ([client.get_env(args.env_id)] if args.env_id
            else client.list_all_envs())
    current = secrets.get("bitbrowser.env_id", "")
    for e in envs:
        mark = "*" if e.id == current else " "
        print(f"{mark} {e.id}  status={e.status}  {e.name:22s} "
              f"proxyMethod={e.proxy_method} {e.host}:{e.port}  "
              f"lastIp={e.raw.get('lastIp')} ({e.raw.get('lastCountry')})")
        if e.status != 1 or not args.pages:
            continue
        try:
            launch = client.open_env(e.id)
        except BitBrowserError as exc:
            print(f"    打不开: {exc}")
            continue
        print(f"    CDP {launch.cdp_http}")
        try:
            for t in cdp_raw.http_targets(launch.cdp_http):
                if t.get("type") == "page":
                    print(f"    page: {(t.get('url') or '')[:100]}")
        except Exception as exc:
            print(f"    列页面失败: {type(exc).__name__}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
