"""用已抓的图测试缺口定位算法（多方法对比）。

用法：
    python tools/test_vision.py
    python tools/test_vision.py --bg data/captcha_diag/bg.png --piece data/captcha_diag/piece.png
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.register import captcha_vision  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--bg", default=str(ROOT / "data/captcha_diag/bg.png"))
    ap.add_argument("--piece", default=str(ROOT / "data/captcha_diag/piece.png"))
    args = ap.parse_args()

    bgp, pzp = Path(args.bg), Path(args.piece)
    if not bgp.exists() or not pzp.exists():
        print(f"图片不存在：{bgp} / {pzp}")
        return 2

    bg = Image.open(bgp)
    pz = Image.open(pzp)
    print(f"背景 {bg.size} {bg.mode}   拼图 {pz.size} {pz.mode}\n")

    # ---- NCC 模板匹配 ----
    print("=== NCC 模板匹配 ===")
    m = captcha_vision.match_template(bg, pz)
    print(f"  x={m['x']}  y={m['y']}  conf={m['conf']:.4f}  "
          f"tpl={m['tpl_w']}×{m['tpl_h']}")
    print(f"  前 {len(m.get('peaks', []))} 个峰:")
    for px, py, s in m.get("peaks", []):
        print(f"    x={px:<4} y={py:<4} score={s:.4f}")

    # ---- 边缘 NCC ----
    print("\n=== 边缘 NCC ===")
    e = captcha_vision.match_edge(bg, pz)
    print(f"  x={e['x']}  y={e['y']}  conf={e['conf']:.4f}")

    # ---- 列亮度 ----
    print("\n=== 列亮度（旧方法）===")
    prim = captcha_vision.find_gap_columns(bg, piece_w=pz.size[0])
    print(f"  left={prim['left']}  right={prim['right']}  strength={prim['strength']:.1f}")
    print(f"  runs={prim.get('runs')}")

    # ---- 综合 ----
    print("\n=== 综合结果 ===")
    r = captcha_vision.solve(bg, pz, display_w=300.0)
    for k in ("x", "x_px", "conf", "method", "ncc_conf", "edge_x",
              "edge_conf", "agree", "tpl_w", "scale"):
        if k in r:
            print(f"  {k:<10} = {r[k]}")

    # ---- 可视化 ----
    vis = bg.convert("RGB").copy()
    d = ImageDraw.Draw(vis)
    colors = {"ncc": (255, 0, 0), "edge": (0, 160, 255), "bright": (0, 200, 0)}
    if m["x"] >= 0:
        tw = m["tpl_w"]
        d.rectangle([m["x"], 0, m["x"] + tw, vis.size[1] - 1],
                    outline=colors["ncc"], width=2)
        d.text((m["x"] + 2, 2), "NCC", fill=colors["ncc"])
    if e["x"] >= 0:
        d.line([e["x"], 0, e["x"], vis.size[1]], fill=colors["edge"], width=2)
        d.text((e["x"] + 2, 16), "EDGE", fill=colors["edge"])
    if prim["left"] >= 0:
        d.line([prim["left"], 0, prim["left"], vis.size[1]],
               fill=colors["bright"], width=2)
        d.text((prim["left"] + 2, 30), "BRIGHT", fill=colors["bright"])

    outp = ROOT / "data" / "captcha_diag" / "vision_result.png"
    vis.save(outp)
    print(f"\n可视化已保存: {outp}")

    # 拼图块放大 3 倍另存，方便肉眼比对
    pz_big = pz.resize((pz.size[0] * 3, pz.size[1] * 3), Image.NEAREST)
    pz_big.save(ROOT / "data" / "captcha_diag" / "piece_3x.png")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
