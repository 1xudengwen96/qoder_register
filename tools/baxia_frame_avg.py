"""探查：Baxia 题干 canvas 动态噪点 —— 多帧平均能否重建目标图案。

用法：
    python tools/baxia_frame_avg.py --cdp http://127.0.0.1:54999 --frames 30
输出：
    data/artifacts/baxia_avg.png    帧平均结果
    data/artifacts/baxia_avg_dbg.png  原始单帧对照
"""

from __future__ import annotations

import argparse
import base64
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.register import cdp_raw  # noqa: E402

JS = r"""async (opts) => {
  const FRAMES = (opts && opts.frames) || 30;
  const STEP = 40;                     // 每帧间隔 ms
  const f = document.getElementById('baxia-dialog-content') ||
    Array.from(document.querySelectorAll('iframe')).find(
      x => (x.src || '').includes('tmd') || (x.src || '').includes('punish'));
  if (!f) return {error: 'no-iframe'};
  const d = f.contentDocument;
  if (!d) return {error: 'cross-origin'};
  const desc = Array.from(d.querySelectorAll('canvas')).find(
    c => !/^click-grid-/.test(c.id) && c.id !== 'click-question-canvas' &&
         c.width > 20);
  if (!desc) return {error: 'no-desc-canvas',
    canvases: Array.from(d.querySelectorAll('canvas')).map(c => (
      {id: c.id, w: c.width, h: c.height}))};

  const w = desc.width, h = desc.height;
  const acc = new Float64Array(w * h * 3);
  const tmp = d.createElement('canvas');
  tmp.width = w; tmp.height = h;
  const tctx = tmp.getContext('2d', {willReadFrequently: true});
  let n = 0;
  for (let i = 0; i < FRAMES; i++) {
    tctx.clearRect(0, 0, w, h);
    tctx.drawImage(desc, 0, 0);
    const px = tctx.getImageData(0, 0, w, h).data;
    for (let p = 0, q = 0; p < px.length; p += 4, q += 3) {
      acc[q] += px[p]; acc[q + 1] += px[p + 1]; acc[q + 2] += px[p + 2];
    }
    n++;
    await new Promise(r => setTimeout(r, STEP));
  }
  // 单帧对照（最后抓的那帧）
  const single = tmp.toDataURL('image/png');
  // 平均帧
  const out = d.createElement('canvas');
  out.width = w; out.height = h;
  const octx = out.getContext('2d');
  const oimg = octx.createImageData(w, h);
  const od = oimg.data;
  for (let p = 0, q = 0; p < od.length; p += 4, q += 3) {
    od[p] = acc[q] / n; od[p + 1] = acc[q + 1] / n;
    od[p + 2] = acc[q + 2] / n; od[p + 3] = 255;
  }
  octx.putImageData(oimg, 0, 0);
  return {ok: true, w, h, frames: n,
          avg: out.toDataURL('image/png'), single};
}"""


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--cdp", default="http://127.0.0.1:54999")
    ap.add_argument("--frames", type=int, default=30)
    args = ap.parse_args()

    pages = [t for t in cdp_raw.http_targets(args.cdp)
             if t.get("type") == "page"]
    target = next((t for t in pages if "sign-up" in (t.get("url") or "")),
                  pages[0] if pages else None)
    if not target:
        print("没有页面 target")
        return 1
    with cdp_raw.CDP(args.cdp,
                     ws_url=target["webSocketDebuggerUrl"]) as page:
        page.enable()
        expr = (f"(async()=>{{const fn={JS};"
                f"return await fn({{frames:{args.frames}}})}})()")
        data = page.evaluate(expr, await_promise=True) or {}
    if data.get("error"):
        print(json.dumps(data, ensure_ascii=False)[:2000])
        return 2
    outdir = ROOT / "data" / "artifacts"
    outdir.mkdir(parents=True, exist_ok=True)
    for key, name in (("avg", "baxia_avg.png"), ("single", "baxia_avg_dbg.png")):
        b64 = data.get(key, "")
        if "," in b64:
            (outdir / name).write_bytes(base64.b64decode(b64.split(",", 1)[1]))
            print(f"已保存 {outdir / name}")
    print(f"w={data.get('w')} h={data.get('h')} frames={data.get('frames')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
