"""启动/连接 Qoder 客户端（带 CDP），把界面文本和可点元素 dump 出来。

    python tools/client_login_drive.py --dump
    python tools/client_login_drive.py --click "登录"
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import secrets  # noqa: E402
from app.register import cdp_raw  # noqa: E402

DEFAULT_EXE = "C:/Users/Administrator/AppData/Local/Programs/Qoder/Qoder.exe"
PORT = 9222

JS_DUMP = r"""(() => {
  const vis = (e) => !!(e.offsetWidth || e.offsetHeight || e.getClientRects().length);
  const txt = (e) => (e.innerText || e.innerText === '' ? e.innerText :
                      (e.textContent || '')).trim().slice(0, 60);
  const btns = Array.from(document.querySelectorAll(
      'button, [role=button], a, [class*=btn], [class*=login], [class*=Login]'))
    .filter(vis).map(e => {
      const r = e.getBoundingClientRect();
      return {tag: e.tagName, text: txt(e),
              cls: String(e.className || '').slice(0, 50),
              x: Math.round(r.x + r.width / 2), y: Math.round(r.y + r.height / 2),
              w: Math.round(r.width), h: Math.round(r.height)};
    }).filter(b => b.text || b.cls).slice(0, 40);
  return {url: location.href, title: document.title,
          body: (document.body ? document.body.innerText : '').slice(0, 1200),
          buttons: btns};
})()"""


def http_json(port: int, path: str) -> dict | None:
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}{path}",
                                    timeout=5) as r:
            return json.loads(r.read().decode("utf-8", "replace"))
    except Exception:
        return None


def ensure_client(exe: str, port: int) -> bool:
    if http_json(port, "/json/version"):
        print(f"客户端已在调试端口 {port}")
        return True
    print(f"启动客户端: {exe} --remote-debugging-port={port}")
    subprocess.Popen([exe, f"--remote-debugging-port={port}"],
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    for _ in range(40):
        time.sleep(1.5)
        if http_json(port, "/json/version"):
            print("CDP 已就绪")
            return True
    print("等不到 CDP 端口")
    return False


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--exe", default=DEFAULT_EXE)
    ap.add_argument("--port", type=int, default=PORT)
    ap.add_argument("--click", default="", help="按文本点一个元素")
    ap.add_argument("--dump", action="store_true", default=True)
    args = ap.parse_args()

    if not ensure_client(args.exe, args.port):
        return 1

    targets = cdp_raw.http_targets(f"127.0.0.1:{args.port}")
    pages = [t for t in targets if t.get("type") == "page"]
    print(f"页面 {len(pages)} 个:")
    for t in pages:
        print(f"  - {(t.get('url') or '')[:90]}  id={t.get('id')}")

    target = pages[0] if pages else None
    if not target:
        print("没有页面目标")
        return 1

    with cdp_raw.CDP(f"127.0.0.1:{args.port}",
                     ws_url=target["webSocketDebuggerUrl"]) as page:
        page.enable()
        data = page.evaluate(JS_DUMP) or {}
        print("\n=== 界面 ===")
        print(f"url={data.get('url')}  title={data.get('title')}")
        print("正文:", (data.get("body") or "")[:600].replace("\n", " | "))
        print("\n可点元素:")
        for b in data.get("buttons") or []:
            print(f"  {b['tag']:6s} ({b['x']},{b['y']}) {b['w']}x{b['h']} "
                  f"[{b['cls'][:28]}] {b['text'][:30]}")

        if args.click:
            hit = next((b for b in data.get("buttons") or []
                        if args.click in (b.get("text") or "")), None)
            if not hit:
                print(f"\n没找到含「{args.click}」的可点元素")
                return 1
            print(f"\n点击 ({hit['x']},{hit['y']}) {hit['text']}")
            page.mouse("mousePressed", hit["x"], hit["y"], buttons=1)
            page.mouse("mouseReleased", hit["x"], hit["y"], buttons=0)
            time.sleep(4)
            after = page.evaluate(JS_DUMP) or {}
            print("点击后正文:", (after.get("body") or "")[:600].replace("\n", " | "))
            print("点击后页面:", [t.get("url") for t in
                                 cdp_raw.http_targets(f"127.0.0.1:{args.port}")])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
