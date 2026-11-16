"""在已登录页面里抓真实 API：导航 /account 相关页，记录所有 /api/ 请求与响应。"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import secrets  # noqa: E402
from app.register import cdp_raw  # noqa: E402
from app.register.bitbrowser import BitBrowserClient  # noqa: E402

PAGES = ("/account/profile", "/account", "/account/usage", "/account/plan")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--env-id", default=secrets.get("bitbrowser.env_id", ""))
    args = ap.parse_args()

    client = BitBrowserClient()
    launch = client.open_env(args.env_id)
    pages = [t for t in cdp_raw.http_targets(launch.cdp_http)
             if t.get("type") == "page"]
    page = next((t for t in pages if "qoder.com" in (t.get("url") or "")),
                pages[0] if pages else None)
    if not page:
        print("没有页面")
        return 1

    with cdp_raw.CDP(launch.cdp_http, ws_url=page["webSocketDebuggerUrl"],
                     timeout=90) as cdp:
        cdp.enable()
        print("起始页:", cdp.current_url())
        for path in PAGES:
            url = "https://qoder.com" + path
            try:
                cdp.navigate(url, timeout=45)
            except Exception as exc:
                print(f"  导航 {path} 失败: {type(exc).__name__}")
                continue
            time.sleep(4)
            print(f"\n=== {path} -> {cdp.current_url()} ===")
            body = cdp.evaluate("document.body ? document.body.innerText.slice(0,200) : ''")
            print("  文本:", (body or "").replace("\n", " | ")[:160])

        print("\n=== 捕获到的 /api/ 请求 ===")
        seen = set()
        for ev in cdp.events:
            method = ev.get("method")
            params = ev.get("params") or {}
            if method == "Network.requestWillBeSent":
                req = params.get("request") or {}
                u = str(req.get("url") or "")
                if "/api/" in u and u not in seen:
                    seen.add(u)
                    print(f"  {req.get('method')} {u[:130]}")
            elif method == "Network.responseReceived":
                resp = params.get("response") or {}
                u = str(resp.get("url") or "")
                if "/api/" in u:
                    print(f"      -> {resp.get('status')} {u[:100]}")

        # cookie 快照
        try:
            res = cdp.call("Network.getCookies",
                           {"urls": ["https://qoder.com"]})
            names = [c["name"] for c in res.get("cookies") or []]
            print("\ncookie:", names)
        except Exception:
            pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
