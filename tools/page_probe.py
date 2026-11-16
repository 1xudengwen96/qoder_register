"""对当前注册页做一次全面探查：找出「还有没有一层验证码、是哪一家」。

    python tools/page_probe.py                    # 用配置里的环境
    python tools/page_probe.py --cdp 127.0.0.1:52990
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import secrets  # noqa: E402
from app.register import cdp_raw  # noqa: E402
from app.register.bitbrowser import BitBrowserClient  # noqa: E402

JS_PROBE = r"""(() => {
  const vis = (e) => !!(e.offsetWidth || e.offsetHeight || e.getClientRects().length);
  const brief = (e) => ({
    tag: e.tagName, id: e.id,
    cls: String(e.className || '').slice(0, 70),
    text: (e.innerText || '').trim().slice(0, 40),
    visible: vis(e),
    w: Math.round(e.getBoundingClientRect().width),
    h: Math.round(e.getBoundingClientRect().height),
    src: (e.getAttribute && (e.getAttribute('src') || '')).slice(0, 90),
  });
  const q = (sel) => Array.from(document.querySelectorAll(sel)).slice(0, 12).map(brief);
  return {
    url: location.href,
    title: document.title,
    text: (document.body ? document.body.innerText : '').slice(0, 900),
    iframes: Array.from(document.querySelectorAll('iframe')).map(e => ({
      src: (e.src || '').slice(0, 160), w: e.offsetWidth, h: e.offsetHeight,
      visible: vis(e), id: e.id, cls: String(e.className || '').slice(0, 50),
    })),
    aliyun: q('[id*=aliyunCaptcha], [class*=aliyunCaptcha], #captcha-element'),
    captchaish: q('[id*=captcha], [class*=captcha], [id*=verify], [class*=verify],'
                  + '[class*=challenge], [class*=slide], [class*=puzzle],'
                  + '[class*=geetest], [class*=turnstile], [class*=hcaptcha]'),
    canvases: q('canvas'),
    data_imgs: Array.from(document.querySelectorAll('img[src^="data:image"]'))
      .slice(0, 6).map(brief),
    buttons: q('button, [role=button]'),
    scripts: Array.from(document.scripts).map(s => s.src || '')
      .filter(s => /captcha|verify|challenge|feilin|geetest|hcaptcha|turnstile|aliyun/i.test(s))
      .slice(0, 8),
    inputs: q('input'),
  };
})()"""


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--env-id", default=secrets.get("bitbrowser.env_id", ""))
    ap.add_argument("--cdp", default="")
    args = ap.parse_args()

    endpoint = args.cdp
    if not endpoint:
        client = BitBrowserClient()
        endpoint = client.open_env(args.env_id).cdp_http
    print(f"CDP {endpoint}")

    pages = [t for t in cdp_raw.http_targets(endpoint) if t.get("type") == "page"]
    print(f"页面 {len(pages)} 个:")
    for t in pages:
        print(f"  - {(t.get('url') or '')[:100]}")

    target = next((t for t in pages if "sign-up" in (t.get("url") or "")),
                  pages[-1] if pages else None)
    if not target:
        print("没有页面")
        return 1
    print(f"\n探查: {(target.get('url') or '')[:100]}")

    with cdp_raw.CDP(endpoint, ws_url=target["webSocketDebuggerUrl"]) as page:
        page.enable()
        data = page.evaluate(JS_PROBE) or {}
        print(json.dumps(data, ensure_ascii=False, indent=2)[:6000])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
