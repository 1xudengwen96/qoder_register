"""在 /device/selectAccounts 页点 Continue 完成设备授权，然后确认客户端拿到 token。"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import secrets  # noqa: E402
from app.register import cdp_raw  # noqa: E402
from app.register.bitbrowser import BitBrowserClient  # noqa: E402
from app.register.cdp_page import CDPPage  # noqa: E402


def main() -> int:
    env_id = secrets.get("bitbrowser.env_id")
    client = BitBrowserClient()
    launch = client.open_env(env_id)
    pages = [t for t in cdp_raw.http_targets(launch.cdp_http)
             if t.get("type") == "page"]
    page = next((p for p in pages if "selectAccounts" in (p.get("url") or "")),
                None)
    if not page:
        print("没有 selectAccounts 页面：", [p.get("url") for p in pages])
        return 1

    with cdp_raw.CDP(launch.cdp_http, ws_url=page["webSocketDebuggerUrl"],
                     timeout=60) as raw:
        raw.enable()
        pg = CDPPage.__new__(CDPPage)
        pg.cdp = raw
        pg.default_timeout = 60
        print("URL:", pg.url())
        ok = pg.click_text("Continue", tag="button", human=True)
        print("点 Continue:", ok)
        time.sleep(4)
        print("点后 URL:", pg.url())
        print("页面文本:", pg.text()[:200].replace("\n", " | "))

    time.sleep(3)
    from app.claim.token_harvester import harvest_token

    tok = harvest_token()
    if tok:
        print(f"客户端已拿到 token: {tok.token[:16]}… source={tok.source}")
        return 0
    print("客户端还没写 token，稍后再试")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
