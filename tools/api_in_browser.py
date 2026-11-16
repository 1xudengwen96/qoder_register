"""在浏览器会话内调 Qoder 接口（同源、带 cookie、指纹可信）。

    python tools/api_in_browser.py
    python tools/api_in_browser.py --path /api/v1/users/me
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
from app.register.bitbrowser import BitBrowserClient  # noqa: E402

JS_FETCH = r"""(async (opts) => {
  const out = [];
  for (const p of opts.paths) {
    const url = p.startsWith('http') ? p : ('https://qoder.com' + p);
    try {
      const r = await fetch(url, {credentials: 'include',
                                  headers: {'Accept': 'application/json'}});
      const t = await r.text();
      out.push({path: p, status: r.status, body: t.slice(0, 240)});
    } catch (e) {
      out.push({path: p, status: 0, body: 'ERR ' + String(e).slice(0, 120)});
    }
  }
  return out;
})"""

DEFAULT_PATHS = [
    "/api/v1/users/me",
    "/api/v1/users/profile",
    "/api/v1/user/info",
    "/api/v1/users/current",
    "/api/v1/credits/balance",
    "/api/v1/subscription",
]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--env-id", default=secrets.get("bitbrowser.env_id", ""))
    ap.add_argument("--path", action="append", default=None)
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

    with cdp_raw.CDP(launch.cdp_http, ws_url=page["webSocketDebuggerUrl"]) as cdp:
        cdp.enable()
        print("页面:", cdp.current_url())
        expr = f"({JS_FETCH})({json.dumps({'paths': args.path or DEFAULT_PATHS})})"
        rows = cdp.evaluate(expr, await_promise=True) or []
        for r in rows:
            b = (r.get("body") or "").replace("\n", " ")
            flag = "OK " if r.get("status") == 200 else "   "
            print(f"{flag}{r.get('path'):28s} {r.get('status')} {b[:160]}")
        print("\n网络事件（本次）:")
        for ev in cdp.events[-40:]:
            if ev.get("method") != "Network.responseReceived":
                continue
            resp = (ev.get("params") or {}).get("response") or {}
            u = str(resp.get("url") or "")
            if "/api/" in u:
                print(f"  {resp.get('status')} {u[:110]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
