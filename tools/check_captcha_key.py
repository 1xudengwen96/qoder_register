"""检查打码账号与识别通道是否可用。

    python tools/check_captcha_key.py                 # 余额 + 通道装配
    python tools/check_captcha_key.py --solve         # 用存下的图真跑一次识别
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import secrets  # noqa: E402
from app.net.twocaptcha_dragdrop import TwoCaptchaDragDrop, TwoCaptchaError  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--key", default="")
    ap.add_argument("--solve", action="store_true", help="真跑一次 DragAndDrop")
    ap.add_argument("--bg", default=str(ROOT / "data/puzzle_css/bg_raw.png"))
    ap.add_argument("--piece", default=str(ROOT / "data/puzzle_css/puzzle_raw.png"))
    args = ap.parse_args()

    key = args.key or secrets.env_or("captcha.twocaptcha_key", "TWOCAPTCHA_KEY")
    if not key:
        print("没有 key：设 TWOCAPTCHA_KEY 或写进 config/secrets.yaml")
        return 2
    proxy = secrets.get("proxy.local", "http://127.0.0.1:7897")
    print(f"key={key[:6]}...{key[-4:]}  proxy={proxy}")

    client = TwoCaptchaDragDrop(key, proxy=proxy)
    try:
        bal = client.balance()
        print(f"PASS 余额 ${bal:.4f}")
    except TwoCaptchaError as exc:
        print(f"FAIL 余额查询: code={exc.code} {exc}")
        return 1

    if not args.solve:
        print("（没加 --solve，跳过真识别）")
        return 0

    bg_p, pz_p = Path(args.bg), Path(args.piece)
    if not bg_p.exists() or not pz_p.exists():
        print(f"缺图: {bg_p} / {pz_p}")
        return 2

    from PIL import Image

    sys.path.insert(0, str(ROOT / "tools"))
    from test_twocaptcha_dragdrop import crop_alpha

    bg = Image.open(bg_p)
    pz = crop_alpha(Image.open(pz_p))
    print(f"背景 {bg.size} 拼图 {pz.size}")
    try:
        sol = client.solve(bg, pz)
    except TwoCaptchaError as exc:
        print(f"FAIL 识别: code={exc.code} {exc}")
        return 1
    print(f"PASS 识别 x={sol.x:.1f} y={sol.y:.1f} 耗时 {sol.elapsed:.1f}s "
          f"cost ${sol.cost:.4f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
