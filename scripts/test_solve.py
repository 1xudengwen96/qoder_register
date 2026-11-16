"""在当前滑块页面真跑一次识别+拖动，验证是否通过。"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import secrets  # noqa: E402
from app.net.twocaptcha_dragdrop import TwoCaptchaDragDrop  # noqa: E402
from app.register import cdp_raw  # noqa: E402
from app.register.aliyun_slider_cdp import AliyunSliderCDP  # noqa: E402
from app.register.bitbrowser import BitBrowserClient  # noqa: E402
from app.register.cdp_page import CDPPage  # noqa: E402


def main() -> int:
    max_try = int(sys.argv[1]) if len(sys.argv) > 1 else 1
    endpoint = BitBrowserClient().open_env(
        secrets.get("bitbrowser.env_id")).cdp_http
    pages = [t for t in cdp_raw.http_targets(endpoint) if t.get("type") == "page"]
    target = next((t for t in pages if "sign-up" in (t.get("url") or "")), pages[0])
    with cdp_raw.CDP(endpoint, ws_url=target["webSocketDebuggerUrl"]) as raw:
        raw.enable()
        page = CDPPage.__new__(CDPPage)
        page.cdp = raw
        page.default_timeout = 60
        paid = TwoCaptchaDragDrop(secrets.get("captcha.twocaptcha_key", ""))
        solver = AliyunSliderCDP(page, human=True, paid_client=paid,
                                 prefer_paid=True, proxy="http://127.0.0.1:7897")
        res = solver.solve(max_attempts=max_try)
        print(json.dumps({
            "ok": res.ok, "attempts": res.attempts, "target": res.target_x,
            "final": res.final_x, "method": res.method, "detail": res.detail,
            "cost": round(res.cost, 4), "trail": res.trail,
        }, ensure_ascii=False, indent=2))
    return 0 if res.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
