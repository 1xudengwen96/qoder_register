"""把 Baxia 题干容器整块 dump 出来（文本 + 子节点 + 图片/canvas 尺寸）。"""

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
  const d = document.getElementById('baxia-dialog-content').contentDocument;
  const c = d.getElementById('click-captcha-question-container') ||
            d.getElementById('click-question-canvas');
  if (!c) return {error: 'no-container'};
  const kids = Array.from(c.querySelectorAll('*')).map(e => {
    const r = e.getBoundingClientRect();
    return {tag: e.tagName, id: e.id,
            cls: String(e.className || '').slice(0, 40),
            text: (e.innerText || '').trim().slice(0, 60),
            w: Math.round(r.width), h: Math.round(r.height),
            src: (e.getAttribute && (e.getAttribute('src') || '')).slice(0, 100),
            disp: getComputedStyle(e).display};
  }).slice(0, 25);
  return {
    html: (c.outerHTML || '').slice(0, 1200),
    text: (c.innerText || '').slice(0, 300),
    kids,
  };
})()"""


def main() -> int:
    endpoint = BitBrowserClient().open_env(
        secrets.get("bitbrowser.env_id")).cdp_http
    pages = [t for t in cdp_raw.http_targets(endpoint) if t.get("type") == "page"]
    target = next((t for t in pages if "sign-up" in (t.get("url") or "")), pages[0])
    with cdp_raw.CDP(endpoint, ws_url=target["webSocketDebuggerUrl"]) as raw:
        raw.enable()
        print(json.dumps(raw.evaluate(JS), ensure_ascii=False, indent=2)[:3500])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
