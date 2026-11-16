"""Qoder 客户端 UI 驱动：dump 界面 / 按文本点击 / 拦截 window.open。

    python tools/client_ui.py --dump
    python tools/client_ui.py --click "dengwen xu"
    python tools/client_ui.py --hook          # 装一个 window.open 钩子
    python tools/client_ui.py --opened        # 看被打开过的链接
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.register import cdp_raw  # noqa: E402

JS_FIND = r"""(args) => {
  const vis = (e) => !!(e.offsetWidth || e.offsetHeight || e.getClientRects().length);
  const want = String(args.text || '').toLowerCase();
  const nodes = Array.from(document.querySelectorAll(
      'button, a, [role=button], [role=menuitem], li, div, span'));
  for (const e of nodes) {
    if (!vis(e)) continue;
    const t = ((e.innerText || e.textContent || '') + '').trim();
    if (!t) continue;
    const hit = args.exact ? (t.toLowerCase() === want)
                           : (t.toLowerCase().includes(want));
    if (!hit) continue;
    if (t.length > 40) continue;
    e.scrollIntoView({block: 'center'});
    const r = e.getBoundingClientRect();
    if (r.width < 6 || r.height < 6) continue;
    return {text: t.slice(0, 40), x: Math.round(r.x + r.width / 2),
            y: Math.round(r.y + r.height / 2),
            w: Math.round(r.width), h: Math.round(r.height)};
  }
  return null;
}"""

JS_DUMP = r"""(() => {
  const vis = (e) => !!(e.offsetWidth || e.offsetHeight || e.getClientRects().length);
  const txt = (e) => ((e.innerText || e.textContent || '') + '').trim().slice(0, 40);
  const items = [];
  document.querySelectorAll('button, a, [role=button], [role=menuitem], li, div')
    .forEach(e => {
      if (!vis(e)) return;
      const t = txt(e);
      if (!t || t.length > 30) return;
      const r = e.getBoundingClientRect();
      if (r.width < 8 || r.height < 8) return;
      items.push({tag: e.tagName, text: t,
                  cls: String(e.className || '').slice(0, 36),
                  x: Math.round(r.x + r.width / 2),
                  y: Math.round(r.y + r.height / 2),
                  w: Math.round(r.width), h: Math.round(r.height)});
    });
  const seen = new Set();
  const uniq = [];
  for (const it of items) {
    const k = it.text + '@' + it.x + ',' + it.y;
    if (seen.has(k)) continue;
    seen.add(k);
    uniq.push(it);
  }
  return {url: location.href,
          body: (document.body ? document.body.innerText : '').slice(0, 900),
          items: uniq.slice(0, 80)};
})()"""

JS_HOOK = r"""(() => {
  if (window.__qoderHook) return 'already';
  window.__opened = [];
  const orig = window.open;
  window.open = function (u, ...rest) {
    try { window.__opened.push(String(u)); } catch (e) {}
    return orig.call(window, u, ...rest);
  };
  window.__qoderHook = true;
  return 'hooked';
})()"""


def best_page(port: int) -> dict | None:
    pages = [t for t in cdp_raw.http_targets(f"127.0.0.1:{port}")
             if t.get("type") == "page"]
    pages = [p for p in pages if (p.get("url") or "").startswith("qoder-app")]
    return pages[0] if pages else (pages[0] if pages else None)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=9222)
    ap.add_argument("--dump", action="store_true")
    ap.add_argument("--click", default="")
    ap.add_argument("--exact", action="store_true", help="文本精确匹配")
    ap.add_argument("--hook", action="store_true")
    ap.add_argument("--opened", action="store_true")
    ap.add_argument("--hash", default="", help="切换到某个路由，如 #/settings")
    args = ap.parse_args()

    pages = [t for t in cdp_raw.http_targets(f"127.0.0.1:{args.port}")
             if t.get("type") == "page"]
    pages = [p for p in pages if (p.get("url") or "").startswith("qoder-app")]
    print(f"页面 {len(pages)} 个")
    for p in pages:
        print("  -", (p.get("url") or "")[:90])
    if not pages:
        return 1
    target = pages[0]

    with cdp_raw.CDP(f"127.0.0.1:{args.port}",
                     ws_url=target["webSocketDebuggerUrl"], timeout=30) as page:
        page.enable()
        if args.hook:
            print("hook:", page.evaluate(JS_HOOK))
        if args.hash:
            page.evaluate("(h) => { location.hash = h; return location.href; }"
                          .replace("(h) => ", "") + "") if False else None
            print("hash ->", page.evaluate(
                "(() => { location.hash = %s; return location.href; })()"
                % json.dumps(args.hash)))
            time.sleep(3)
        if args.opened:
            print("opened:", page.evaluate("window.__opened || []"))

        data = page.evaluate(JS_DUMP) or {}
        print(f"\nurl={data.get('url')}")
        print("正文:", (data.get("body") or "").replace("\n", " | ")[:400])
        if args.dump or args.click:
            print("\n可点元素:")
            for it in data.get("items") or []:
                print(f"  ({it['x']},{it['y']}) {it['w']}x{it['h']} "
                      f"[{it['cls'][:20]}] {it['text']}")

        if args.click:
            hit = page.evaluate(
                f"({JS_FIND})({json.dumps({'text': args.click, 'exact': args.exact})})")
            if not hit:
                print(f"\n没找到「{args.click}」")
                return 1
            print(f"\n点击「{hit['text']}」({hit['x']},{hit['y']})")
            _x, _y = float(hit["x"]), float(hit["y"])
            page.call("Input.dispatchMouseEvent", {
                "type": "mouseMoved", "x": _x, "y": _y,
                "button": "none", "buttons": 0, "pointerType": "mouse"})
            page.call("Input.dispatchMouseEvent", {
                "type": "mousePressed", "x": _x, "y": _y, "button": "left",
                "buttons": 1, "clickCount": 1, "pointerType": "mouse"})
            page.call("Input.dispatchMouseEvent", {
                "type": "mouseReleased", "x": _x, "y": _y, "button": "left",
                "buttons": 0, "clickCount": 1, "pointerType": "mouse"})
            time.sleep(3)
            after = page.evaluate(JS_DUMP) or {}
            print("点击后正文:", (after.get("body") or "").replace("\n", " | ")[:500])
            print("点击后可点:")
            for it in (after.get("items") or [])[:40]:
                print(f"  ({it['x']},{it['y']}) {it['text']}")
            print("opened:", page.evaluate("window.__opened || []"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
