"""起一个隔离数据目录的 Qoder 客户端实例（不动用户当前登录的实例）。

    python tools/client_second_instance.py --dump
    python tools/client_second_instance.py --click 登录
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import urllib.request  # noqa: E402

from app.register import cdp_raw  # noqa: E402

EXE = "C:/Users/Administrator/AppData/Local/Programs/Qoder/Qoder.exe"
PORT = 9333
PROFILE = ROOT / "data" / "qoder_client"

JS_DUMP = r"""(() => {
  const vis = (e) => !!(e.offsetWidth || e.offsetHeight || e.getClientRects().length);
  const txt = (e) => ((e.innerText || e.textContent || '') + '').trim().slice(0, 50);
  const btns = Array.from(document.querySelectorAll('button, [role=button], a'))
    .filter(vis).map(e => {
      const r = e.getBoundingClientRect();
      return {tag: e.tagName, text: txt(e),
              cls: String(e.className || '').slice(0, 40),
              x: Math.round(r.x + r.width / 2), y: Math.round(r.y + r.height / 2),
              w: Math.round(r.width), h: Math.round(r.height)};
    }).filter(b => b.text).slice(0, 40);
  return {url: location.href, body: (document.body ? document.body.innerText : '').slice(0, 800),
          buttons: btns};
})()"""


def version(port: int) -> dict | None:
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/json/version",
                                    timeout=4) as r:
            return json.loads(r.read().decode())
    except Exception:
        return None


def clean_env() -> dict:
    import os

    env = {k: v for k, v in os.environ.items()
           if k not in ("ELECTRON_RUN_AS_NODE", "ELECTRON_NO_ATTACH_CONSOLE")}
    return env


def launch() -> bool:
    PROFILE.mkdir(parents=True, exist_ok=True)
    args = [EXE, f"--user-data-dir={PROFILE}",
            f"--remote-debugging-port={PORT}"]
    print("启动:", " ".join(args))
    subprocess.Popen(args, stdout=subprocess.DEVNULL,
                     stderr=subprocess.DEVNULL, env=clean_env())
    for _ in range(40):
        time.sleep(1.5)
        if version(PORT):
            print("CDP 就绪:", version(PORT).get("Browser"))
            return True
    return False


def dump(port: int, click: str = "") -> int:
    targets = cdp_raw.http_targets(f"127.0.0.1:{port}")
    pages = [t for t in targets if t.get("type") == "page" and (t.get("url") or "").strip()]
    print(f"页面 {len(pages)} 个:")
    for t in pages:
        print("  -", (t.get("url") or "")[:90])
    best = None
    for t in pages:
        try:
            with cdp_raw.CDP(f"127.0.0.1:{port}",
                             ws_url=t["webSocketDebuggerUrl"], timeout=15) as pg:
                pg.enable()
                d = pg.evaluate(JS_DUMP) or {}
        except Exception as exc:
            print(f"  跳过 {type(exc).__name__}")
            continue
        if best is None or len(d.get("body") or "") > len(best[1].get("body") or ""):
            best = (t, d)
    if not best:
        print("没有可用页面")
        return 1
    t, d = best
    print("\n=== 界面 ===")
    print((d.get("body") or "")[:700].replace("\n", " | "))
    print("\n可点:")
    for b in d.get("buttons") or []:
        print(f"  ({b['x']},{b['y']}) {b['w']}x{b['h']} [{b['cls'][:20]}] {b['text']}")

    if click:
        hit = next((b for b in d.get("buttons") or []
                    if click.lower() in b["text"].lower()), None)
        if not hit:
            print(f"\n没找到「{click}」")
            return 1
        print(f"\n点击「{hit['text']}」({hit['x']},{hit['y']})")
        with cdp_raw.CDP(f"127.0.0.1:{port}",
                         ws_url=t["webSocketDebuggerUrl"], timeout=20) as pg:
            pg.enable()
            pg.mouse("mousePressed", hit["x"], hit["y"], buttons=1)
            pg.mouse("mouseReleased", hit["x"], hit["y"], buttons=0)
            time.sleep(6)
            after = pg.evaluate(JS_DUMP) or {}
            print("点击后:", (after.get("body") or "")[:600].replace("\n", " | "))
        print("\n所有目标:")
        for x in cdp_raw.http_targets(f"127.0.0.1:{port}"):
            print("  -", x.get("type"), (x.get("url") or "")[:110])
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--click", default="")
    ap.add_argument("--no-launch", action="store_true")
    args = ap.parse_args()
    if not args.no_launch and not version(PORT):
        if not launch():
            print("启动失败（端口没起来）")
            return 1
    return dump(PORT, args.click)


if __name__ == "__main__":
    raise SystemExit(main())
