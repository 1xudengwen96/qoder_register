"""用背景图自身特征找缺口（不依赖拼图块）。

关键结论（tools/diag_pixels.py 实测）
------------------------------------
拼图块的内容 **等于背景图最左边（left=0）那一条**（相关度 1.000），
不是缺口处的图像。所以模板匹配必然定位到 x=0，完全不可用。

必须改用背景图自身的特征：
FeiLin 的缺口是在原图上**挖掉一块并用半透明纯色覆盖**，所以缺口区域：
  1. 局部方差显著低于周围（被纯色覆盖）
  2. 有清晰的矩形边界（边缘梯度成对出现）
  3. 宽度固定等于拼图块宽度

算法
----
1. 算每列的**纵向方差**，得到一条曲线
2. 缺口区域方差会形成一个"低洼段"
3. 用拼图块宽度 52 做滑窗，找**窗口内方差最低**的位置
4. 用边缘梯度做交叉验证

用法：
    python tools/diag_gap_variance.py
"""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

D = ROOT / "data" / "captcha_diag"


def main() -> int:
    bgp = D / "bg.png"
    pzp = D / "piece.png"
    if not bgp.exists():
        print("缺图，先跑 tools\ndiag_captcha_position.py")
        return 2

    bg = Image.open(bgp)
    pz = Image.open(pzp)
    g = np.asarray(bg.convert("L"), dtype=np.float64)
    h, w = g.shape
    pw = pz.size[0]
    print(f"背景 {w}x{h}  拼图宽 {pw}\n")

    # ---------- 1. 每列纵向方差 ----------
    col_var = g.var(axis=0)
    # 平滑
    k = 3
    pad = np.pad(col_var, k, mode="edge")
    col_var_s = np.convolve(pad, np.ones(2 * k + 1) / (2 * k + 1), mode="valid")

    print("=== 每列纵向方差 ===")
    print(f"  中位数={np.median(col_var_s):.1f}  "
          f"最小={col_var_s.min():.1f} 最大={col_var_s.max():.1f}")

    # 滑窗找方差最低的 pw 宽窗口
    print(f"\n=== 滑窗（宽 {pw}）找方差最低段 ===")
    win_scores = []
    for x0 in range(w - pw + 1):
        win_scores.append(float(col_var_s[x0:x0 + pw].mean()))
    win_scores = np.array(win_scores)
    order = np.argsort(win_scores)
    print("  方差最低的 6 个窗口位置:")
    for i in order[:6]:
        print(f"    x={int(i):<4} 平均方差={win_scores[i]:.2f}")

    # ---------- 2. 边缘梯度 ----------
    print("\n=== 列边缘梯度 ===")
    gx = np.abs(np.diff(g, axis=1)).mean(axis=0)   # 每列与左右列的差异
    gx_s = np.convolve(np.pad(gx, 2, mode="edge"), np.ones(5) / 5, mode="valid")
    top = np.argsort(gx_s)[::-1][:10]
    print("  梯度最强的列:")
    print(f"    {[(int(i), round(gx_s[i],1)) for i in sorted(top)]}")

    # 找成对的强边缘（相距约 pw）
    pairs = []
    for i in range(len(gx_s)):
        for j in range(i + 10, min(len(gx_s), i + pw + 20)):
            dist = j - i
            if abs(dist - pw) <= pw * 0.45:
                score = gx_s[i] + gx_s[j]
                pairs.append((i, j, dist, float(score)))
    pairs.sort(key=lambda p: p[3], reverse=True)
    print(f"  成对边缘（间距≈{pw}）:")
    for a, b, d, s in pairs[:6]:
        print(f"    x={a}..{b} 间距={d} 强度和={s:.1f}")

    # ---------- 3. 局部方差图（2D） ----------
    print("\n=== 2D 局部方差最低区域 ===")
    win = 10
    best = []
    for y0 in range(0, h - win, 2):
        for x0 in range(0, w - win - pw + 1, 2):
            patch = g[y0:y0 + win, x0:x0 + pw]
            best.append((float(patch.var()), x0, y0))
    best.sort()
    print("  方差最低的 6 个 (x, y) 位置:")
    for v, x0, y0 in best[:6]:
        print(f"    x={x0:<4} y={y0:<4} var={v:.2f}")

    # ---------- 4. 综合候选 ----------
    print("\n=== 综合候选 ===")
    cands: list[tuple[str, float, float]] = []
    # 滑窗方差最低
    for i in order[:3]:
        cands.append(("var-win", float(i), float(win_scores[i])))
    # 成对边缘
    for a, b, d, s in pairs[:3]:
        cands.append(("edge-pair", float(a), s))
    # 2D 方差最低
    for v, x0, y0 in best[:3]:
        cands.append(("var-2d", float(x0), v))

    seen = set()
    uniq = []
    for name, x, score in cands:
        key = round(x / 10)
        if key in seen:
            continue
        seen.add(key)
        uniq.append((name, x, score))
    for name, x, score in uniq:
        print(f"  {name:<10} x={x:<6.0f} score={score:.2f}")

    # ---------- 可视化 ----------
    vis = bg.convert("RGB").copy()
    d = ImageDraw.Draw(vis)
    colors = [(255, 0, 0), (0, 180, 255), (0, 200, 0), (255, 160, 0),
              (200, 0, 255), (0, 220, 220)]
    for idx, (name, x, _) in enumerate(uniq[:6]):
        c = colors[idx % len(colors)]
        d.rectangle([x, 0, x + pw, h - 1], outline=c, width=2)
        d.text((x + 2, 2 + idx * 12), f"{idx}:{name}", fill=c)

    outp = D / "gap_candidates.png"
    vis.save(outp)
    print(f"\n候选可视化: {outp}")

    # 也导方差曲线
    csv = D / "col_variance.csv"
    with csv.open("w", encoding="utf-8") as fh:
        fh.write("x,col_var,window_score\n")
        for x in range(w):
            ws = ""
            if x <= w - pw:
                ws = f"{win_scores[x]:.3f}"
            fh.write(f"{x},{col_var_s[x]:.3f},{ws}\n")
    print(f"列方差曲线: {csv}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
