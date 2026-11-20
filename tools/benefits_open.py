# -*- coding: utf-8 -*-
"""点开客户端「订阅福利」入口，dump 权益页真实内容。"""
import json
import sys
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / ".pylibs"))

from app.register.cdp_page import CDPPage  # noqa: E402

PORT = "9222"
OUT = Path("data/logs/benefits_view.txt")

JS_FIND = r"""(() => {
  const els = Array.from(document.querySelectorAll('button,a,[role=button],div,span,li'));
  const hit = els.filter(e => {
    const t = (e.innerText || '').trim();
    return t && t.includes('限时加赠');
  }).pop();
  if (!hit) return null;
  const r = hit.getBoundingClientRect();
  return {text: (hit.innerText||'').trim().slice(0,40), x: r.x + r.width/2,
          y: r.y + r.height/2, tag: hit.tagName};
})()"""

lines = []


def w(s):
    lines.append(s)


ts = json.load(urllib.request.urlopen(
    f"http://127.0.0.1:{PORT}/json/list", timeout=5))
page = None
for t in ts:
    if t.get("type") == "page" and "renderer" in (t.get("url") or ""):
        page = CDPPage(f"http://127.0.0.1:{PORT}",
                       ws_url=t["webSocketDebuggerUrl"], timeout=10)
        break
if not page:
    raise SystemExit("no workbench target")

found = page.eval(JS_FIND)
w(f"[find] {json.dumps(found, ensure_ascii=False)}")
if found:
    # React 不吃 DOM click，用 CDP Input 真实鼠标事件
    x, y = float(found["x"]), float(found["y"])
    page.mouse("mouseMoved", x, y)
    time.sleep(0.25)
    page.mouse("mousePressed", x, y, buttons=1)
    time.sleep(0.1)
    page.mouse("mouseReleased", x, y, buttons=0)
    time.sleep(6)
    body = page.eval("document.body ? document.body.innerText : ''") or ""
    w("[body after click]")
    w(body[:4000])
    btns = page.eval("""Array.from(document.querySelectorAll(
      'button,a,[role=button]')).map(e => (e.innerText||'').trim())
      .filter(Boolean).slice(0,60)""") or []
    w("[btns] " + json.dumps(btns, ensure_ascii=False))

    # 重扫所有 target，看是否有新 webview 打开
    ts2 = json.load(urllib.request.urlopen(
        f"http://127.0.0.1:{PORT}/json/list", timeout=5))
    w("[targets after click]")
    for t2 in ts2:
        w(f"  - {t2.get('type')} | {t2.get('url','')[:120]} | {t2.get('title','')[:40]}")
        if t2.get("type") == "page":
            try:
                p2 = CDPPage(f"http://127.0.0.1:{PORT}",
                             ws_url=t2["webSocketDebuggerUrl"], timeout=8)
                b2 = p2.eval("document.body ? document.body.innerText.slice(0,1500) : ''") or ""
                w("[inner] " + b2[:600])
                p2.close()
            except Exception as exc:  # noqa: BLE001
                w(f"  [inner fail] {exc}")

OUT.write_text("\n".join(lines), encoding="utf-8")
print(f"written {OUT}")
