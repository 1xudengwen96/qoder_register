"""dump 指定 CDP 端口当前 page 的 url/按钮/正文（诊断用）。"""
import json
import sys
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / ".pylibs"))

from app.register.cdp_page import CDPPage  # noqa: E402

JS = """(() => ({
  url: location.href.slice(0, 160),
  btns: Array.from(document.querySelectorAll('button,a,[role=button]'))
    .map(e => ({t: (e.innerText || '').trim().slice(0, 30),
                dis: !!e.disabled,
                href: String((e.getAttribute && e.getAttribute('href')) || '').slice(0, 70)}))
    .slice(0, 25),
  body: (document.body ? document.body.innerText : '').slice(0, 700),
}))()"""

port = sys.argv[1] if len(sys.argv) > 1 else "65280"
ts = json.load(urllib.request.urlopen(
    f"http://127.0.0.1:{port}/json/list", timeout=5))
for t in ts:
    if t.get("type") != "page":
        continue
    try:
        p = CDPPage(f"http://127.0.0.1:{port}",
                    ws_url=t["webSocketDebuggerUrl"], timeout=10)
        print("=" * 60)
        print(json.dumps(p.eval(JS) or {}, ensure_ascii=False, indent=1))
        p.close()
    except Exception as exc:  # noqa: BLE001
        print("ERR", type(exc).__name__, exc)