"""实验：acct21 现场「组件 Verified 但表单不推进」的解法探索。

假设：判定延迟 + refresh 打断 → 组件显示 Verified 但 qoder 前端没收到
success 回调，页面死停在人机验证页。试三种推进方式（每种之间检查状态）：
A. 点 verified 的滑块药丸（#aliyunCaptcha-captcha-body）
B. 点组件里的 "Go back" 元素
C. reload 页面看表单状态
每步只读检查 + 一次点击，全程保留现场。
"""
from __future__ import annotations

import json
import time
import urllib.request

from app.register.cdp_page import CDPPage

ENDPOINT = "http://127.0.0.1:57546"

with urllib.request.urlopen(ENDPOINT + "/json", timeout=10) as r:
    targets = json.loads(r.read().decode("utf-8", "replace"))
pages = [t for t in targets if t.get("type") == "page"
         and "qoder.com" in (t.get("url") or "")]


def connect():
    return CDPPage(ENDPOINT, ws_url=pages[-1]["webSocketDebuggerUrl"], timeout=15)


def state(p, tag: str) -> None:
    st = p.eval(r"""(() => {
      const vis = e => !!(e.offsetWidth || e.offsetHeight || e.getClientRects().length);
      const inputs = Array.from(document.querySelectorAll('input')).filter(vis).length;
      const btns = Array.from(document.querySelectorAll('button,[role=button]'))
        .filter(vis).map(b => (b.innerText || '').trim().slice(0, 20)).filter(t => t);
      const b = document.getElementById('aliyunCaptcha-captcha-body');
      return {url: location.href, inputs, btns,
              bodyCls: b ? String(b.className) : null,
              bodyText: (document.body ? document.body.innerText : '')
                .slice(0, 120)};
    })()""")
    print(f"[{tag}] {st}")


p = connect()
state(p, "初始")

# --- A. 点 verified 的滑块药丸
r = p.eval(r"""(() => {
  const b = document.getElementById('aliyunCaptcha-captcha-body');
  if (!b) return null;
  const rc = b.getBoundingClientRect();
  return {x: rc.x + rc.width / 2, y: rc.y + rc.height / 2, w: rc.width};
})()""")
print("药丸 rect:", r)
if r and r["w"] > 0:
    p.click_xy(float(r["x"]), float(r["y"]), human=True)
    time.sleep(8)
    state(p, "A 点药丸后")

# --- B. 点 "Go back"
g = p.eval(r"""(() => {
  const vis = e => !!(e.offsetWidth || e.offsetHeight || e.getClientRects().length);
  for (const e of document.querySelectorAll('*')) {
    if (!vis(e)) continue;
    if ((e.innerText || '').trim() === 'Go back' && e.children.length === 0) {
      const r = e.getBoundingClientRect();
      return {tag: e.tagName, cls: String(e.className).slice(0, 60),
              x: r.x + r.width / 2, y: r.y + r.height / 2};
    }
  }
  return null;
})()""")
print("Go back 元素:", g)
if g:
    p.click_xy(float(g["x"]), float(g["y"]), human=True)
    time.sleep(8)
    state(p, "B 点 Go back 后")

# --- C. reload（最后手段，看表单是否保留）
print("\n--- 以上均未推进的话，考虑 reload（此处暂不执行，保留现场）---")
