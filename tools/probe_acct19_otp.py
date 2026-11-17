"""探查 acct19 现场：OTP 提交失败的页面真实状态（只读，不关现场）。"""
from __future__ import annotations

import json
import urllib.request

from app.register.cdp_page import CDPPage

ENDPOINT = "http://127.0.0.1:55012"

with urllib.request.urlopen(ENDPOINT + "/json", timeout=10) as r:
    targets = json.loads(r.read().decode("utf-8", "replace"))
pages = [t for t in targets if t.get("type") == "page"
         and "qoder.com" in (t.get("url") or "")]
print("qoder tabs:", len(pages))
p = CDPPage(ENDPOINT, ws_url=pages[-1]["webSocketDebuggerUrl"], timeout=15)

print("\n=== 所有 input（含隐藏）===")
print(p.eval("""(() => {
  return Array.from(document.querySelectorAll('input')).map(i => {
    const r = i.getBoundingClientRect();
    return {id: i.id || null, type: i.type, maxlength: i.maxLength,
            autoComplete: i.autocomplete || null, value: i.value,
            visible: !!(r.width || r.height), w: Math.round(r.width),
            cls: String(i.className).slice(0, 60)};
  });
})()"""))

print("\n=== OTP 框的 React props 键与事件名 ===")
print(p.eval("""(() => {
  const e = document.querySelector(
    "input[autocomplete='one-time-code'], input[maxlength='6']");
  if (!e) return null;
  const rkey = Object.keys(e).find(k => k.startsWith('__reactProps$'));
  const fkey = Object.keys(e).find(k => k.startsWith('__reactFiber$'));
  const props = rkey ? e[rkey] : null;
  return {value: e.value, reactProps: rkey, reactFiber: fkey,
          propKeys: props ? Object.keys(props) : null,
          disabled: e.disabled, readOnly: e.readOnly,
          placeholder: i => i};
})()"""))

print("\n=== 表单 / 按钮（含隐藏的位置信息）===")
print(p.eval("""(() => {
  const forms = Array.from(document.querySelectorAll('form')).map(f => ({
    id: f.id || null, w: Math.round(f.getBoundingClientRect().width),
    inputs: f.querySelectorAll('input').length,
    buttons: f.querySelectorAll('button').length}));
  const buttons = Array.from(document.querySelectorAll('button')).map(b => {
    const r = b.getBoundingClientRect();
    return {text: (b.innerText || '').trim().slice(0, 30),
            visible: !!(r.width || r.height), disabled: b.disabled,
            type: b.type};
  });
  return {forms, buttons};
})()"""))

print("\n=== 页面可见文本 ===")
txt = p.eval("document.body ? document.body.innerText : ''") or ""
print(txt[:800])

print("\n=== 报错/提示元素 ===")
print(p.eval("""(() => {
  return Array.from(document.querySelectorAll(
      '[class*=message],[class*=alert],[class*=error],'
      + '[class*=toast],[class*=tips],[role=alert]'))
    .map(e => ({cls: String(e.className).slice(0, 60),
                text: (e.innerText || '').trim().slice(0, 100)}))
    .filter(x => x.text);
})()"""))

print("\n=== Baxia 网格是否存在 ===")
print(p.eval("""(() => {
  const q = s => document.querySelector(s);
  return {
    tmdGrid: !!q('[class*=nc_grid]') || !!q('#nc_grid'),
    iframes: Array.from(document.querySelectorAll('iframe'))
      .map(f => ({src: (f.src || '').slice(0, 80),
                  w: Math.round(f.getBoundingClientRect().width)})),
    imgCount: document.querySelectorAll('img').length,
  };
})()"""))
