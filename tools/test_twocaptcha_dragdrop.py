"""2Captcha DragAndDrop 实测。

用法：
    python tools/test_twocaptcha_dragdrop.py
    python tools/test_twocaptcha_dragdrop.py --bg X.png --piece Y.png
"""

from __future__ import annotations

import os

import argparse
import json
import sys
from pathlib import Path

from PIL import Image

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.net.twocaptcha_dragdrop import (  # noqa: E402
    TwoCaptchaDragDrop,
    TwoCaptchaError,
)

API_KEY = os.environ.get("TWOCAPTCHA_KEY", "")


def crop_alpha(img: Image.Image, pad: int = 2) -> Image.Image:
    import numpy as np
    rgba = np.array(img.convert("RGBA"))
    a = rgba[:, :, 3]
    m = a > 32
    if not m.any():
        return img
    ys, xs = np.where(m)
    x0 = max(0, int(xs.min()) - pad)
    x1 = min(img.size[0], int(xs.max()) + 1 + pad)
    y0 = max(0, int(ys.min()) - pad)
    y1 = min(img.size[1], int(ys.max()) + 1 + pad)
    return Image.fromarray(rgba[y0:y1, x0:x1])


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--key", default=API_KEY)
    ap.add_argument("--bg", default=str(ROOT / "data/puzzle_css/bg_raw.png"))
    ap.add_argument("--piece", default=str(ROOT / "data/puzzle_css/puzzle_raw.png"))
    ap.add_argument("--proxy", default="http://127.0.0.1:7897")
    args = ap.parse_args()

    bgp, pzp = Path(args.bg), Path(args.piece)
    if not bgp.exists() or not pzp.exists():
        print(f"缺图: {bgp} / {pzp}")
        return 2

    bg = Image.open(bgp)
    pz = Image.open(pzp)
    pz_c = crop_alpha(pz)
    print(f"背景 {bg.size}   拼图原图 {pz.size}   裁bbox后 {pz_c.size}\n")

    c = TwoCaptchaDragDrop(args.key, proxy=args.proxy)

    print("=== 余额 ===")
    try:
        print(f"  ${c.balance():.4f}")
    except TwoCaptchaError as exc:
        print(f"  失败: {exc}")

    for label, piece in (("裁bbox后提交", pz_c), ("原图提交", pz)):
        print(f"\n=== DragAndDrop · {label} ===")
        try:
            sol = c.solve(bg, piece)
            print(f"  耗时 {sol.elapsed:.1f}s  花费 ${sol.cost:.4f}")
            print(f"  坐标 x={sol.x}  y={sol.y}")
            print(f"  原始: {json.dumps(sol.raw.get('solution'), ensure_ascii=False)[:300]}")
        except TwoCaptchaError as exc:
            print(f"  失败: code={exc.code} {exc}")
            print(f"  原始: {json.dumps(exc.raw, ensure_ascii=False)[:400]}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
