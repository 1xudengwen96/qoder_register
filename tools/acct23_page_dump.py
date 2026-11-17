"""dump acct23 环境 usage / profile 页原始文本，核对额度事实。"""
from __future__ import annotations

import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.register.bitbrowser import BitBrowserClient  # noqa: E402
from app.register.cdp_page import new_page  # noqa: E402


def main() -> int:
    client = BitBrowserClient()
    env = next((e for e in client.list_all_envs() if e.name == "acct23"), None)
    launch = client.open_env(env.id)
    page = new_page(launch.cdp_http, "about:blank")
    for url in ("https://qoder.com/account/usage",
                "https://qoder.com/account/profile"):
        page.goto(url, wait_ms=5000)
        time.sleep(2)
        text = page.text() or ""
        out = ROOT / "data" / "acct23_page.txt"
        with out.open("a", encoding="utf-8") as fh:
            fh.write(f"\n===== {url} =====\n")
            fh.write(text[:2500])
            fh.write("\n")
        print(f"===== {url} =====")
        print(text[:2500])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
