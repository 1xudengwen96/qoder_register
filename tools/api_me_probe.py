"""调已确认的 Qoder 会话接口：/api/v1/me、userplan、big_model_credits。"""

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

PATHS = (
    "/api/v1/me",
    "/api/v1/me/userplan",
    "/api/v2/me/usages/big_model_credits",
    "/api/v1/me/sessions?page=1&page_size=5",
)

JS = r"""(async (paths) => {
  const out = [];
  for (const p of paths) {
    try {
      const r = await fetch(p, {credentials: 'include',
                                headers: {'Accept': 'application/json'}});
      const t = await r.text();
      out.push({path: p, status: r.status, body: t});
    } catch (e) {
      out.push({path: p, status: 0, body: 'ERR ' + String(e).slice(0, 120)});
    }
  }
  return out;
})"""


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--env-id", default=secrets.get("bitbrowser.env_id", ""))
    args = ap.parse_args()

    client = BitBrowserClient()
    launch = client.open_env(args.env_id)
    pages = [t for t in cdp_raw.http_targets(launch.cdp_http)
             if t.get("type") == "page"]
    page = next((t for t in pages if "qoder.com" in (t.get("url") or "")),
                pages[0])
    with cdp_raw.CDP(launch.cdp_http, ws_url=page["webSocketDebuggerUrl"],
                     timeout=90) as cdp:
        cdp.enable()
        print("页面:", cdp.current_url())
        rows = cdp.evaluate(f"({JS})({json.dumps(list(PATHS))})",
                            await_promise=True) or []
        for r in rows:
            print(f"\n=== {r.get('path')} -> {r.get('status')} ===")
            body = r.get("body") or ""
            try:
                obj = json.loads(body)
                print(json.dumps(obj, ensure_ascii=False, indent=2)[:1200])
            except Exception:
                print(body[:400])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
