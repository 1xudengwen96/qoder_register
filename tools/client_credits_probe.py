"""点客户端 Credits 入口，dump 面板内容（找试用领取/额度信息）。"""
import json
import sys
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / ".pylibs"))

from app.register.cdp_page import CDPPage  # noqa: E402

JS_DUMP = """(() => ({
  btns: Array.from(document.querySelectorAll('button,a,[role=button]'))
    .map(e => (e.innerText || '').trim()).filter(Boolean).slice(0, 40),
  body: (document.body ? document.body.innerText : '').slice(0, 1200),
}))()"""

JS_CLICK_CREDITS = """(() => {
  const els = Array.from(document.querySelectorAll('button,a,[role=button],div,span'))
    .filter(e => (e.innerText || '').includes('权益中心')
      && e.getBoundingClientRect().width > 0);
  if (!els.length) return false;
  // 取文本最短的（最深层）节点，避免点到外层大容器
  els.sort((a, b) => (a.innerText || '').length - (b.innerText || '').length);
  els[0].click();
  return true;
})()"""

ts = json.load(urllib.request.urlopen("http://127.0.0.1:9222/json/list", timeout=5))
pages = [t for t in ts if t.get("type") == "page"
         and "workbench" in (t.get("url") or "")]
if not pages:
    pages = [t for t in ts if t.get("type") == "page"]
p = CDPPage("http://127.0.0.1:9222", ws_url=pages[0]["webSocketDebuggerUrl"],
            timeout=10)
print("click Credits:", p.eval(JS_CLICK_CREDITS))
time.sleep(3)
print(json.dumps(p.eval(JS_DUMP) or {}, ensure_ascii=False, indent=1))
p.close()
# 权益中心可能开在新 webview target：枚举所有 page 再 dump
ts2 = json.load(urllib.request.urlopen("http://127.0.0.1:9222/json/list", timeout=5))
for t in ts2:
    if t.get("type") != "page":
        continue
    url = t.get("url") or ""
    if "workbench" in url or "onboarding" in url:
        continue
    try:
        q = CDPPage("http://127.0.0.1:9222",
                    ws_url=t["webSocketDebuggerUrl"], timeout=8)
        print("=" * 60)
        print("TARGET:", url[:100])
        print(json.dumps(q.eval(JS_DUMP) or {}, ensure_ascii=False, indent=1))
        q.close()
    except Exception as exc:  # noqa: BLE001
        print("ERR", type(exc).__name__, exc)