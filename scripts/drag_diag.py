"""拖动诊断：按下 → 移动 → 读拼图/滑块的状态变化，判断事件有没有被接收。"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import secrets  # noqa: E402
from app.register import cdp_raw  # noqa: E402
from app.register.aliyun_slider_cdp import AliyunSliderCDP  # noqa: E402
from app.register.bitbrowser import BitBrowserClient  # noqa: E402
from app.register.cdp_page import CDPPage  # noqa: E402

JS_STATE = r"""(() => {
  const p = document.querySelector('#aliyunCaptcha-puzzle');
  const s = document.querySelector('#aliyunCaptcha-sliding-slider');
  const b = document.querySelector('#aliyunCaptcha-sliding-body');
  const w = document.querySelector('#aliyunCaptcha-window-float');
  const r = (e) => { if (!e) return null; const q = e.getBoundingClientRect();
    return {x: Math.round(q.x), y: Math.round(q.y),
            w: Math.round(q.width), h: Math.round(q.height)}; };
  return {
    puzzle: p ? {style: p.getAttribute('style'), cls: String(p.className)} : null,
    puzzleRect: r(p),
    slider: s ? {style: s.getAttribute('style'), cls: String(s.className)} : null,
    sliderRect: r(s),
    bodyRect: r(b),
    winCls: w ? String(w.className) : null,
  };
})()"""


def main() -> int:
    endpoint = BitBrowserClient().open_env(
        secrets.get("bitbrowser.env_id")).cdp_http
    pages = [t for t in cdp_raw.http_targets(endpoint) if t.get("type") == "page"]
    page = next((t for t in pages if "sign-up" in (t.get("url") or "")), pages[0])
    print("页面:", (page.get("url") or "")[:80])

    with cdp_raw.CDP(endpoint, ws_url=page["webSocketDebuggerUrl"],
                     timeout=60) as raw:
        raw.enable()
        pg = CDPPage.__new__(CDPPage)
        pg.cdp = raw
        pg.default_timeout = 60
        solver = AliyunSliderCDP(pg, human=True)
        print("expand:", solver.expand())
        before = pg.eval(JS_STATE)
        print("拖前:", json.dumps(before, ensure_ascii=False))

        s = before.get("sliderRect") or {}
        sx = s.get("x", 0) + s.get("w", 40) / 2
        sy = s.get("y", 0) + s.get("h", 40) / 2
        print(f"按下 ({sx},{sy})")
        pg.move(sx, sy)
        time.sleep(0.1)
        pg.mouse("mousePressed", sx, sy, buttons=1)
        time.sleep(0.2)
        for i in range(1, 13):
            pg.move(sx + i * 5, sy, buttons=1)
            time.sleep(0.05)
        time.sleep(0.3)
        mid = pg.eval(JS_STATE)
        print("拖中:", json.dumps(mid, ensure_ascii=False))
        pg.mouse("mouseReleased", sx + 60, sy, buttons=0)
        time.sleep(1.5)
        after = pg.eval(JS_STATE)
        print("拖后:", json.dumps(after, ensure_ascii=False))
        print("\n变化判断:")
        print("  puzzle style 变化:",
              (before.get("puzzle") or {}).get("style") !=
              (after.get("puzzle") or {}).get("style"))
        print("  slider style 变化:",
              (before.get("slider") or {}).get("style") !=
              (after.get("slider") or {}).get("style"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
