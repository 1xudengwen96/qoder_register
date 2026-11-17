"""探查 e2e3 标签页的滑块验证结果状态（只读）。"""
from __future__ import annotations

import json
import urllib.request

from app.register.cdp_page import CDPPage

with urllib.request.urlopen("http://127.0.0.1:50272/json", timeout=10) as r:
    targets = json.loads(r.read().decode("utf-8", "replace"))
pages = [t for t in targets if t.get("type") == "page"
         and "qoder.com" in (t.get("url") or "")]
p = CDPPage("http://127.0.0.1:50272", ws_url=pages[-1]["webSocketDebuggerUrl"],
            timeout=15)

print("=== aliyun 组件状态 ===")
print(p.eval("""(() => {
  const q = s => document.querySelector(s);
  const win = q('#aliyunCaptcha-window-float');
  const sd = q('#aliyunCaptcha-sliding-slider');
  const ce = q('#captcha-element');
  return {
    winCls: win ? String(win.className) : null,
    winVisible: win ? (win.offsetParent !== null) : null,
    sliderCls: sd ? String(sd.className) : null,
    sliderDisp: sd ? getComputedStyle(sd).display : null,
    captchaElCls: ce ? String(ce.className).slice(0, 80) : null,
    captchaElText: ce ? (ce.innerText || '').slice(0, 120) : null,
  };
})()"""))

print("\n=== 正文文本（captcha 相关行）===")
txt = p.eval("document.body ? document.body.innerText : ''") or ""
for line in txt.splitlines():
    s = line.strip()
    if s and any(k in s.lower() for k in ("captcha", "verif", "code", "email")):
        print(" |", s[:120])

print("\n=== 可见 input/button ===")
print(p.eval("""(() => {
  const vis = e => !!(e.offsetWidth || e.offsetHeight ||
                        e.getClientRects().length);
  return {
    inputs: Array.from(document.querySelectorAll('input')).filter(vis)
      .map(i => ({id: i.id, type: i.type, ph: (i.placeholder || '').slice(0, 30)})),
    buttons: Array.from(document.querySelectorAll('button')).filter(vis)
      .map(b => (b.innerText || '').trim()).filter(Boolean),
  };
})()"""))
