"""单次 Baxia 尝试 + 全量日志：点完/提交后组件回什么。"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import secrets  # noqa: E402
from app.net.twocaptcha_click import TwoCaptchaClick, data_url_to_bytes  # noqa: E402
from app.register import cdp_raw  # noqa: E402
from app.register.baxia_cdp import (  # noqa: E402
    JS_CAPTURE,
    JS_STATE,
    points_to_cells,
)
from app.register.bitbrowser import BitBrowserClient  # noqa: E402
from app.register.cdp_page import CDPPage  # noqa: E402

JS_DETAIL = r"""(() => {
  const fr = document.getElementById('baxia-dialog-content');
  if (!fr) return {iframe: false};
  const d = fr.contentDocument;
  const grids = [];
  for (let i = 0; i < 9; i++) {
    const g = d.getElementById('click-grid-' + i);
    if (g) grids.push(i + ':' + (g.getAttribute('style') || '').replace(/\s+/g, ''));
  }
  const sub = d.getElementById('click-submit');
  const r = sub ? sub.getBoundingClientRect() : null;
  return {
    iframe: true,
    text: (d.body ? d.body.innerText : '').slice(0, 300),
    grids,
    submit: r ? {x: Math.round(r.x), y: Math.round(r.y),
                 w: Math.round(r.width), h: Math.round(r.height),
                 disp: getComputedStyle(sub).display} : null,
  };
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
        if data.get("error"):
            print("取图失败", data)
            return 1
        print("布局:", data.get("layout"))
        print("submit:", data.get("submit"))
        print("refresh:", data.get("refresh"))

        cli = TwoCaptchaClick(secrets.get("captcha.twocaptcha_key", ""))
        png = data_url_to_bytes(data["dataUrl"])
        q = data_url_to_bytes(data["questionUrl"]) if data.get("questionUrl") else None
        comment = ("body 图是 3x3 九宫格；imgInstructions 图是题干（中文/图标）。"
                   "请选择所有符合题干的格子，返回格子中心坐标。")
        sol = cli.solve(png, comment, img_instructions=q)
        cells = points_to_cells(sol.points, data.get("layout") or {})
        print(f"2Captcha: points={sol.points} cells={cells} cost=${sol.cost:.4f}")

        by_idx = {g["i"]: g for g in data["gridRects"]}
        for c in cells:
            g = by_idx[c]
            page.click_xy(float(g["x"]), float(g["y"]), human=True)
            time.sleep(0.3)
        print("点后:", json.dumps(page.eval(JS_DETAIL), ensure_ascii=False)[:500])

        sub = page.eval(JS_DETAIL).get("submit")
        if sub:
            fr = page.eval("(() => { const r=document.getElementById("
                           "'baxia-dialog-content').getBoundingClientRect();"
                           "return {x:r.x,y:r.y}; })()")
            page.click_xy(fr["x"] + sub["x"] + sub["w"] / 2,
                          fr["y"] + sub["y"] + sub["h"] / 2, human=True)
        time.sleep(1.5)
        print("提交后:", json.dumps(page.eval(JS_DETAIL), ensure_ascii=False)[:600])
        time.sleep(4)
        print("4 秒后:", json.dumps(page.eval(JS_STATE), ensure_ascii=False)[:400])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
