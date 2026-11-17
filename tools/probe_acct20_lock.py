"""探查 acct20 现场：refresh 换题后滑块窗口起不来的锁定态根因（只读，不关现场）。"""
from __future__ import annotations

import json
import urllib.request

from app.register.cdp_page import CDPPage

ENDPOINT = "http://127.0.0.1:56629"

with urllib.request.urlopen(ENDPOINT + "/json", timeout=10) as r:
    targets = json.loads(r.read().decode("utf-8", "replace"))
pages = [t for t in targets if t.get("type") == "page"
         and "qoder.com" in (t.get("url") or "")]
print("qoder tabs:", len(pages))
p = CDPPage(ENDPOINT, ws_url=pages[-1]["webSocketDebuggerUrl"], timeout=15)

print("\n=== 滑块组件全家状态 ===")
print(p.eval(r"""(() => {
  const names = ['aliyunCaptcha-window-embed', 'aliyunCaptcha-window-popup',
    'aliyunCaptcha-sliding-body', 'aliyunCaptcha-sliding-bottom',
    'aliyunCaptcha-captcha-element', 'aliyunCaptcha-captcha-left',
    'aliyunCaptcha-captcha-body'];
  const out = {};
  for (const n of names) {
    const e = document.getElementById(n);
    if (!e) { out[n] = null; continue; }
    const r = e.getBoundingClientRect();
    const st = getComputedStyle(e);
    out[n] = {
      w: Math.round(r.width), h: Math.round(r.height),
      display: st.display, visibility: st.visibility,
      pe: st.pointerEvents, opacity: st.opacity,
      cls: String(e.className).slice(0, 80),
    };
  }
  return out;
})()"""))

print("\n=== 滑块图片 / 拼图块 ===")
print(p.eval(r"""(() => {
  const out = [];
  for (const img of document.querySelectorAll('img')) {
    const r = img.getBoundingClientRect();
    if (r.width < 5 && r.height < 5 && !img.naturalWidth) continue;
    const st = getComputedStyle(img);
    out.push({
      src: (img.src || '').slice(0, 40),
      natW: img.naturalWidth, natH: img.naturalHeight,
      w: Math.round(r.width), h: Math.round(r.height),
      display: st.display, pe: st.pointerEvents,
      cls: String(img.className).slice(0, 50),
    });
  }
  return out;
})()"""))

print("\n=== iframe 列表 / body 文本 ===")
print(p.eval(r"""(() => ({
  iframes: Array.from(document.querySelectorAll('iframe')).map(f => ({
    src: (f.src || '').slice(0, 70),
    w: Math.round(f.getBoundingClientRect().width)})),
  body: (document.body ? document.body.innerText : '').slice(0, 300),
}))()"""))

print("\n=== 滑块按钮（slider-move）class 与 pe ===")
print(p.eval(r"""(() => {
  const s = document.querySelector('[class*=slider-move]');
  if (!s) return null;
  const r = s.getBoundingClientRect();
  return {cls: String(s.className), pe: getComputedStyle(s).pointerEvents,
          display: getComputedStyle(s).display,
          w: Math.round(r.width), x: Math.round(r.left)};
})()"""))
