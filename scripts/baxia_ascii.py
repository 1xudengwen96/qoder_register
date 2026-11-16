"""把 desc 画布和九宫格降采样成 ASCII，肉眼粗看内容类型。"""

from __future__ import annotations

import io
import sys
from pathlib import Path

import numpy as np
from PIL import Image

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import secrets  # noqa: E402
from app.net.twocaptcha_click import data_url_to_bytes  # noqa: E402
from app.register import cdp_raw  # noqa: E402
from app.register.baxia_cdp import JS_CAPTURE  # noqa: E402
from app.register.bitbrowser import BitBrowserClient  # noqa: E402
from app.register.cdp_page import CDPPage  # noqa: E402

JS = r"""(() => {
  const d = document.getElementById('baxia-dialog-content').contentDocument;
  const out = {grids: [], desc: '', question: ''};
  for (let i = 0; i < 9; i++) {
    const g = d.getElementById('click-grid-' + i);
    if (g) out.grids.push({i, url: g.toDataURL('image/png')});
  }
  const desc = Array.from(d.querySelectorAll('canvas')).find(
    c => !/^click-grid-/.test(c.id) && c.id !== 'click-question-canvas' && c.width > 20);
  if (desc) out.desc = desc.toDataURL('image/png');
  const q = d.getElementById('click-question-canvas');
  if (q) out.question = q.toDataURL('image/png');
  return out;
})()"""

RAMP = " .:-=+*#%@"


def ascii_art(data_url: str, cols: int = 24) -> str:
    im = Image.open(io.BytesIO(data_url_to_bytes(data_url))).convert("L")
    w, h = im.size
    rows = max(1, int(cols * h / max(w, 1) / 2))
    im = im.resize((cols, rows))
    a = np.array(im)
    lines = []
    for row in a:
        lines.append("".join(
            RAMP[min(len(RAMP) - 1, int(v) * len(RAMP) // 256)] for v in row))
    return f"({w}x{h})\n" + "\n".join(lines)


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
        data = page.eval(JS) or {}
        if data.get("desc"):
            print("=== desc 画布 ===")
            print(ascii_art(data["desc"], 28))
        if data.get("question"):
            print("\n=== question 画布 ===")
            print(ascii_art(data["question"], 56))
        for g in data.get("grids") or []:
            print(f"\n=== 格 {g['i']} ===")
            print(ascii_art(g["url"], 24))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
