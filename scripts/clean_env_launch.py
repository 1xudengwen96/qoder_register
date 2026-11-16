"""把环境的「启动前清理」打开：清 cookie / 缓存 / 历史，保证每轮全新身份。"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import secrets  # noqa: E402
from app.register.bitbrowser import BitBrowserClient  # noqa: E402


def main() -> int:
    env_id = sys.argv[1] if len(sys.argv) > 1 else secrets.get("bitbrowser.env_id")
    c = BitBrowserClient()
    if c.get_env(env_id).status == 1:
        c.close_env(env_id)
    c.update_env(env_id,
                 clearCookiesBeforeLaunch=True,
                 clearCacheFilesBeforeLaunch=True,
                 clearHistoriesBeforeLaunch=True)
    e = c.get_env(env_id)
    print("cookie:", e.raw.get("clearCookiesBeforeLaunch"),
          "cache:", e.raw.get("clearCacheFilesBeforeLaunch"),
          "history:", e.raw.get("clearHistoriesBeforeLaunch"))
    # 顺便把这些字段也确认在指纹里
    fp = e.raw.get("browserFingerPrint") or {}
    print("fp cookie:", fp.get("clearCookiesBeforeLaunch"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
