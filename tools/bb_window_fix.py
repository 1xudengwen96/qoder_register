"""查看/修正比特浏览器窗口位置（CDP 层）。

用途：确认浏览器不是无头，而是窗口被最小化、或落在屏幕外（多显示器/远程桌面
常见）。可以直接把窗口摆回可见区域。

    python tools/bb_window_fix.py --cdp http://127.0.0.1:53888          # 只看
    python tools/bb_window_fix.py --cdp http://127.0.0.1:53888 --fix    # 摆回屏幕
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.register import cdp_raw  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--cdp", required=True, help="CDP 端点，如 127.0.0.1:53888")
    ap.add_argument("--fix", action="store_true", help="把窗口摆回可见区域")
    ap.add_argument("--left", type=int, default=60)
    ap.add_argument("--top", type=int, default=40)
    ap.add_argument("--width", type=int, default=1280)
    ap.add_argument("--height", type=int, default=860)
    ap.add_argument("--front", action="store_true", help="把窗口/标签页拉到前台")
    args = ap.parse_args()

    base = cdp_raw.normalize_base(args.cdp)
    ver = cdp_raw.http_version(base)
    browser_ws = ver.get("webSocketDebuggerUrl") or ""
    if not browser_ws:
        print("FAIL 拿不到浏览器级 webSocketDebuggerUrl")
        return 1
    print(f"PASS 浏览器: {ver.get('Browser')}")

    targets = [t for t in cdp_raw.http_targets(base) if t.get("type") == "page"]
    if not targets:
        print("FAIL 没有页面目标")
        return 1

    with cdp_raw.CDP(base, ws_url=browser_ws) as b:
        b.call("Browser.enable") if False else None   # 该域无需 enable
        for t in targets:
            tid = t.get("id")
            try:
                win = b.call("Browser.getWindowForTarget", {"targetId": tid})
            except cdp_raw.CDPError as exc:
                print(f"  target {tid}: 取窗口失败 {exc}")
                continue
            wid = win.get("windowId")
            bounds = win.get("bounds") or {}
            state = bounds.get("windowState", "normal")
            print(f"  target={tid[:12]} url={(t.get('url') or '')[:60]}")
            print(f"    windowId={wid} state={state} "
                  f"left={bounds.get('left')} top={bounds.get('top')} "
                  f"w={bounds.get('width')} h={bounds.get('height')}")
            if state != "normal" or (bounds.get("left", 0) or 0) < -2000:
                print(f"    ! 窗口状态异常: {state} {bounds}")
            if args.front and wid is not None:
                b.call("Target.activateTarget", {"targetId": tid})
                print("    Target.activateTarget 已调用（窗口拉到前台）")
            if args.fix and wid is not None:
                b.call("Browser.setWindowBounds", {
                    "windowId": wid,
                    "bounds": {"windowState": "normal",
                               "left": args.left, "top": args.top,
                               "width": args.width, "height": args.height},
                })
                time_bounds = b.call("Browser.getWindowBounds",
                                     {"windowId": wid}).get("bounds") or {}
                print(f"    已摆正 -> {json.dumps(time_bounds, ensure_ascii=False)}")
    return 0


if __name__ == "__main__":
    import time  # noqa: F401
    raise SystemExit(main())
