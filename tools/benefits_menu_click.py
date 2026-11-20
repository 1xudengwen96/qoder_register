"""点账户菜单里的「权益中心」菜单项，dump 权益中心面板找领取按钮。"""
import json
import sys
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / ".pylibs"))

from app.register.cdp_page import CDPPage  # noqa: E402

JS_OPEN_MENU = """(() => {
  const els = Array.from(document.querySelectorAll('button,a,[role=button],div,span'))
    .filter(e => (e.innerText || '').includes('权益中心，')
      && e.getBoundingClientRect().width > 0);
  if (!els.length) return null;
  els.sort((a, b) => (a.innerText || '').length - (b.innerText || '').length);
  const r = els[0].getBoundingClientRect();
  return {x: r.x + r.width / 2, y: r.y + r.height / 2};
})()"""

JS_CLICK_MENU = """(() => {
  const els = Array.from(document.querySelectorAll('button,a,[role=button],div,span,li'))
    .filter(e => (e.innerText || '').trim() === '权益中心'
      && e.getBoundingClientRect().width > 0);
  if (!els.length) return {clicked: false};
  els.sort((a, b) => (a.innerText || '').length - (b.innerText || '').length);
  const r = els[0].getBoundingClientRect();
  return {clicked: true, x: r.x + r.width / 2, y: r.y + r.height / 2};
})()"""

JS_DISMISS = """(() => {
  const want = ['忽略', '跳过', '开始使用', '知道了', '稍后', '以后再说', '关闭'];
  const els = Array.from(document.querySelectorAll('button,a,[role=button],div,span'))
    .filter(e => want.includes((e.innerText || '').trim())
      && e.getBoundingClientRect().width > 0);
  if (!els.length) return null;
  els.sort((a, b) => (a.innerText || '').length - (b.innerText || '').length);
  const r = els[0].getBoundingClientRect();
  return {text: (els[0].innerText || '').trim(),
          x: r.x + r.width / 2, y: r.y + r.height / 2};
})()"""

JS_FIND = """(() => {
  const vis = (e) => e.getBoundingClientRect().width > 0;
  const all = Array.from(document.querySelectorAll('button,a,[role=button],div,span,li'));
  const item = all.filter(e => (e.innerText || '').trim() === '权益中心' && vis(e));
  if (item.length) {
    item.sort((a, b) => (a.innerText || '').length - (b.innerText || '').length);
    const r = item[0].getBoundingClientRect();
    return {kind: 'item', x: r.x + r.width / 2, y: r.y + r.height / 2};
  }
  const ban = all.filter(e => (e.innerText || '').includes('权益中心') && vis(e));
  if (ban.length) {
    ban.sort((a, b) => (a.innerText || '').length - (b.innerText || '').length);
    const r = ban[0].getBoundingClientRect();
    return {kind: 'banner', x: r.x + r.width / 2, y: r.y + r.height / 2};
  }
  return null;
})()"""

JS_DUMP = """(() => ({
  url: location.href.slice(0, 110),
  btns: Array.from(document.querySelectorAll('button,a,[role=button]'))
    .map(e => (e.innerText || '').trim()).filter(Boolean).slice(0, 40),
  body: (document.body ? document.body.innerText : '').slice(0, 1200),
}))()"""


def pages():
    ts = json.load(urllib.request.urlopen(
        "http://127.0.0.1:9222/json/list", timeout=5))
    return [t for t in ts if t.get("type") == "page"]


for t in pages():
    if "workbench" not in (t.get("url") or ""):
        continue
    p = CDPPage("http://127.0.0.1:9222", ws_url=t["webSocketDebuggerUrl"],
                timeout=10)
    # 先清促销/引导弹层（忽略/跳过/知道了…），否则侧栏点击被挡
    for i in range(6):
        d = p.eval(JS_DISMISS)
        if not d:
            break
        print(f"dismiss {i}:", d.get("text"))
        p.mouse("mouseMoved", float(d["x"]), float(d["y"]))
        time.sleep(0.2)
        p.mouse("mousePressed", float(d["x"]), float(d["y"]), buttons=1)
        time.sleep(0.1)
        p.mouse("mouseReleased", float(d["x"]), float(d["y"]), buttons=0)
        time.sleep(1.5)
    opened = False
    for i in range(4):
        got = p.eval(JS_FIND)
        print(f"step {i} find:", got)
        if not got:
            break
        x, y = float(got["x"]), float(got["y"])
        p.mouse("mouseMoved", x, y)
        time.sleep(0.25)
        p.mouse("mousePressed", x, y, buttons=1)
        time.sleep(0.1)
        p.mouse("mouseReleased", x, y, buttons=0)
        time.sleep(1.5)
        if got.get("kind") == "item":
            opened = True
            break
    time.sleep(3)
    print("opened:", opened)
    print("workbench after:", json.dumps(p.eval(JS_DUMP) or {},
                                         ensure_ascii=False, indent=1))
    p.close()

for t in pages():
    url = t.get("url") or ""
    if "workbench" in url or "onboarding" in url:
        continue
    try:
        q = CDPPage("http://127.0.0.1:9222", ws_url=t["webSocketDebuggerUrl"],
                    timeout=12)
        print("TARGET:", json.dumps(q.eval(JS_DUMP) or {},
                                   ensure_ascii=False, indent=1))
        q.close()
    except Exception as exc:  # noqa: BLE001
        print("ERR", type(exc).__name__, exc)