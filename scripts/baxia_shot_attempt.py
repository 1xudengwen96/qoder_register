"""用真实截图（而非 canvas 导出）解 Baxia 九宫格。

截图区域：
- body：九宫格容器（坐标按它算）
- imgInstructions：题面区域（题干文字 + 目标图案的渲染结果）
"""

from __future__ import annotations

import base64
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import secrets  # noqa: E402
from app.net.twocaptcha_click import TwoCaptchaClick  # noqa: E402
from app.register import cdp_raw  # noqa: E402
from app.register.baxia_cdp import JS_CENTERS, JS_STATE  # noqa: E402
from app.register.bitbrowser import BitBrowserClient  # noqa: E402
from app.register.cdp_page import CDPPage  # noqa: E402

JS_RECTS = r"""(() => {
  const fr = document.getElementById('baxia-dialog-content');
  if (!fr) return {error: 'no-iframe'};
  const d = fr.contentDocument;
  const frR = fr.getBoundingClientRect();
  const cont = d.getElementById('click-captcha-question-container');
  const cr = cont ? cont.getBoundingClientRect() : null;
  return {
    iframe: {x: frR.left, y: frR.top, w: frR.width, h: frR.height},
    grid: cr ? {x: frR.left + cr.left, y: frR.top + cr.top,
                w: cr.width, h: cr.height} : null,
  };
})()"""


def shot(page: CDPPage, clip: dict, scale: float) -> bytes:
    res = page.cdp.call("Page.captureScreenshot", {
        "format": "jpeg", "quality": 88,
        "clip": {"x": clip["x"], "y": clip["y"], "width": clip["w"],
                 "height": clip["h"], "scale": scale},
    })
    return base64.b64decode(res["data"])


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

        rects = page.eval(JS_RECTS) or {}
        print("rects:", json.dumps(rects, ensure_ascii=False))
        if not rects.get("grid"):
            print("没找到九宫格")
            return 1
        grid = rects["grid"]
        ifr = rects["iframe"]
        body = shot(page, grid, 1.6)
        instruct = shot(page, {"x": ifr["x"], "y": ifr["y"],
                               "w": ifr["w"], "h": 120}, 3.0)
        print(f"body {len(body)}B  instr {len(instruct)}B")
        Path("data/artifacts/baxia_shot_body.jpg").write_bytes(body)
        Path("data/artifacts/baxia_shot_instr.jpg").write_bytes(instruct)

        cli = TwoCaptchaClick(secrets.get("captcha.twocaptcha_key", ""))
        comment = ("body 图是 3x3 九宫格照片；imgInstructions 图是题面（文字+目标图案）。"
                   "请选出所有包含目标图案的格子，返回格子中心在 body 图中的坐标。")
        sol = cli.solve(body, comment, img_instructions=instruct)
        cell = grid["w"] / 3.0
        cells = []
        for x, y in sol.points:
            col = int(x // cell)
            row = int(y // cell)
            if 0 <= col < 3 and 0 <= row < 3:
                idx = row * 3 + col
                if idx not in cells:
                    cells.append(idx)
        print(f"2Captcha: {sol.points} -> cells {cells} cost=${sol.cost:.4f}")

        # 点击
        for idx in cells:
            c = page.eval(JS_CENTERS) or {}
            g = {int(x["i"]): x for x in (c.get("grids") or [])}.get(idx)
            if g:
                page.click_xy(float(g["x"]), float(g["y"]), human=True)
                time.sleep(0.45)
        sel = (page.eval(JS_CENTERS) or {}).get("selected")
        print("已选:", sel)
        c = page.eval(JS_CENTERS) or {}
        sub = c.get("submit")
        if sub:
            page.click_xy(float(sub["x"]), float(sub["y"]), human=True)
        for _ in range(16):
            time.sleep(0.5)
            st = page.eval(JS_STATE) or {}
            if st.get("x5sec") or not st.get("iframe"):
                print("PASS 通过", st)
                return 0
        print("未通过:", json.dumps(page.eval(JS_STATE), ensure_ascii=False)[:300])
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
