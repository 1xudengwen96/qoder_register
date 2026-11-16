"""在当前页面验证「能否把滑块面板点开并拿到图」。"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import secrets  # noqa: E402
from app.register import cdp_raw  # noqa: E402
from app.register.aliyun_slider_cdp import AliyunSliderCDP  # noqa: E402
from app.register.bitbrowser import BitBrowserClient  # noqa: E402
from app.register.cdp_page import CDPPage  # noqa: E402


def main() -> int:
    endpoint = BitBrowserClient().open_env(
        secrets.get("bitbrowser.env_id")).cdp_http
    pages = [t for t in cdp_raw.http_targets(endpoint) if t.get("type") == "page"]
    target = next((t for t in pages if "sign-up" in (t.get("url") or "")),
                  pages[0] if pages else None)
    print(f"页面 {(target.get('url') or '')[:80]}")

    with cdp_raw.CDP(endpoint, ws_url=target["webSocketDebuggerUrl"]) as raw:
        raw.enable()
        page = CDPPage.__new__(CDPPage)
        page.cdp = raw
        page.default_timeout = 60
        solver = AliyunSliderCDP(page, human=True)
        ok = solver.expand()
        print("expand:", ok)
        if ok:
            got = solver.grab_images()
            if got:
                bg, pz, data = got
                print(f"取图 OK: bg={bg.size} pz={pz.size} "
                      f"bgBox={data.get('bgBox')} pzBox={data.get('pzBox')}")
            else:
                print("取图失败")
        state = page.eval(
            "(() => { const w=document.querySelector('#aliyunCaptcha-window-float');"
            " const i=document.querySelector('#aliyunCaptcha-img');"
            " return {win: w? w.className : null,"
            " img: i? [i.complete, i.naturalWidth, i.getBoundingClientRect().width] : null}; })()")
        print("state:", state)
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
