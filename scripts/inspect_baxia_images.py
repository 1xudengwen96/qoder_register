"""检查合成出来的题干图/九宫格图里到底有没有内容（分区域亮度统计）。"""

from __future__ import annotations

import sys
from pathlib import Path

from PIL import Image

ROOT = Path(__file__).resolve().parent.parent


def stats(path: Path) -> None:
    if not path.exists():
        print(f"{path.name}: 不存在")
        return
    im = Image.open(path).convert("L")
    w, h = im.size
    print(f"\n{path.name}: {w}x{h}")
    bands = 4
    for i in range(bands):
        top = int(h * i / bands)
        bot = int(h * (i + 1) / bands)
        crop = im.crop((0, top, w, bot))
        px = list(crop.getdata())
        dark = sum(1 for v in px if v < 128)
        print(f"  第 {i+1}/{bands} 段: avg={sum(px)//len(px)} "
              f"dark={dark}/{len(px)}")


def main() -> int:
    for p in sorted((ROOT / "data" / "artifacts").glob("baxia_grid_*_q.png")):
        stats(p)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
