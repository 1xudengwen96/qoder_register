"""测浏览器当前能否出网（出口 IP / 同源请求）。"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import secrets  # noqa: E402
from app.register import cdp_raw  # noqa: E402
from app.register.bitbrowser import BitBrowserClient  # noqa: E402

JS = r"""(async () => {
  const out = {};
  try {
    const r = await fetch('https://api.ip.sb/geoip', {cache: 'no-store'});
    out.geo = await r.text();
  } catch (e) { out.geo = 'ERR ' + String(e).slice(0, 80); }
  try {
    const r2 = await fetch('/api/v1/me', {credentials: 'include'});
    out.same = r2.status + ' ' + (await r2.text()).slice(0, 80);
  } catch (e) { out.same = 'ERR ' + String(e).slice(0, 80); }
  return out;
})()"""


def main() -> int:
    c = BitBrowserClient()
    env_id = secrets.get("bitbrowser.env_id")
    e = c.get_env(env_id)
    print(f"代理 {e.host}:{e.port} user={e.proxy_user}")
    launch = c.open_env(env_id)
    pages = [t for t in cdp_raw.http_targets(launch.cdp_http)
             if t.get("type") == "page"]
    page = pages[0]
    with cdp_raw.CDP(launch.cdp_http, ws_url=page["webSocketDebuggerUrl"],
                     timeout=40) as cdp:
        cdp.enable()
        print("页面:", cdp.current_url())
        print(json.dumps(cdp.evaluate(JS, await_promise=True),
                         ensure_ascii=False, indent=2)[:600])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
