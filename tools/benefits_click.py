"""用客户端 CDP 真实鼠标事件点「权益中心」横幅，dump 弹出面板。"""
import json
import sys
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / ".pylibs"))

from app.register.cdp_page import CDPPage  # noqa: E402

JS_RECT = """(() => {
  const els = Array.from(document.querySelectorAll('button,a,[role=button],div,span'))
    .filter(e => (e.innerText || '').includes('权益中心')
      && e.getBoundingClientRect().width > 0);
  if (!els.length) return null;
  els.sort((a, b) => (a.innerText || '').length - (b.innerText || '').length);
  const r = els[0].getBoundingClientRect();
  return {x: r.x + r.width / 2, y: r.y + r.height / 2,
          text: (els[0].innerText || '').slice(0, 40)};
})()"""

JS_DUMP = """(() => ({
  btns: Array.from(document.querySelectorAll('button,a,[role=button]'))
    .map(e => (e.innerText || '').trim()).filter(Boolean).slice(0, 40),
  body: (document.body ? document.body.innerText : '').slice(0, 1000),
}))()"""

ts = json.load(urllib.request.urlopen("http://127.0.0.1:9222/json/list", timeout=5))
pages = [t for t in ts if t.get("type") == "page"
         and "workbench" in (t.get("url") or "")]
p = CDPPage("http://127.0.0.1:9222", ws_url=pages[0]["webSocketDebuggerUrl"],
            timeout=10)
rect = p.eval(JS_RECT)
print("rect:", rect)
if rect:
    x, y = float(rect["x"]), float(rect["y"])
    p.mouse("mouseMoved", x, y)
    time.sleep(0.3)
    p.mouse("mousePressed", x, y, buttons=1)
    time.sleep(0.1)
    p.mouse("mouseReleased", x, y, buttons=0)
    time.sleep(3)
    print(json.dumps(p.eval(JS_DUMP) or {}, ensure_ascii=False, indent=1))
p.close()