"""把 Baxia 题干图交给 2Captcha 做文字识别，确认「题干到底能不能读」。"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import secrets  # noqa: E402
from app.net.twocaptcha_click import TwoCaptchaClick, data_url_to_bytes  # noqa: E402
from app.register import cdp_raw  # noqa: E402
from app.register.baxia_cdp import JS_CAPTURE  # noqa: E402
from app.register.bitbrowser import BitBrowserClient  # noqa: E402
from app.register.cdp_page import CDPPage  # noqa: E402


def main() -> int:
    endpoint = BitBrowserClient().open_env(
        secrets.get("bitbrowser.env_id")).cdp_http
    pages = [t for t in cdp_raw.http_targets(endpoint) if t.get("type") == "page"]
    target = next((t for t in pages if "sign-up" in (t.get("url") or "")), pages[0])
    with cdp_raw.CDP(endpoint, ws_url=target["webSocketDebuggerUrl"]) as raw:
        raw.enable()
        page = CDPPage.__new__(CDPPage)
        page.cdp = raw
        page.default_timeout = 60
        data = page.eval(JS_CAPTURE) or {}
        if data.get("error"):
            print("取图失败:", data)
            return 1
        q = data.get("questionUrl") or ""
        grid = data.get("dataUrl") or ""
        print(f"题干图 base64 长度: {len(q)}  九宫格: {len(grid)}")
        Path("data/artifacts/baxia_question.jpg").write_bytes(
            data_url_to_bytes(q))
        print("已存 data/artifacts/baxia_question.jpg")

        cli = TwoCaptchaClick(secrets.get("captcha.twocaptcha_key", ""))
        try:
            text = cli.solve_text(data_url_to_bytes(q))
            print(f"2Captcha OCR 结果: {text!r}")
        except Exception as exc:
            print(f"OCR 失败: {type(exc).__name__}: {exc}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
