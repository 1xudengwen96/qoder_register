"""用 Claimer 的干净环境启动客户端（带调试端口），连 CDP 看界面 / 点登录。"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.config import get, load_config  # noqa: E402
from app.claim.claimer import Claimer  # noqa: E402
from app.register import cdp_raw  # noqa: E402

JS_DUMP = r"""(() => {
  const vis = (e) => !!(e.offsetWidth || e.offsetHeight || e.getClientRects().length);
  const txt = (e) => ((e.innerText || e.textContent || '') + '').trim().slice(0, 60);
  const btns = Array.from(document.querySelectorAll(
      'button, [role=button], a, [class*=btn], [class*=login], [class*=Login], [class*=sign]'))
    .filter(vis).map(e => {
      const r = e.getBoundingClientRect();
      return {tag: e.tagName, text: txt(e),
              cls: String(e.className || '').slice(0, 50),
              x: Math.round(r.x + r.width / 2), y: Math.round(r.y + r.height / 2),
              w: Math.round(r.width), h: Math.round(r.height)};
    }).filter(b => b.text || b.cls).slice(0, 40);
  return {url: location.href, title: document.title,
          body: (document.body ? document.body.innerText : '').slice(0, 1500),
          buttons: btns};
})()"""


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--click", default="")
    ap.add_argument("--port", type=int, default=0)
    args = ap.parse_args()

    cfg = load_config()
    claimer = Claimer(
        client_path=get(cfg, "claim.client_path", ""),
        login_mode="cdp",
    )
    print(f"客户端: {claimer.client_path}")
    if not claimer.is_running():
        print("启动:", claimer.launch(remote_debug=True))
        claimer.wait_started(timeout=90)
    else:
        print("客户端已在运行（可能没带调试端口）")

    port = args.port or claimer.harvester.read_devtools_port()
    print(f"调试端口: {port}")
    if not port:
        print("没有 DevToolsActivePort，说明当前实例没开调试")
        return 1

    for _ in range(20):
        try:
            targets = cdp_raw.http_targets(f"127.0.0.1:{port}")
            break
        except Exception:
            time.sleep(1)
            targets = []
    pages = [t for t in targets if t.get("type") == "page"]
    print(f"页面 {len(pages)} 个:")
    for t in pages:
        print(f"  - {(t.get('url') or '')[:90]}")

    if not pages:
        print("没有页面")
        return 1

    # 选内容最丰富的页面（跳过不可用的）
    best = None
    for t in pages:
        if not (t.get("url") or "").strip():
            continue
        try:
            with cdp_raw.CDP(f"127.0.0.1:{port}",
                             ws_url=t["webSocketDebuggerUrl"], timeout=15) as pg:
                pg.enable()
                d = pg.evaluate(JS_DUMP) or {}
        except Exception as exc:
            print(f"  跳过 {(t.get('url') or '')[:60]}: {type(exc).__name__}")
            continue
        body = d.get("body") or ""
        if best is None or len(body) > len(best[1].get("body") or ""):
            best = (t, d)
    if best is None:
        print("没有可用的渲染页面")
        return 1
    t, d = best
    print(f"\n=== {d.get('url')} ===")
    print("正文:", (d.get("body") or "")[:800].replace("\n", " | "))
    print("\n可点元素:")
    for b in d.get("buttons") or []:
        print(f"  {b['tag']:6s} ({b['x']},{b['y']}) {b['w']}x{b['h']} "
              f"[{b['cls'][:26]}] {b['text'][:30]}")

    if args.click:
        hit = next((b for b in d.get("buttons") or []
                    if args.click.lower() in (b.get("text") or "").lower()), None)
        if not hit:
            print(f"\n没找到「{args.click}」")
            return 1
        print(f"\n点击 {hit['text']} ({hit['x']},{hit['y']})")
        with cdp_raw.CDP(f"127.0.0.1:{port}",
                         ws_url=t["webSocketDebuggerUrl"]) as pg:
            pg.enable()
            pg.mouse("mousePressed", hit["x"], hit["y"], buttons=1)
            pg.mouse("mouseReleased", hit["x"], hit["y"], buttons=0)
            time.sleep(5)
            after = pg.evaluate(JS_DUMP) or {}
            print("点击后:", (after.get("body") or "")[:600].replace("\n", " | "))
        print("\n新页面列表:",
              [x.get("url", "")[:100] for x in
               cdp_raw.http_targets(f"127.0.0.1:{port}")])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
