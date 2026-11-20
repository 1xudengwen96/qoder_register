"""客户端新手引导处理：点「跳过」越过 onboarding 浮层，再点权益中心看面板。"""
import json
import sys
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / ".pylibs"))

from app.register.cdp_page import CDPPage  # noqa: E402

JS_SKIP = """(() => {
  const els = Array.from(document.querySelectorAll('button,a,[role=button],div,span'));
  const hit = els.find(e => ['跳过', 'skip', '开始使用', 'get started']
    .includes((e.innerText || '').trim().toLowerCase())
    && e.getBoundingClientRect().width > 0);
  if (!hit) return {clicked: false,
    texts: els.map(e => (e.innerText || '').trim()).filter(Boolean).slice(0, 15)};
  hit.click();
  return {clicked: true};
})()"""

JS_RECT_BENEFIT = """(() => {
  const els = Array.from(document.querySelectorAll('button,a,[role=button],div,span'))
    .filter(e => (e.innerText || '').includes('权益中心')
      && e.getBoundingClientRect().width > 0);
  if (!els.length) return null;
  els.sort((a, b) => (a.innerText || '').length - (b.innerText || '').length);
  const r = els[0].getBoundingClientRect();
  return {x: r.x + r.width / 2, y: r.y + r.height / 2};
})()"""

JS_DUMP = """(() => ({
  url: location.href.slice(0, 90),
  btns: Array.from(document.querySelectorAll('button,a,[role=button]'))
    .map(e => (e.innerText || '').trim()).filter(Boolean).slice(0, 40),
  body: (document.body ? document.body.innerText : '').slice(0, 900),
}))()"""


def pages():
    ts = json.load(urllib.request.urlopen(
        "http://127.0.0.1:9222/json/list", timeout=5))
    return [t for t in ts if t.get("type") == "page"]


# 1) onboarding 浮层循环点跳过（多步引导：角色选择→数据导入→…）
for round_i in range(6):
    onboard = [t for t in pages() if "onboarding" in (t.get("url") or "")]
    if not onboard:
        print(f"round {round_i}: onboarding 已消失")
        break
    p = CDPPage("http://127.0.0.1:9222",
                ws_url=onboard[0]["webSocketDebuggerUrl"], timeout=10)
    r = p.eval(JS_SKIP) or {}
    print(f"round {round_i} skip:", json.dumps(r, ensure_ascii=False))
    p.close()
    if not r.get("clicked"):
        break
    time.sleep(2.5)
time.sleep(2)

# 2) workbench 点权益中心（真实鼠标事件）
for t in pages():
    if "workbench" not in (t.get("url") or ""):
        continue
    p = CDPPage("http://127.0.0.1:9222", ws_url=t["webSocketDebuggerUrl"],
                timeout=10)
    rect = p.eval(JS_RECT_BENEFIT)
    print("benefit rect:", rect)
    if rect:
        x, y = float(rect["x"]), float(rect["y"])
        p.mouse("mouseMoved", x, y)
        time.sleep(0.3)
        p.mouse("mousePressed", x, y, buttons=1)
        time.sleep(0.1)
        p.mouse("mouseReleased", x, y, buttons=0)
        time.sleep(3)
    print("workbench:", json.dumps(p.eval(JS_DUMP) or {}, ensure_ascii=False,
                                   indent=1))
    p.close()

# 3) 新开的 target（权益中心 webview/页）
for t in pages():
    url = t.get("url") or ""
    if "workbench" in url or "onboarding" in url:
        continue
    try:
        q = CDPPage("http://127.0.0.1:9222", ws_url=t["webSocketDebuggerUrl"],
                    timeout=10)
        print("NEW TARGET:", json.dumps(q.eval(JS_DUMP) or {},
                                       ensure_ascii=False, indent=1))
        q.close()
    except Exception as exc:  # noqa: BLE001
        print("ERR", type(exc).__name__, exc)