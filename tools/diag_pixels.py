"""深入诊断拼图块与背景图的像素特征。

目标：搞清楚拼图块到底是什么，为什么 NCC 会退化。

用法：
    python tools/diag_pixels.py
"""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
from PIL import Image

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

D = ROOT / "data" / "captcha_diag"


def stat(name: str, arr: np.ndarray) -> None:
    print(f"\n--- {name} ---")
    print(f"  shape={arr.shape} dtype={arr.dtype}")
    print(f"  min={arr.min():.1f} max={arr.max():.1f} mean={arr.mean():.2f} "
          f"std={arr.std():.2f}")
    uniq = np.unique(arr)
    print(f"  唯一值个数={len(uniq)}")
    if len(uniq) <= 12:
        print(f"  唯一值={uniq.tolist()}")
    else:
        print(f"  唯一值前10={uniq[:10].tolist()}")


def main() -> int:
    bgp = D / "bg.png"
    pzp = D / "piece.png"
    if not pzp.exists():
        print("缺图片，先跑 tools/diag_captcha_position.py")
        return 2

    bg = Image.open(bgp)
    pz = Image.open(pzp)

    print(f"背景 {bg.size} {bg.mode}   拼图 {pz.size} {pz.mode}")

    pz_a = np.asarray(pz)
    bg_a = np.asarray(bg)

    print("\n" + "=" * 60)
    print("拼图块分析")
    print("=" * 60)

    if pz_a.ndim == 3:
        for i, cname in enumerate(["R", "G", "B", "A"][: pz_a.shape[2]]):
            stat(f"拼图-{cname}", pz_a[:, :, i].astype(np.float64))

    # 灰度
    pz_g = np.asarray(pz.convert("L"), dtype=np.float64)
    stat("拼图-灰度", pz_g)

    # 逐列统计
    print("\n  逐列均值（前 20 列）:")
    col_mean = pz_g.mean(axis=0)
    print(f"    {[round(x, 1) for x in col_mean[:20]]}")
    print(f"  逐列 std（前 20 列）:")
    col_std = pz_g.std(axis=0)
    print(f"    {[round(x, 1) for x in col_std[:20]]}")

    # 横向相邻列差异（看是否有明显边界）
    diff = np.abs(np.diff(pz_g, axis=1))
    print(f"\n  横向相邻列平均差异: {diff.mean():.2f}")
    if diff.size:
        per_col = diff.mean(axis=0)
        top = np.argsort(per_col)[::-1][:6]
        print(f"  变化最大的列位置: {[(int(i), round(per_col[i],1)) for i in top]}")

    # 是否是纯色
    if pz_g.std() < 5:
        print("\n  ⚠ 拼图块几乎是纯色（std<5）→ 模板匹配必然退化！")
    else:
        print(f"\n  ✓ 拼图块有纹理（std={pz_g.std():.1f}）")

    print("\n" + "=" * 60)
    print("背景图分析")
    print("=" * 60)
    bg_g = np.asarray(bg.convert("L"), dtype=np.float64)
    stat("背景-灰度", bg_g)

    # 找背景中的"异常区域"：用局部方差图
    print("\n  局部方差图（找缺口）:")
    h, w = bg_g.shape
    win = 8
    # 逐列逐窗口的方差
    var_map = np.zeros((h - win + 1, w - win + 1))
    for y in range(h - win + 1):
        for x in range(w - win + 1):
            var_map[y, x] = bg_g[y:y + win, x:x + win].var()

    # 方差最低的区域（缺口常为纯色覆盖）
    flat = var_map.flatten()
    low_idx = np.argsort(flat)[:5]
    print("    方差最低的 5 个位置:")
    for idx in low_idx:
        y, x = np.unravel_index(int(idx), var_map.shape)
        print(f"      ({x},{y}) var={flat[idx]:.2f}")

    # 用拼图块的列均值曲线在背景上扫描
    print("\n  用拼图列均值在背景上扫描（相关）:")
    pw = pz_g.shape[1]
    if pw < w:
        pc = pz_g.mean(axis=0)
        pc_z = (pc - pc.mean()) / (pc.std() + 1e-9)
        scores = []
        for x0 in range(w - pw + 1):
            seg = bg_g[:, x0:x0 + pw].mean(axis=0)
            s_z = (seg - seg.mean()) / (seg.std() + 1e-9)
            scores.append(float((pc_z * s_z).mean()))
        scores = np.array(scores)
        top = np.argsort(scores)[::-1][:6]
        print(f"    最高相关位置: {[(int(i), round(scores[i],3)) for i in top]}")

    # 保存拼图块放大版，方便人眼确认
    big = pz.resize((pz.size[0] * 6, pz.size[1] * 6), Image.NEAREST)
    big.save(D / "piece_6x.png")
    print(f"\n拼图块 6 倍放大: {D / 'piece_6x.png'}")

    # 保存拼图块与背景左边缘的对比（拼图初始在 left=0，理论上应与背景左沿一致）
    left_strip = bg.crop((0, 0, pw, bg.size[1]))
    cmp_img = Image.new("RGB", (pw * 2 + 10, bg.size[1]), (255, 255, 255))
    cmp_img.paste(left_strip, (0, 0))
    cmp_img.paste(pz.convert("RGB"), (pw + 10, 0))
    cmp_img = cmp_img.resize(((pw * 2 + 10) * 3, bg.size[1] * 3), Image.NEAREST)
    cmp_img.save(D / "compare_left_vs_piece.png")
    print(f"对比图（左=背景最左条带，右=拼图块）: {D / 'compare_left_vs_piece.png'}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
