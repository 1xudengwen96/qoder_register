"""找背景图实际渲染的元素（CSS background-image），拿显示宽高。"""

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
  const out = [];
  document.querySelectorAll('*').forEach(e => {
    const bg = getComputedStyle(e).backgroundImage || '';
    if (bg && bg.includes('aliyuncs')) {
      const r = e.getBoundingClientRect();
      out.push({id: e.id, cls: String(e.className || '').slice(0, 60),
                w: Math.round(r.width), h: Math.round(r.height),
                x: Math.round(r.x), y: Math.round(r.y),
                bg: bg.slice(0, 100)});
    }
  });
  const pz = document.querySelector('#aliyunCaptcha-puzzle');
  const pzParent = pz ? pz.parentElement : null;
  return {
    bgElements: out.slice(0, 8),
    puzzleStyle: pz ? {left: pz.style.left, top: pz.style.top,
                       w: pz.style.width, h: pz.style.height,
                       disp: getComputedStyle(pz).display} : null,
    puzzleParent: pzParent ? {
      cls: String(pzParent.className || '').slice(0, 60),
      w: Math.round(pzParent.getBoundingClientRect().width),
      h: Math.round(pzParent.getBoundingClientRect().height)} : null,
  };
})()"""


def main() -> int:
    endpoint = BitBrowserClient().open_env(
        secrets.get("bitbrowser.env_id")).cdp_http
    pages = [t for t in cdp_raw.http_targets(endpoint) if t.get("type") == "page"]
    target = next((t for t in pages if "sign-up" in (t.get("url") or "")), pages[0])
    with cdp_raw.CDP(endpoint, ws_url=target["webSocketDebuggerUrl"]) as raw:
        raw.enable()
        print(json.dumps(raw.evaluate(JS), ensure_ascii=False, indent=2)[:2500])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
