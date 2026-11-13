"""滑块识别方案对比：先把拼图块裁到有效 bbox，再试多种识别器。

关键前提（实测）
----------------
``#aliyunCaptcha-puzzle`` 是 52×200 的 RGBA 画布，但 **81% 透明**，
真正有内容的区域是 bbox（实测 x=2..50, y=124..173，即 48×49）。
不裁 bbox 直接匹配必然失败。

对比四种方案
------------
A. ddddocr.slide_match（simple_target=True）
B. ddddocr.slide_match（simple_target=False）
C. OpenCV matchTemplate + 掩膜（TM_CCORR_NORMED + mask）
D. OpenCV Canny 边缘 + 掩膜匹配

输出每个方案的 x 和置信度，以及可视化标注。

用法：
    python tools/test_slider_solvers.py
"""

from __future__ import annotations

import argparse
import io
import sys
from pathlib import Path

import cv2
import numpy as np
from PIL import Image, ImageDraw

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def crop_piece(piece: Image.Image, pad: int = 2) -> tuple[Image.Image, Image.Image, tuple]:
    """裁到 alpha 不透明 bbox。返回 (rgb裁图, alpha掩膜图, bbox)。"""
    rgba = np.array(piece.convert("RGBA"))
    alpha = rgba[:, :, 3]
    mask = alpha > 32
    if not mask.any():
        return piece.convert("RGB"), Image.new("L", piece.size, 255), (0, 0, *piece.size)
    ys, xs = np.where(mask)
    x0, x1 = max(0, int(xs.min()) - pad), min(piece.size[0], int(xs.max()) + 1 + pad)
    y0, y1 = max(0, int(ys.min()) - pad), min(piece.size[1], int(ys.max()) + 1 + pad)
    sub = rgba[y0:y1, x0:x1]
    rgb = Image.fromarray(sub[:, :, :3])
    msk = Image.fromarray(sub[:, :, 3])
    return rgb, msk, (x0, y0, x1, y1)


def to_png_bytes(img: Image.Image) -> bytes:
    b = io.BytesIO()
    img.save(b, format="PNG")
    return b.getvalue()


# ---------------------------------------------------------------- A/B
def solve_ddddocr(bg: Image.Image, piece: Image.Image) -> list[dict]:
    import ddddocr

    out = []
    bg_b = to_png_bytes(bg.convert("RGB"))
    pz_b = to_png_bytes(piece)

    for label, simple in (("ddddocr-simple", True), ("ddddocr-full", False)):
        try:
            det = ddddocr.DdddOcr(det=False, ocr=False, show_ad=False)
            r = det.slide_match(pz_b, bg_b, simple_target=simple)
            out.append({"method": label, "raw": r,
                        "x": r.get("target_x"), "y": r.get("target_y"),
                        "conf": r.get("confidence", 0.0)})
        except Exception as exc:
            out.append({"method": label, "x": None, "conf": 0.0,
                        "error": f"{type(exc).__name__}: {exc}"})
    return out


# ---------------------------------------------------------------- C/D
def solve_cv(bg: Image.Image, piece: Image.Image,
             mask: Image.Image) -> list[dict]:
    bg_bgr = cv2.cvtColor(np.array(bg.convert("RGB")), cv2.COLOR_RGB2BGR)
    pz_bgr = cv2.cvtColor(np.array(piece.convert("RGB")), cv2.COLOR_RGB2BGR)
    m = np.array(mask.convert("L"))
    m = ((m > 32).astype(np.uint8)) * 255

    out = []
    bh, bw = bg_bgr.shape[:2]
    ph, pw = pz_bgr.shape[:2]
    if ph > bh or pw > bw:
        return [{"method": "cv-skip", "x": None, "conf": 0.0,
                 "error": f"模板 {pw}x{ph} 大于背景 {bw}x{bh}"}]

    # C1: 彩色 CCORR + mask
    try:
        mask3 = cv2.merge([m] * 3)
        r = cv2.matchTemplate(bg_bgr, pz_bgr, cv2.TM_CCORR_NORMED, mask=mask3)
        r = np.nan_to_num(r, nan=-1.0, posinf=-1.0, neginf=-1.0)
        _, mx, _, ml = cv2.minMaxLoc(r)
        out.append({"method": "cv-ccorr-color", "x": int(ml[0]), "y": int(ml[1]),
                    "conf": float(mx)})
    except Exception as exc:
        out.append({"method": "cv-ccorr-color", "x": None, "conf": 0.0,
                    "error": str(exc)[:100]})

    # C2: 灰度 CCORR + mask（对颜色变化最稳）
    try:
        bg_g = cv2.cvtColor(bg_bgr, cv2.COLOR_BGR2GRAY)
        pz_g = cv2.cvtColor(pz_bgr, cv2.COLOR_BGR2GRAY)
        r = cv2.matchTemplate(bg_g, pz_g, cv2.TM_CCORR_NORMED, mask=m)
        r = np.nan_to_num(r, nan=-1.0, posinf=-1.0, neginf=-1.0)
        _, mx, _, ml = cv2.minMaxLoc(r)
        out.append({"method": "cv-ccorr-gray", "x": int(ml[0]), "y": int(ml[1]),
                    "conf": float(mx)})
    except Exception as exc:
        out.append({"method": "cv-ccorr-gray", "x": None, "conf": 0.0,
                    "error": str(exc)[:100]})

    # D: Canny 边缘 + mask
    try:
        bg_e = cv2.Canny(cv2.cvtColor(bg_bgr, cv2.COLOR_BGR2GRAY), 50, 130)
        pz_e = cv2.Canny(cv2.cvtColor(pz_bgr, cv2.COLOR_BGR2GRAY), 50, 130)
        r = cv2.matchTemplate(bg_e, pz_e, cv2.TM_CCORR_NORMED, mask=m)
        r = np.nan_to_num(r, nan=-1.0, posinf=-1.0, neginf=-1.0)
        _, mx, _, ml = cv2.minMaxLoc(r)
        out.append({"method": "cv-canny-mask", "x": int(ml[0]), "y": int(ml[1]),
                    "conf": float(mx)})
    except Exception as exc:
        out.append({"method": "cv-canny-mask", "x": None, "conf": 0.0,
                    "error": str(exc)[:100]})

    # D2: 平方差（TM_SQDIFF_NORMED，越小越好）
    try:
        bg_g = cv2.cvtColor(bg_bgr, cv2.COLOR_BGR2GRAY).astype(np.float32)
        pz_g = cv2.cvtColor(pz_bgr, cv2.COLOR_BGR2GRAY).astype(np.float32)
        r = cv2.matchTemplate(bg_g, pz_g, cv2.TM_SQDIFF_NORMED, mask=m)
        r = np.nan_to_num(r, nan=1.0, posinf=1.0, neginf=1.0)
        mn, _, ml, _ = cv2.minMaxLoc(r)
        out.append({"method": "cv-sqdiff-gray", "x": int(ml[0]), "y": int(ml[1]),
                    "conf": float(1.0 - mn)})
    except Exception as exc:
        out.append({"method": "cv-sqdiff-gray", "x": None, "conf": 0.0,
                    "error": str(exc)[:100]})

    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--bg", default=str(ROOT / "data/puzzle_css/bg_raw.png"))
    ap.add_argument("--piece", default=str(ROOT / "data/puzzle_css/puzzle_raw.png"))
    args = ap.parse_args()

    bgp, pzp = Path(args.bg), Path(args.piece)
    if not bgp.exists() or not pzp.exists():
        print(f"缺图：{bgp} / {pzp}")
        print("先跑 tools/diag_puzzle_css.py")
        return 2

    bg = Image.open(bgp)
    pz_full = Image.open(pzp)
    print(f"背景 {bg.size} {bg.mode}   拼图(原) {pz_full.size} {pz_full.mode}")

    pz_c, msk_c, bbox = crop_piece(pz_full)
    print(f"拼图(裁bbox) {pz_c.size}   bbox={bbox}")
    print(f"  → 有效内容 {bbox[2]-bbox[0]}×{bbox[3]-bbox[1]}，"
          f"位于原画布 y={bbox[1]}..{bbox[3]}\n")

    results: list[dict] = []
    print("=== A/B: ddddocr（裁 bbox 后）===")
    results += solve_ddddocr(bg, pz_c)
    for r in results:
        print(f"  {r['method']:<18} x={r.get('x')} y={r.get('y')} "
              f"conf={r.get('conf', 0):.3f} {r.get('error','')}")

    print("\n=== C/D: OpenCV 掩膜匹配（裁 bbox 后）===")
    cvres = solve_cv(bg, pz_c, msk_c)
    results += cvres
    for r in cvres:
        print(f"  {r['method']:<18} x={r.get('x')} y={r.get('y')} "
              f"conf={r.get('conf', 0):.3f} {r.get('error','')}")

    # ---- 汇总投票 ----
    print("\n=== 汇总 ===")
    xs = [r["x"] for r in results if isinstance(r.get("x"), int)]
    if xs:
        # 去掉明显离群（右边缘伪影）
        max_x = bg.size[0] - pz_c.size[0]
        xs_ok = [x for x in xs if 0 <= x <= max_x]
        from collections import Counter
        c = Counter(round(x / 10) * 10 for x in xs_ok)
        print(f"  所有 x: {xs}")
        print(f"  分桶统计(每10px): {dict(c)}")
        if c:
            best_bucket, cnt = c.most_common(1)[0]
            print(f"  最集中的位置: ~{best_bucket}px（{cnt} 票）")

    # ---- 可视化：每个方案画一条线 ----
    vis = bg.convert("RGB").copy()
    d = ImageDraw.Draw(vis)
    colors = [(255, 0, 0), (0, 150, 255), (0, 200, 0), (255, 160, 0),
              (180, 0, 255), (0, 200, 200), (255, 0, 180)]
    yoff = 0
    for idx, r in enumerate(results):
        x = r.get("x")
        if not isinstance(x, int):
            continue
        c = colors[idx % len(colors)]
        d.line([x, 0, x, vis.size[1]], fill=c, width=2)
        d.text((max(0, x - 60), 2 + yoff), f"{r['method'][:14]}", fill=c)
        yoff += 11

    outp = ROOT / "data" / "captcha_diag" / "solvers_compare.png"
    vis.save(outp)
    print(f"\n对比图: {outp}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
