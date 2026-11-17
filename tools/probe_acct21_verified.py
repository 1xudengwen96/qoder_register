"""探查 acct21 现场：滑块无感通过后停在 "Verified | Go back" 页，OTP 没推进（只读）。"""
from __future__ import annotations

import json
import urllib.request

from app.register.cdp_page import CDPPage

ENDPOINT = "http://127.0.0.1:57546"

with urllib.request.urlopen(ENDPOINT + "/json", timeout=10) as r:
    targets = json.loads(r.read().decode("utf-8", "replace"))
pages = [t for t in targets if t.get("type") == "page"
         and "qoder.com" in (t.get("url") or "")]
print("qoder tabs:", len(pages), [t.get("url") for t in pages])
p = CDPPage(ENDPOINT, ws_url=pages[-1]["webSocketDebuggerUrl"], timeout=15)

print("\n=== url / body ===")
print(p.eval("location.href"))
txt = p.eval("document.body ? document.body.innerText : ''") or ""
print(txt[:500])

print("\n=== 所有可见可交互元素（button / a / [role=button] / 可点 div）===")
print(p.eval(r"""(() => {
  const vis = e => !!(e.offsetWidth || e.offsetHeight || e.getClientRects().length);
  const out = [];
  for (const e of document.querySelectorAll('button, a, [role=button], input, [onclick]')) {
    if (!vis(e)) continue;
    const r = e.getBoundingClientRect();
    out.push({tag: e.tagName, text: (e.innerText || e.value || '').trim().slice(0, 40),
              href: e.href ? e.href.slice(0, 70) : null,
              w: Math.round(r.width), x: Math.round(r.x), y: Math.round(r.y),
              disabled: !!e.disabled});
  }
  return out;
})()"""))

print("\n=== 滑块组件当前态 ===")
print(p.eval(r"""(() => {
  const b = document.getElementById('aliyunCaptcha-captcha-body');
  const w = document.getElementById('aliyunCaptcha-window-float');
  const s = document.querySelector('[class*=slider-move]');
  return {bodyCls: b ? String(b.className) : null,
          bodyText: b ? (b.innerText||'').trim().slice(0,40) : null,
          floatShown: w ? w.className : null,
          sliderCls: s ? String(s.className) : null};
})()"""))

print("\n=== body 里有没有隐藏的 OTP 输入框 / 下一步表单 ===")
print(p.eval(r"""(() => {
  const inputs = Array.from(document.querySelectorAll('input')).map(i => ({
    type: i.type, name: i.name || null, ac: i.autocomplete || null,
    ml: i.maxLength, vis: !!(i.offsetWidth || i.offsetHeight), val: (i.value||'').slice(0,10)}));
  const forms = Array.from(document.querySelectorAll('form')).map(f => ({
    vis: !!(f.offsetWidth || f.offsetHeight),
    action: (f.action || '').slice(0, 60)}));
  return {inputs, forms};
})()"""))
