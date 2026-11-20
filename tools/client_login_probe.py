"""探测 Qoder 客户端自身 CDP（9222）的登录界面，尝试自动点「登录」拿链接。

用法：
    $env:PYTHONPATH='.;.pylibs'
    python tools/client_login_probe.py          # 只探测 DOM
    python tools/client_login_probe.py --click  # 点登录按钮并观察链接产出
"""
from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / ".pylibs"))

from app.register.cdp_page import CDPPage  # noqa: E402

PORT = 9222

JS_DOM = """(() => ({
  btns: Array.from(document.querySelectorAll('button,a,[role=button]'))
    .map(e => ({
      tag: e.tagName,
      text: (e.innerText || '').trim().slice(0, 40),
      href: String((e.getAttribute && e.getAttribute('href')) || '').slice(0, 100),
      cls: String(e.className || '').slice(0, 60),
    })).slice(0, 30),
  body: (document.body ? document.body.innerText : '').slice(0, 400),
}))()"""

JS_CLICK_LOGIN = """(() => {
  const want = ['立即登录', '登录', 'log in', 'login', 'sign in'];
  const els = Array.from(document.querySelectorAll('button,a,[role=button]'));
  const hit = els.find(e => {
    const t = (e.innerText || '').trim().toLowerCase();
    return t && want.some(w => t.includes(w));
  });
  if (!hit) return {clicked: false, texts: els.map(e => (e.innerText || '').trim()).slice(0, 10)};
  hit.click();
  return {clicked: true, text: (hit.innerText || '').trim()};
})()"""

JS_LINKS = """(() => ({
  hrefs: Array.from(document.querySelectorAll('a[href]'))
    .map(a => a.getAttribute('href'))
    .filter(h => h && (h.includes('selectAccounts') || h.includes('device') || h.startsWith('qoder')))
    .slice(0, 5),
  body: (document.body ? document.body.innerText : '').slice(0, 300),
}))()"""


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--click", action="store_true")
    args = ap.parse_args()

    ts = json.load(urllib.request.urlopen(f"http://127.0.0.1:{PORT}/json/list", timeout=5))
    pages = [t for t in ts if t.get("type") == "page"]
    print(f"page targets: {len(pages)}")
    for t in pages:
        print(" -", t.get("title", "")[:80])
    if not pages:
        return 1
    p = CDPPage(f"http://127.0.0.1:{PORT}", ws_url=pages[0]["webSocketDebuggerUrl"])
    dom = p.eval(JS_DOM) or {}
    print(json.dumps(dom, ensure_ascii=False, indent=1))
    if args.click:
        r = p.eval(JS_CLICK_LOGIN) or {}
        print("click:", json.dumps(r, ensure_ascii=False))
        for i in range(10):
            time.sleep(1.5)
            links = p.eval(JS_LINKS) or {}
            if links.get("hrefs"):
                print("LINKS:", json.dumps(links, ensure_ascii=False))
                break
            # 也看有没有新 target（客户端可能开新页放链接）
            ts2 = json.load(urllib.request.urlopen(
                f"http://127.0.0.1:{PORT}/json/list", timeout=5))
            new = [t for t in ts2 if t.get("type") == "page"
                   and t["id"] not in {x["id"] for x in pages}]
            if new:
                print("new targets:", [t.get("url", "")[:100] for t in new])
            print(f"wait {i + 1}: body={links.get('body', '')[:80]!r}")
    p.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())