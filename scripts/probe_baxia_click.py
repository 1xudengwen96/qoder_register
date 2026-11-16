"""点一个格子，看它有没有选中态；并确认 submit 坐标对不对。"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import secrets  # noqa: E402
from app.register import cdp_raw  # noqa: E402
from app.register.baxia_cdp import JS_CAPTURE  # noqa: E402
from app.register.bitbrowser import BitBrowserClient  # noqa: E402
from app.register.cdp_page import CDPPage  # noqa: E402

JS_STATE = r"""(() => {
  const d = document.getElementById('baxia-dialog-content').contentDocument;
  const gs = [];
  for (let i = 0; i < 9; i++) {
    const g = d.getElementById('click-grid-' + i);
    if (!g) continue;
    gs.push({i, cls: String(g.className || ''), style: g.getAttribute('style') || ''});
  }
  const sub = d.getElementById('click-submit');
  const r = sub ? sub.getBoundingClientRect() : null;
  return {grids: gs, text: (d.body ? d.body.innerText : '').slice(0, 200),
          submit: r ? {x: r.x, y: r.y, w: r.width, h: r.height,
                       cls: String(sub.className || '')} : null};
})()"""


def main() -> int:
    endpoint = BitBrowserClient().open_env(
        secrets.get("bitbrowser.env_id")).cdp_http
    pages = [t for t in cdp_raw.http_targets(endpoint) if t.get("type") == "page"]
    target = next((t for t in pages if "sign-up" in (t.get("url") or "")), pages[0])
    with cdp_raw.CDP(endpoint, ws_url=target["webSocketDebuggerUrl"]) as raw:
        raw.enable()
        page = CDPPage.__new__(CDPPage)
        page.cdp = raw
        page.default_timeout = 60

        data = page.eval(JS_CAPTURE) or {}
        grids = data.get("gridRects") or []
        print("格子中心:", [(g["i"], round(g["x"]), round(g["y"])) for g in grids])
        print("submit:", data.get("submit"))
        before = page.eval(JS_STATE)
        print("点前:", json.dumps(before, ensure_ascii=False)[:300])

        g0 = grids[0]
        print(f"点击 格0 ({g0['x']:.0f},{g0['y']:.0f})")
        page.click_xy(float(g0["x"]), float(g0["y"]), human=True)
        time.sleep(1.2)
        after = page.eval(JS_STATE)
        print("点后:", json.dumps(after, ensure_ascii=False)[:400])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
