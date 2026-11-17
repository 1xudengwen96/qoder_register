"""探针：列出留存环境的标签页，dump qoder 页面的 url 和正文前 600 字。"""
from __future__ import annotations

import json
import sys
import urllib.request

from app.register.cdp_page import CDPPage

endpoint = "http://127.0.0.1:50272"
if len(sys.argv) > 1:
    endpoint = sys.argv[1]

with urllib.request.urlopen(endpoint + "/json", timeout=10) as r:
    targets = json.loads(r.read().decode("utf-8", "replace"))

pages = [t for t in targets if t.get("type") == "page"]
for t in pages:
    print(f"- {t.get('title','')!r}  {t.get('url','')}")

qoder = [t for t in pages if "qoder" in (t.get("url") or "")]
if not qoder:
    print("没有 qoder 页面")
    raise SystemExit(0)

ws = qoder[0]["webSocketDebuggerUrl"]
page = CDPPage(endpoint, ws_url=ws, timeout=15)
print("\n=== url ===")
print(page.url())
print("\n=== body.text（前 600 字）===")
print((page.eval(
    "(() => document.body ? document.body.innerText.slice(0, 600) : '')()"
) or "")[:600])
print("\n=== 可见 input/button ===")
print(page.eval("""(() => {
  const vis = (e) => !!(e.offsetWidth || e.offsetHeight ||
                        e.getClientRects().length);
  return {
    inputs: Array.from(document.querySelectorAll('input')).filter(vis)
      .map(i => ({id: i.id, type: i.type, ph: i.placeholder || ''})),
    buttons: Array.from(document.querySelectorAll('button')).filter(vis)
      .map(b => (b.innerText || '').trim()).filter(Boolean),
  };
})()"""))
