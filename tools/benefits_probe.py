"""权益中心定位：dump 客户端空 URL target + Chrome 最近 qoder 链接。"""
import json
import sys
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / ".pylibs"))

from app.register.cdp_page import CDPPage  # noqa: E402

JS = """(() => ({
  url: location.href.slice(0, 120),
  btns: Array.from(document.querySelectorAll('button,a,[role=button]'))
    .map(e => (e.innerText || '').trim()).filter(Boolean).slice(0, 30),
  body: (document.body ? document.body.innerText : '').slice(0, 800),
}))()"""

ts = json.load(urllib.request.urlopen("http://127.0.0.1:9222/json/list", timeout=5))
for t in ts:
    if t.get("type") != "page" or t.get("url"):
        continue
    try:
        p = CDPPage("http://127.0.0.1:9222",
                    ws_url=t["webSocketDebuggerUrl"], timeout=20)
        print("EMPTY-TARGET:", json.dumps(p.eval(JS) or {}, ensure_ascii=False,
                                          indent=1))
        p.close()
    except Exception as exc:  # noqa: BLE001
        print("ERR", type(exc).__name__, exc)

from app.claim.ui_login import from_browser_history  # noqa: E402
link, src = from_browser_history(since=time.time() - 900), "history"
print("recent chrome qoder link:", link[:160] if link else None)