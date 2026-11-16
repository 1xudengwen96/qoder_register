"""在活着的页面上解 Baxia 九宫格点选（2Captcha CoordinatesTask）。

    python tools/baxia_solve.py              # 自动找注册页
    python tools/baxia_solve.py --dry        # 只截图 + 问 2Captcha，不点击
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import secrets  # noqa: E402
from app.net.twocaptcha_click import TwoCaptchaClick, data_url_to_bytes  # noqa: E402
from app.register import cdp_raw  # noqa: E402
from app.register.baxia_cdp import BaxiaClickCaptcha, points_to_cells  # noqa: E402
from app.register.bitbrowser import BitBrowserClient  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--env-id", default=secrets.get("bitbrowser.env_id", ""))
    ap.add_argument("--cdp", default="")
    ap.add_argument("--dry", action="store_true", help="只识别不点击")
    ap.add_argument("--out", default="data/artifacts/baxia_grid")
    ap.add_argument("--max-attempts", type=int, default=3)
    args = ap.parse_args()

    key = secrets.get("captcha.twocaptcha_key", "")
    if not key:
        print("缺 2Captcha key")
        return 2

    endpoint = args.cdp or BitBrowserClient().open_env(args.env_id).cdp_http
    pages = [t for t in cdp_raw.http_targets(endpoint) if t.get("type") == "page"]
    target = next((t for t in pages if "sign-up" in (t.get("url") or "")),
                  pages[0] if pages else None)
    if not target:
        print("没有页面")
        return 1
    print(f"页面: {(target.get('url') or '')[:100]}")

    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    with cdp_raw.CDP(endpoint, ws_url=target["webSocketDebuggerUrl"]) as raw_page:
        from app.register.cdp_page import CDPPage

        page = CDPPage.__new__(CDPPage)
        page.cdp = raw_page
        page.default_timeout = 60
        raw_page.enable()

        solver = BaxiaClickCaptcha(page)
        if not solver.present():
            print("当前页面没有 Baxia 验证框")
            return 1

        data = solver.capture()
        if data.get("error"):
            print("取图失败:", data)
            return 1
        png = data_url_to_bytes(data["dataUrl"])
        out = f"{args.out}_0.png"
        Path(out).write_bytes(png)
        print(f"合成图已存: {out}  {len(png)} bytes")
        print(f"布局: {data.get('layout')}  格子数: {len(data.get('gridRects') or [])}")
        print("格子中心（页面坐标）:",
              [(g['i'], round(g['x']), round(g['y'])) for g in data.get('gridRects') or []])

        client = TwoCaptchaClick(key)
        try:
            print(f"2Captcha 余额: ${client.balance():.4f}")
        except Exception as exc:
            print(f"余额查询失败: {exc}")

        if args.dry:
            comment = ("body 图是 3x3 九宫格照片。imgInstructions 图：上面是题干文字，"
                       "下面是要找的目标图案（放大 4 倍）。请在九宫格中选出所有包含该图案的格子，"
                       "返回格子中心点在 body 图中的坐标。")
            q_png = (data_url_to_bytes(data["questionUrl"])
                     if data.get("questionUrl") else None)
            sol = client.solve(png, comment, img_instructions=q_png)
            cells = points_to_cells(sol.points, data.get("layout") or {})
            print(f"识别结果: points={sol.points} -> cells={cells} "
                  f"耗时={sol.elapsed:.1f}s cost=${sol.cost:.4f}")
            return 0

        res = solver.solve(client, max_attempts=args.max_attempts,
                           save_path=f"{args.out}.png")
        print(json.dumps({
            "ok": res.ok, "cells": res.cells, "points": res.points,
            "detail": res.detail, "cost": round(res.cost, 4),
            "image": res.image_path,
        }, ensure_ascii=False, indent=2))
        return 0 if res.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
