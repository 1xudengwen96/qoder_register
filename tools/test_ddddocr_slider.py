"""用 ddddocr 的 slide_match 识别阿里滑块缺口（现成库，非自研）。

ddddocr 内置 ``slide_match(target, background)``，专为标准滑块设计：
  输入：拼图块（带 alpha）+ 背景图
  输出：{"target_x": ..., "target_y": ...} 或带 confidence

这是成熟库，不用自己写 NCC/边缘检测。

用法：
    python tools/test_ddddocr_slider.py
    python tools/test_ddddocr_slider.py --bg X.png --piece Y.png
"""

from __future__ import annotations

import argparse
import base64
import io
import sys
from pathlib import Path

from PIL import Image

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def try_ddddocr(bg_bytes: bytes, pz_bytes: bytes) -> dict:
    import ddddocr

    out: dict = {}
    # 标准用法
    try:
        det = ddddocr.DdddOcr(det=False, ocr=False, show_ad=False)
        r = det.slide_match(pz_bytes, bg_bytes, simple_target=True)
        out["slide_match"] = r
    except Exception as exc:
        out["slide_match_error"] = f"{type(exc).__name__}: {exc}"

    # 带 confidence 的用法（新版 ddddocr 支持）
    try:
        det2 = ddddocr.DdddOcr(det=False, ocr=False, show_ad=False)
        r2 = det2.slide_match(pz_bytes, bg_bytes, simple_target=False)
        out["slide_match_full"] = r2
    except Exception as exc:
        out["slide_match_full_error"] = f"{type(exc).__name__}: {exc}"

    # 对比度增强版
    try:
        det3 = ddddocr.DdddOcr(det=False, ocr=False, show_ad=False)
        r3 = det3.slide_comparison(pz_bytes, bg_bytes)
        out["slide_comparison"] = r3
    except Exception as exc:
        out["slide_comparison_error"] = f"{type(exc).__name__}: {exc}"

    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--bg", default=str(ROOT / "data/captcha_diag/bg.png"))
    ap.add_argument("--piece", default=str(ROOT / "data/captcha_diag/piece.png"))
    ap.add_argument("--raw", action="store_true",
                    help="用 data/puzzle_css 下的原始图（带 alpha）")
    args = ap.parse_args()

    if args.raw:
        bgp = ROOT / "data/puzzle_css/bg_raw.png"
        pzp = ROOT / "data/puzzle_css/puzzle_raw.png"
    else:
        bgp = Path(args.bg)
        pzp = Path(args.piece)

    if not bgp.exists() or not pzp.exists():
        print(f"图片不存在：{bgp} / {pzp}")
        print("先跑 tools/diag_puzzle_css.py 生成原始图，或指定 --bg/--piece")
        return 2

    bg = Image.open(bgp)
    pz = Image.open(pzp)
    print(f"背景 {bg.size} {bg.mode}")
    print(f"拼图 {pz.size} {pz.mode}")

    # 转成 PNG bytes（ddddocr 要 bytes）
    b1 = io.BytesIO()
    bg.convert("RGB").save(b1, format="PNG")
    b2 = io.BytesIO()
    pz.save(b2, format="PNG")

    r = try_ddddocr(b1.getvalue(), b2.getvalue())

    print("\n=== ddddocr 结果 ===")
    for k, v in r.items():
        print(f"  {k}: {v}")

    # 如果拿到 x，标到图上
    x = None
    for key in ("slide_match", "slide_match_full", "slide_comparison"):
        v = r.get(key)
        if isinstance(v, dict):
            x = v.get("target_x") or v.get("target") or v.get("x")
            if isinstance(x, (list, tuple)):
                x = x[0]
            if isinstance(x, (int, float)):
                break
    if x is None and isinstance(r.get("slide_match"), (list, tuple)):
        x = r["slide_match"][0]

    if isinstance(x, (int, float)):
        print(f"\n识别到的目标 x = {x:.1f}（截图坐标系）")
        from PIL import ImageDraw
        vis = bg.convert("RGB").copy()
        d = ImageDraw.Draw(vis)
        d.line([int(x), 0, int(x), vis.size[1]], fill=(255, 0, 0), width=2)
        d.rectangle([int(x), 0, int(x) + pz.size[0], vis.size[1]],
                    outline=(255, 0, 0), width=2)
        outp = ROOT / "data" / "captcha_diag" / "ddddocr_result.png"
        vis.save(outp)
        print(f"标注图: {outp}")
    else:
        print("\n没解析出 x，看上面原始返回")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
