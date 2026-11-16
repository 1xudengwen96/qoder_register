"""看滑块窗口里各元素的真实尺寸与类名，判断「就绪」该怎么判。"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import secrets  # noqa: E402
from app.register import cdp_raw  # noqa: E402
from app.register.bitbrowser import BitBrowserClient  # noqa: E402

JS = r"""(() => {
  const pick = (sel) => {
    const e = document.querySelector(sel);
    if (!e) return null;
    const r = e.getBoundingClientRect();
    return {sel, cls: String(e.className || '').slice(0, 60),
            w: Math.round(r.width), h: Math.round(r.height),
            x: Math.round(r.x), y: Math.round(r.y),
            disp: getComputedStyle(e).display,
            vis: getComputedStyle(e).visibility};
  };
  return [
    pick('#aliyunCaptcha-window-float'),
    pick('#aliyunCaptcha-img'),
    pick('#aliyunCaptcha-puzzle'),
    pick('#aliyunCaptcha-sliding-body'),
    pick('#aliyunCaptcha-sliding-slider'),
    pick('#aliyunCaptcha-btn-refresh'),
  ];
})()"""


def main() -> int:
    endpoint = BitBrowserClient().open_env(
        secrets.get("bitbrowser.env_id")).cdp_http
    pages = [t for t in cdp_raw.http_targets(endpoint) if t.get("type") == "page"]
    target = next((t for t in pages if "sign-up" in (t.get("url") or "")), pages[0])
    with cdp_raw.CDP(endpoint, ws_url=target["webSocketDebuggerUrl"]) as raw:
        raw.enable()
        print(json.dumps(raw.evaluate(JS), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
