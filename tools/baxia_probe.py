"""探查 baxia 惩罚页 iframe 内部（同源，可直接读 DOM）。

    python tools/baxia_probe.py
    python tools/baxia_probe.py --iframe-id baxia-dialog-content
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

JS = r"""(opts) => {
  const id = opts && opts.id ? opts.id : 'baxia-dialog-content';
  const f = document.getElementById(id) ||
            Array.from(document.querySelectorAll('iframe')).find(x => (x.src||'').includes('tmd') || (x.src||'').includes('punish'));
  if (!f) return {error: 'no-iframe'};
  let d = null, err = '';
  try { d = f.contentDocument; } catch (e) { err = String(e); }
  if (!d) return {error: 'cross-origin', src: (f.src||'').slice(0,200), err};
  const vis = (e) => !!(e.offsetWidth || e.offsetHeight || e.getClientRects().length);
  return {
    src: (f.src || '').slice(0, 300),
    text: (d.body ? d.body.innerText : '').slice(0, 800),
    html_head: (d.documentElement ? d.documentElement.outerHTML : '').slice(0, 1200),
    ids: Array.from(d.querySelectorAll('[id]')).map(e => e.id).slice(0, 60),
    imgs: Array.from(d.querySelectorAll('img')).map(e => ({
      id: e.id, cls: String(e.className||'').slice(0,50),
      src: (e.src||'').slice(0,120), w: e.offsetWidth, h: e.offsetHeight,
      natural: [e.naturalWidth, e.naturalHeight], visible: vis(e),
    })).slice(0, 10),
    canvases: Array.from(d.querySelectorAll('canvas')).map(e => ({
      id: e.id, cls: String(e.className||'').slice(0,50),
      w: e.width, h: e.height, visible: vis(e),
    })).slice(0, 6),
    buttons: Array.from(d.querySelectorAll('button, [role=button], a')).map(e => ({
      tag: e.tagName, text: (e.innerText||'').trim().slice(0,30),
      cls: String(e.className||'').slice(0,50), visible: vis(e),
    })).slice(0, 15),
    inputs: Array.from(d.querySelectorAll('input')).map(e => ({
      id: e.id, type: e.type, maxlength: e.maxLength,
      cls: String(e.className||'').slice(0,50), visible: vis(e),
    })).slice(0, 10),
  };
}"""


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--env-id", default=secrets.get("bitbrowser.env_id", ""))
    ap.add_argument("--cdp", default="")
    ap.add_argument("--iframe-id", default="baxia-dialog-content")
    args = ap.parse_args()

    endpoint = args.cdp or BitBrowserClient().open_env(args.env_id).cdp_http
    pages = [t for t in cdp_raw.http_targets(endpoint) if t.get("type") == "page"]
    target = next((t for t in pages if "sign-up" in (t.get("url") or "")),
                  pages[0] if pages else None)
    if not target:
        print("没有页面")
        return 1
    with cdp_raw.CDP(endpoint, ws_url=target["webSocketDebuggerUrl"]) as page:
        page.enable()
        expr = f"({JS})({json.dumps({'id': args.iframe_id})})"
        data = page.evaluate(expr) or {}
        print(json.dumps(data, ensure_ascii=False, indent=2)[:6000])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
