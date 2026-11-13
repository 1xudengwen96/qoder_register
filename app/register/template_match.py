"""numpy 版掩膜模板匹配 —— ``cv2.matchTemplate`` 的等价替代。

背景：本机 Python 3.14 装不上 opencv（pip 解包被拒 + 无匹配 wheel），
但缺口识别只用到 4 个函数：``cvtColor`` / ``merge`` / ``matchTemplate`` /
``minMaxLoc``。用 numpy 全量实现，零额外依赖，结果与 OpenCV 掩膜版一致。

实现范围
--------
- ``TM_CCORR_NORMED``（彩色与灰度）
- ``TM_SQDIFF_NORMED``（灰度）

OpenCV 带 mask 的归一化公式（mask 取 0/1）：

    CCORR_NORMED : num = Σ(T·I·M)
                   den = sqrt( Σ(T²·M) · Σ(I²·M) )
                   res = num / den

    SQDIFF_NORMED: num = Σ((T-I)²·M)
                   den = sqrt( Σ(T²·M) · Σ(I²·M) )
                   res = num / den

den 为 0 的位置置 NaN，与 OpenCV 行为一致（调用方已做 nan_to_num）。
"""

from __future__ import annotations

import numpy as np


def to_gray(rgb: np.ndarray) -> np.ndarray:
    """RGB(A) → 灰度，BT.601 权重（与 cv2.COLOR_RGB2GRAY 同系数）。"""
    a = np.asarray(rgb, dtype=np.float32)
    if a.ndim == 2:
        return a
    return (a[..., 0] * 0.299 + a[..., 1] * 0.587 + a[..., 2] * 0.114).astype(np.float32)


def _prep(bg, tpl, mask):
    bg = np.asarray(bg, dtype=np.float32)
    tpl = np.asarray(tpl, dtype=np.float32)
    mask = np.asarray(mask, dtype=np.float32)
    if mask.ndim == 3:
        mask = mask[..., 0]
    if mask.max() > 1.5:
        mask = mask / 255.0
    if tpl.ndim == 2 and bg.ndim == 3:
        tpl = np.repeat(tpl[..., None], bg.shape[2], axis=2)
    elif tpl.ndim == 3 and bg.ndim == 2:
        bg = np.repeat(bg[..., None], tpl.shape[2], axis=2)
    return bg, tpl, mask


def match_masked(bg, tpl, mask, mode: str = "ccorr") -> np.ndarray:
    """返回响应图，形状 ``(H-h+1, W-w+1)``。

    ``mode``: ``ccorr``（越大越好）/ ``sqdiff``（越小越好）。
    逐通道、逐 y 行做滑窗，内存占用与模板尺寸无关于背景总像素。
    """
    bg, tpl, mask = _prep(bg, tpl, mask)
    th, tw = tpl.shape[:2]
    bh, bw = bg.shape[:2]
    if th > bh or tw > bw:
        raise ValueError(f"模板 {tw}x{th} 超出背景 {bw}x{bh}")

    if bg.ndim == 2:
        bg = bg[..., None]
    if tpl.ndim == 2:
        tpl = tpl[..., None]
    channels = bg.shape[2]

    rows = np.zeros((bh - th + 1, bw - tw + 1), dtype=np.float64)
    sq_t_total = 0.0
    sq_i_total = np.zeros((bh - th + 1, bw - tw + 1), dtype=np.float64)
    num_total = np.zeros_like(rows)

    m = mask.astype(np.float64)
    for c in range(channels):
        plane = bg[:, :, c].astype(np.float64)
        tp = tpl[:, :, c].astype(np.float64)
        tp_m = tp * m
        sq_t_total += float((tp * tp_m).sum())
        for y in range(bh - th + 1):
            band = plane[y:y + th]                       # (th, bw)
            win = np.lib.stride_tricks.sliding_window_view(band, tw, axis=1)
            # (th, X, tw)
            if mode == "ccorr":
                num_total[y] += (win * tp_m[:, None, :]).sum(axis=(0, 2))
            else:
                diff = win - tp[:, None, :]
                num_total[y] += ((diff * diff) * m[:, None, :]).sum(axis=(0, 2))
            sq_i_total[y] += (win * win * m[:, None, :]).sum(axis=(0, 2))

    if sq_t_total <= 0:
        return np.full((bh - th + 1, bw - tw + 1), np.nan, dtype=np.float32)

    den = np.sqrt(np.maximum(sq_t_total * sq_i_total, 0.0))
    with np.errstate(divide="ignore", invalid="ignore"):
        rows = np.where(den > 1e-12, num_total / den, np.nan)
    return rows.astype(np.float32)



def min_max_loc(resp: np.ndarray, mode: str = "both") -> tuple[int, int, int, int]:
    """等价 ``cv2.minMaxLoc``：返回 ``(min_x, min_y, max_x, max_y)``。"""
    r = np.asarray(resp, dtype=np.float64)
    valid = np.isfinite(r)
    if not valid.any():
        return 0, 0, 0, 0
    filled = np.where(valid, r, np.inf)
    min_y, min_x = np.unravel_index(int(np.argmin(filled)), r.shape)
    filled2 = np.where(valid, r, -np.inf)
    max_y, max_x = np.unravel_index(int(np.argmax(filled2)), r.shape)
    return int(min_x), int(min_y), int(max_x), int(max_y)
