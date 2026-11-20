"""探测 Qoder 客户端各页面 DOM（按钮/正文），找试用领取入口。"""
import json
import sys
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / ".pylibs"))

from app.register.cdp_page import CDPPage  # noqa: E402

JS = """(() => ({
  url: location.href.slice(0, 90),
  btns: Array.from(document.querySelectorAll('button,a,[role=button]'))
    .map(e => (e.innerText || '').trim()).filter(Boolean).slice(0, 25),
  body: (document.body ? document.body.innerText : '').slice(0, 500),
}))()"""

ts = json.load(urllib.request.urlopen("http://127.0.0.1:9222/json/list", timeout=5))
for t in ts:
    if t.get("type") != "page":
        continue
    try:
        p = CDPPage("http://127.0.0.1:9222",
                    ws_url=t["webSocketDebuggerUrl"], timeout=8)
        out = p.eval(JS) or {}
        print("=" * 60)
        print(json.dumps(out, ensure_ascii=False, indent=1))
        p.close()
    except Exception as exc:  # noqa: BLE001
        print("ERR", type(exc).__name__, exc)