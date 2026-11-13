"""关闭比特浏览器环境（配置默认值）。"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import secrets  # noqa: E402
from app.register.bitbrowser import BitBrowserClient  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--env-id", default=secrets.get("bitbrowser.env_id", ""))
    ap.add_argument("--all", action="store_true", help="关闭全部环境")
    args = ap.parse_args()

    client = BitBrowserClient()
    if args.all:
        print("close_all:", client.close_all())
        return 0
    if not args.env_id:
        print("缺 env id")
        return 2
    ok = client.close_env(args.env_id)
    e = client.get_env(args.env_id)
    print(f"close {args.env_id}: {ok}  status={e.status}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
