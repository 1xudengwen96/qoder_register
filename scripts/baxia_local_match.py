"""本地匹配 Baxia 九宫格：把「目标图案」拿去做模板匹配，挑最像的格子。

不依赖打码平台：目标图案画布与九宫格画布同源，做多尺度归一化相关即可。
"""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
from PIL import Image

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import secrets  # noqa: E402
from app.net.twocaptcha_click import data_url_to_bytes  # noqa: E402
from app.register import cdp_raw  # noqa: E402
from app.register import template_match  # noqa: E402
from app.register.baxia_cdp import JS_CAPTURE  # noqa: E402
from app.register.bitbrowser import BitBrowserClient  # noqa: E402
from app.register.cdp_page import CDPPage  # noqa: E402

JS_ALL = r"""(() => {
  const d = document.getElementById('baxia-dialog-content').contentDocument;
  const out = {grids: [], desc: ''};
  for (let i = 0; i < 9; i++) {
    const g = d.getElementById('click-grid-' + i);
    if (g) out.grids.push({i, url: g.toDataURL('image/png')});
  }
  const desc = Array.from(d.querySelectorAll('canvas')).find(
    c => !/^click-grid-/.test(c.id) && c.id !== 'click-question-canvas' && c.width > 20);
  if (desc) out.desc = desc.toDataURL('image/png');
  return out;
})()"""


def to_np(data_url: str) -> np.ndarray:
    import io

    return np.array(Image.open(io.BytesIO(data_url_to_bytes(data_url))).convert("RGB"))


def score_cell(desc: np.ndarray, cell: np.ndarray) -> float:
    """目标图案在格子里找得到吗：多尺度掩膜相关取最大。"""
    best = 0.0
    for scale in (0.5, 0.7, 0.9, 1.1, 1.3):
        h = max(8, int(desc.shape[0] * scale))
        w = max(8, int(desc.shape[1] * scale))
        if h > cell.shape[0] or w > cell.shape[1]:
            continue
        tpl = np.array(Image.fromarray(desc).resize((w, h)))
        mask = np.ones((h, w), dtype=np.float32)
        try:
            resp = template_match.match_masked(cell, tpl, mask, "ccorr")
            v = float(np.nanmax(resp))
        except Exception:
            v = 0.0
        best = max(best, v)
    return best


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
        data = page.eval(JS_ALL) or {}
        if not data.get("desc"):
            print("没拿到目标图案画布")
            return 1
        desc = to_np(data["desc"])
        print(f"目标图案 {desc.shape}")
        scores = []
        for g in data["grids"]:
            cell = to_np(g["url"])
            s = score_cell(desc, cell)
            scores.append((g["i"], s))
        scores.sort(key=lambda x: -x[1])
        print("各格得分:")
        for i, s in scores:
            print(f"  格{i}: {s:.4f}")
        print("\n推荐:", [i for i, s in scores[:3]])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
