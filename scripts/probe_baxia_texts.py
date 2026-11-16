"""列出 baxia iframe 里所有带文字的元素，以及所有 canvas 的尺寸与像素特征。"""

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
  const fr = document.getElementById('baxia-dialog-content');
  if (!fr) return {error: 'no-iframe'};
  const d = fr.contentDocument;
  const texts = [];
  d.querySelectorAll('div,span,p,em,i,b,strong,label,li,a,h1,h2,h3').forEach(e => {
    const t = (e.innerText || '').trim();
    if (!t) return;
    if (e.children.length > 0 && t.length > 40) return;
    const r = e.getBoundingClientRect();
    texts.push({tag: e.tagName, id: e.id,
                cls: String(e.className || '').slice(0, 40),
                text: t.slice(0, 80),
                w: Math.round(r.width), h: Math.round(r.height)});
  });
  const canvases = Array.from(d.querySelectorAll('canvas')).map(c => {
    let sig = '';
    try {
      const ctx = c.getContext('2d');
      const data = ctx.getImageData(0, 0, Math.min(c.width, 60), Math.min(c.height, 60)).data;
      let dark = 0, sum = 0;
      for (let i = 0; i < data.length; i += 4) {
        const v = (data[i] + data[i+1] + data[i+2]) / 3;
        sum += v; if (v < 128) dark++;
      }
      sig = 'avg=' + Math.round(sum / (data.length / 4)) + ' dark=' + dark;
    } catch (e) { sig = 'err:' + e; }
    return {id: c.id, cls: String(c.className || '').slice(0, 30),
            w: c.width, h: c.height, sig};
  });
  return {texts: texts.slice(0, 20), canvases};
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
