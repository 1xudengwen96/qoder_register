"""把 fetch 到的验证码图片注入回 <img>（让它真正 load），再试探滑块是否可拖。"""

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

JS_HEAL = r"""(async () => {
  const heal = async (sel) => {
    const e = document.querySelector(sel);
    if (!e) return 'no-el';
    if (e.naturalWidth > 0) return 'already';
    const src = e.src || '';
    if (!src) return 'no-src';
    try {
      const r = await fetch(src, {cache: 'no-store'});
      const b = await r.blob();
      const dataUrl = await new Promise(res => {
        const fr = new FileReader();
        fr.onload = () => res(String(fr.result || ''));
        fr.onerror = () => res('');
        fr.readAsDataURL(b);
      });
      if (!dataUrl) return 'read-fail';
      e.src = dataUrl;
      await new Promise(res => {
        if (e.complete && e.naturalWidth > 0) return res();
        e.onload = () => res();
        e.onerror = () => res();
        setTimeout(res, 3000);
      });
      return 'healed:' + e.naturalWidth + 'x' + e.naturalHeight;
    } catch (err) { return 'err:' + String(err).slice(0, 60); }
  };
  const a = await heal('#aliyunCaptcha-img');
  const b = await heal('#aliyunCaptcha-puzzle');
  const s = document.querySelector('#aliyunCaptcha-sliding-slider');
  await new Promise(r => setTimeout(r, 800));
  return {img: a, puzzle: b,
          sliderCls: s ? String(s.className) : null,
          sliderStyle: s ? s.getAttribute('style') : null};
})()"""

JS_STATE = r"""(() => {
  const p = document.querySelector('#aliyunCaptcha-puzzle');
  const s = document.querySelector('#aliyunCaptcha-sliding-slider');
  const r = (e) => { if (!e) return null; const q = e.getBoundingClientRect();
    return {x: Math.round(q.x), y: Math.round(q.y),
            w: Math.round(q.width), h: Math.round(q.height)}; };
  return {puzzleStyle: p ? p.getAttribute('style') : null,
          puzzleRect: r(p), sliderCls: s ? String(s.className) : null,
          sliderStyle: s ? s.getAttribute('style') : null,
          sliderRect: r(s)};
})()"""


def main() -> int:
    endpoint = BitBrowserClient().open_env(
        secrets.get("bitbrowser.env_id")).cdp_http
    pages = [t for t in cdp_raw.http_targets(endpoint) if t.get("type") == "page"]
    page = next((t for t in pages if "sign-up" in (t.get("url") or "")), pages[0])
    with cdp_raw.CDP(endpoint, ws_url=page["webSocketDebuggerUrl"],
                     timeout=60) as raw:
        raw.enable()
        pg = CDPPage.__new__(CDPPage)
        pg.cdp = raw
        pg.default_timeout = 60
        solver = AliyunSliderCDP(pg, human=True)
        solver.expand()
        print("heal:", pg.eval(JS_HEAL, await_promise=True))
        time.sleep(1)
        print("state:", json.dumps(pg.eval(JS_STATE), ensure_ascii=False))

        st = pg.eval(JS_STATE)
        s = st["sliderRect"]
        sx, sy = s["x"] + s["w"] / 2, s["y"] + s["h"] / 2
        pg.move(sx, sy)
        time.sleep(0.15)
        pg.mouse("mousePressed", sx, sy, buttons=1)
        time.sleep(0.2)
        for i in range(1, 16):
            pg.move(sx + i * 4, sy, buttons=1)
            time.sleep(0.05)
        time.sleep(0.3)
        print("拖中:", json.dumps(pg.eval(JS_STATE), ensure_ascii=False))
        pg.mouse("mouseReleased", sx + 60, sy, buttons=0)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
