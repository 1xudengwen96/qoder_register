"""同一张拼图：本地识别 vs 2Captcha，交叉验证目标 x。"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import secrets  # noqa: E402
from app.net.twocaptcha_dragdrop import TwoCaptchaDragDrop  # noqa: E402
from app.register import captcha_vision  # noqa: E402
from app.register import cdp_raw  # noqa: E402
from app.register.aliyun_slider_cdp import AliyunSliderCDP  # noqa: E402
from app.register.bitbrowser import BitBrowserClient  # noqa: E402
from app.register.cdp_page import CDPPage  # noqa: E402


def main() -> int:
    endpoint = BitBrowserClient().open_env(
        secrets.get("bitbrowser.env_id")).cdp_http
    pages = [t for t in cdp_raw.http_targets(endpoint) if t.get("type") == "page"]
    page = next((t for t in pages if "sign-up" in (t.get("url") or "")), pages[0])
    with cdp_raw.CDP(endpoint, ws_url=page["webSocketDebuggerUrl"],
                     timeout=60) as raw:
        raw.enable()
        pg = CDPPage.__new__(CDPPage)
        pg.cdp = raw
        pg.default_timeout = 60
        solver = AliyunSliderCDP(pg, human=True)
        solver.expand()
        got = solver.grab_images()
        if got is None:
            print("取图失败")
            return 1
        bg, pz, data = got
        pz_c, _mask, bbox = captcha_vision.crop_to_alpha(pz)
        print(f"背景 {bg.size}  拼图块 {pz_c.size}  bbox_y={bbox[1]}")

        # 本地
        gap = captcha_vision.find_gap(bg, pz, display_w=None)
        print(f"本地: ok={gap.ok} x_px={gap.x_px} conf={gap.conf:.3f} "
              f"method={gap.method}")
        print(f"      明细: {gap.detail[:200]}")

        # 2Captcha
        cli = TwoCaptchaDragDrop(secrets.get("captcha.twocaptcha_key", ""))
        sol = cli.solve(bg, pz_c)
        print(f"2Captcha: ok={sol.ok} x={sol.x} y={sol.y} "
              f"cost=${sol.cost:.4f} err={sol.error}")
        if sol.ok:
            left = sol.x - pz_c.size[0] / 2
            print(f"  按「中心-半宽」→ 左沿 {left:.1f}")
            print(f"  与本地 x_px 差: {abs(left - gap.x_px):.1f}px")
            print(f"  y 与 bbox_y 差: {abs(sol.y - bbox[1]):.1f}px")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
