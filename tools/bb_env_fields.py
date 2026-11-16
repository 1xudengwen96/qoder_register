"""看比特浏览器环境里跟「会话恢复 / 标签页」相关的字段。"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import secrets  # noqa: E402
from app.register.bitbrowser import BitBrowserClient  # noqa: E402

TOP_TOKENS = ("sync", "restore", "startup", "tab", "session", "url",
              "clear", "launch", "window", "cookie")


def main() -> int:
    env_id = sys.argv[1] if len(sys.argv) > 1 else secrets.get("bitbrowser.env_id", "")
    c = BitBrowserClient()
    d = c.call("/browser/detail", {"id": env_id})["data"]
    print(f"环境 {env_id} {d.get('name')}")
    print("\n-- 顶层候选字段 --")
    for k in sorted(d):
        if any(t in k.lower() for t in TOP_TOKENS):
            print(f"  {k:34s} {json.dumps(d[k], ensure_ascii=False)[:90]}")
    fp = d.get("browserFingerPrint") or {}
    print("\n-- 指纹里候选字段 --")
    for k in sorted(fp):
        if any(t in k.lower() for t in TOP_TOKENS):
            print(f"  {k:34s} {json.dumps(fp[k], ensure_ascii=False)[:90]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
