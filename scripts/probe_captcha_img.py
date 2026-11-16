"""看验证码图片的 src / 能否取到，以及页面里的 fetch 结果。"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import secrets  # noqa: E402
from app.register import cdp_raw  # noqa: E402
from app.register.bitbrowser import BitBrowserClient  # noqa: E402

JS = r"""(async () => {
  const img = document.querySelector('#aliyunCaptcha-img');
  const pz = document.querySelector('#aliyunCaptcha-puzzle');
  const out = {
    imgSrc: img ? (img.src || '').slice(0, 160) : null,
    imgCurrent: img ? (img.currentSrc || '').slice(0, 160) : null,
    imgComplete: img ? img.complete : null,
    imgNatural: img ? [img.naturalWidth, img.naturalHeight] : null,
    pzSrc: pz ? (pz.src || '').slice(0, 160) : null,
    pzNatural: pz ? [pz.naturalWidth, pz.naturalHeight] : null,
  };
  try {
    const r = await fetch(out.imgSrc, {cache: 'no-store'});
    const b = await r.blob();
    out.fetch = {status: r.status, len: b.size, type: b.type};
  } catch (e) { out.fetch = 'error: ' + e; }
  return out;
})()"""


def main() -> int:
    endpoint = BitBrowserClient().open_env(
        secrets.get("bitbrowser.env_id")).cdp_http
    pages = [t for t in cdp_raw.http_targets(endpoint) if t.get("type") == "page"]
    target = next((t for t in pages if "sign-up" in (t.get("url") or "")),
                  pages[0])
    with cdp_raw.CDP(endpoint, ws_url=target["webSocketDebuggerUrl"]) as raw:
        raw.enable()
        data = raw.evaluate(JS, await_promise=True) or {}
        print(json.dumps(data, ensure_ascii=False, indent=2)[:2000])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
