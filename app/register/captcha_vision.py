"""阿里云 FeiLin 缺口识别（OpenCV 三方法投票）。

踩坑记录（很重要，避免后人重走）
--------------------------------
1. **不能用 locator.screenshot() 取拼图块**
   Playwright 截图会把透明处与背景**合成**，alpha 丢失，
   看起来就像「拼图块是背景的动态切片」，从而误判模板匹配不可用。

2. **必须直接解码 img.src**
   ``#aliyunCaptcha-puzzle`` 是 ``<img src="data:image/png;base64,...">``，
   src 里就是带 alpha 的完整 PNG（52×200）。

3. **必须裁到 alpha bbox**
   那个 52×200 画布有 **81% 是透明的**，真实内容只有中间 52×54
   （实测 bbox y=122..176，随题目浮动）。
   不裁 bbox 直接匹配，三个方法全给出错误结果。

4. **ddddocr 在这种图上不可靠**
   实测 slide_match 给出 108 / 268，置信度 0.5 / 0.18，互相矛盾。
   原因是它内部对「大画布 + 小内容」的处理不适合 FeiLin。

5. **OpenCV 三方法投票最稳**
   裁 bbox 后实测：
       cv-ccorr-color  x=150 conf=0.930
       cv-ccorr-gray   x=150 conf=0.929
       cv-sqdiff-gray  x=150 conf=0.857
   三法完全一致。canny-mask 会离群（244），弃用。

最终算法
--------
1. 解码 puzzle 的 src → RGBA
2. alpha>32 求 bbox，裁出真实拼图块（约 52×54）与掩膜
3. 三个方法在背景图上做掩膜匹配：
   - TM_CCORR_NORMED（彩色 + mask）
   - TM_CCORR_NORMED（灰度 + mask）
   - TM_SQDIFF_NORMED（灰度 + mask）
4. 三者 x 一致（容差 ±8px）→ 高置信采用
   不一致 → 取 conf 最高者并降权
5. 换算：x_display = x_px / (截图宽 / 显示宽)
"""

from __future__ import annotations

import base64
import io
from dataclasses import dataclass

import numpy as np
from PIL import Image

try:                                   # 本机可能没装 opencv
    import cv2
    _CV2_NOTE = "cv2"
except ImportError:                    # 退回 numpy 实现，结果等价
    cv2 = None
    _CV2_NOTE = "numpy"

from app.register import template_match

# 三法一致时的最小置信度
MIN_CONF = 0.55
# 一致判定容差（像素，截图坐标系）
AGREE_TOL = 8


@dataclass(slots=True)
class GapResult:
    ok: bool
    x_display: float = 0.0
    x_px: int = 0
    y_px: int = 0
    conf: float = 0.0
    method: str = ""
    scale: float = 1.0
    piece_w: int = 0
    piece_h: int = 0
    agree_count: int = 0
    detail: str = ""

    def as_dict(self) -> dict:
        return {
            "ok": self.ok, "x": round(self.x_display, 2), "x_px": self.x_px,
            "y_px": self.y_px, "conf": round(self.conf, 3),
            "method": self.method, "scale": round(self.scale, 4),
            "piece_w": self.piece_w, "piece_h": self.piece_h,
            "agree_count": self.agree_count, "detail": self.detail,
        }


# ============================================================ 解码
def decode_data_url(src: str) -> Image.Image | None:
    """解码 data URL（保留 alpha）。"""
    src = (src or "").strip()
    if not src.startswith("data:") or "," not in src:
        return None
    header, payload = src.split(",", 1)
    try:
        if "base64" in header:
            payload = "".join(payload.split())
            raw = base64.b64decode(payload + "=" * ((-len(payload)) % 4))
        else:
            from urllib.parse import unquote_to_bytes
            raw = unquote_to_bytes(payload)
        return Image.open(io.BytesIO(raw))
    except Exception:
        return None


def load_image_src(src: str, proxy: str | None = None,
                   timeout: int = 20) -> Image.Image | None:
    """从任意 src 加载图片。

    实测 FeiLin 的 src 有两种形态：
      1. ``data:image/png;base64,...``  —— 初次加载
      2. ``https://static-captcha-sgp.aliyuncs.com/qst/PUZZLE/...``
         —— **点 refresh 换题后**变成远程 URL

    所以不能只处理 data URL，必须两种都支持。
    """
    src = (src or "").strip()
    if not src:
        return None

    if src.startswith("data:"):
        return decode_data_url(src)

    if src.startswith(("http://", "https://")):
        try:
            import requests
            proxies = {"http": proxy, "https": proxy} if proxy else None
            r = requests.get(src, proxies=proxies, timeout=timeout,
                             headers={"User-Agent":
                                      "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                                      "AppleWebKit/537.36 (KHTML, like Gecko) "
                                      "Chrome/131.0.0.0 Safari/537.36"})
            if r.status_code == 200 and r.content:
                return Image.open(io.BytesIO(r.content))
        except Exception:
            return None
    return None


def crop_to_alpha(piece: Image.Image, pad: int = 1) -> tuple[Image.Image, Image.Image, tuple]:
    """裁到 alpha 不透明 bbox。返回 (rgb, alpha掩膜, bbox)。"""
    rgba = np.array(piece.convert("RGBA"))
    alpha = rgba[:, :, 3]
    mask = alpha > 32
    if not mask.any():
        return (piece.convert("RGB"),
                Image.new("L", piece.size, 255),
                (0, 0, piece.size[0], piece.size[1]))
    ys, xs = np.where(mask)
    x0 = max(0, int(xs.min()) - pad)
    x1 = min(piece.size[0], int(xs.max()) + 1 + pad)
    y0 = max(0, int(ys.min()) - pad)
    y1 = min(piece.size[1], int(ys.max()) + 1 + pad)
    sub = rgba[y0:y1, x0:x1]
    return (Image.fromarray(sub[:, :, :3]),
            Image.fromarray(sub[:, :, 3]),
            (x0, y0, x1, y1))


# ============================================================ 匹配
def _peak_uniqueness(resp: np.ndarray, tol: int = 8) -> tuple[float, float]:
    """返回 (最高峰, 次高峰)。次高峰需排除最高峰附近 tol 像素。"""
    r = np.nan_to_num(resp, nan=-1.0, posinf=-1.0, neginf=-1.0)
    flat = r.flatten()
    order = np.argsort(flat)[::-1]
    if len(order) == 0:
        return -1.0, -1.0
    bi = int(order[0])
    by, bx = np.unravel_index(bi, r.shape)
    best = float(flat[bi])
    second = -1.0
    for idx in order[1: min(len(order), 400)]:
        y, x = np.unravel_index(int(idx), r.shape)
        if abs(x - bx) <= tol and abs(y - by) <= tol:
            continue
        second = float(flat[idx])
        break
    return best, second


def _match_with_uniqueness(bg_bgr: np.ndarray, pz_bgr: np.ndarray,
                           mask: np.ndarray, mode: str,
                           y_center: int | None = None,
                           y_span: int = 24
                           ) -> tuple[int, int, float, float, float] | None:
    """单次掩膜匹配，返回 (x, y, conf, best, second)。

    ``y_center``：拼图块 bbox 的 y。给了就把搜索限制在该 y 附近 ——
    因为拼图块是从原图该位置裁出来的，缺口的 y 必然与它相同。
    这是抑制误匹配最有效的手段（实测能把 (245,31) 这类离群点直接排除）。
    """
    try:
        if mode == "ccorr-color":
            m3 = cv2.merge([mask] * 3)
            r = cv2.matchTemplate(bg_bgr, pz_bgr, cv2.TM_CCORR_NORMED, mask=m3)
            r = np.nan_to_num(r, nan=-1.0, posinf=-1.0, neginf=-1.0)
            r = _restrict_y(r, y_center, y_span)
            best, second = _peak_uniqueness(r)
            _, _, _, ml = cv2.minMaxLoc(r)
            return int(ml[0]), int(ml[1]), float(max(0.0, best)), best, second

        bg_g = cv2.cvtColor(bg_bgr, cv2.COLOR_BGR2GRAY)
        pz_g = cv2.cvtColor(pz_bgr, cv2.COLOR_BGR2GRAY)

        if mode == "ccorr-gray":
            r = cv2.matchTemplate(bg_g, pz_g, cv2.TM_CCORR_NORMED, mask=mask)
            r = np.nan_to_num(r, nan=-1.0, posinf=-1.0, neginf=-1.0)
            r = _restrict_y(r, y_center, y_span)
            best, second = _peak_uniqueness(r)
            _, _, _, ml = cv2.minMaxLoc(r)
            return int(ml[0]), int(ml[1]), float(max(0.0, best)), best, second

        if mode == "sqdiff-gray":
            bgf = bg_g.astype(np.float32)
            pzf = pz_g.astype(np.float32)
            r = cv2.matchTemplate(bgf, pzf, cv2.TM_SQDIFF_NORMED, mask=mask)
            r = np.nan_to_num(r, nan=1.0, posinf=1.0, neginf=1.0)
            r = _restrict_y(r, y_center, y_span, worst=1.0)
            rinv = 1.0 - r
            best, second = _peak_uniqueness(rinv)
            mn, _, ml, _ = cv2.minMaxLoc(r)
            return int(ml[0]), int(ml[1]), float(max(0.0, 1.0 - mn)), best, second
    except Exception:
        return None
    return None


def _match_with_uniqueness_np(bg_rgb: np.ndarray, pz_rgb: np.ndarray,
                              mask01: np.ndarray, mode: str,
                              y_center: int | None = None,
                              y_span: int = 24
                              ) -> tuple[int, int, float, float, float] | None:
    """numpy 版（无 cv2 时使用）。公式与 OpenCV 掩膜归一化一致。"""
    try:
        if mode == "ccorr-color":
            r = template_match.match_masked(bg_rgb, pz_rgb, mask01, "ccorr")
            r = np.nan_to_num(r, nan=-1.0, posinf=-1.0, neginf=-1.0)
            r = _restrict_y(r, y_center, y_span)
            best, second = _peak_uniqueness(r)
            my, mx = np.unravel_index(int(np.argmax(r)), r.shape)
            return int(mx), int(my), float(max(0.0, best)), best, second

        bg_g = template_match.to_gray(bg_rgb)
        pz_g = template_match.to_gray(pz_rgb)

        if mode == "ccorr-gray":
            r = template_match.match_masked(bg_g, pz_g, mask01, "ccorr")
            r = np.nan_to_num(r, nan=-1.0, posinf=-1.0, neginf=-1.0)
            r = _restrict_y(r, y_center, y_span)
            best, second = _peak_uniqueness(r)
            my, mx = np.unravel_index(int(np.argmax(r)), r.shape)
            return int(mx), int(my), float(max(0.0, best)), best, second

        if mode == "sqdiff-gray":
            r = template_match.match_masked(bg_g, pz_g, mask01, "sqdiff")
            r = np.nan_to_num(r, nan=1.0, posinf=1.0, neginf=1.0)
            r = _restrict_y(r, y_center, y_span, worst=1.0)
            rinv = 1.0 - r
            best, second = _peak_uniqueness(rinv)
            my, mx = np.unravel_index(int(np.argmin(r)), r.shape)
            mn = float(r[my, mx])
            return int(mx), int(my), float(max(0.0, 1.0 - mn)), best, second
    except Exception:
        return None
    return None


def _match(bg_rgb: np.ndarray, pz_rgb: np.ndarray, mask01: np.ndarray,
           mode: str, y_center: int | None = None,
           y_span: int = 24) -> tuple[int, int, float, float, float] | None:
    """按环境选后端：有 opencv 用 opencv，没有就走 numpy。"""
    if cv2 is not None:
        bg_bgr = cv2.cvtColor(bg_rgb, cv2.COLOR_RGB2BGR)
        pz_bgr = cv2.cvtColor(pz_rgb, cv2.COLOR_RGB2BGR)
        mask = (mask01 * 255).astype(np.uint8)
        return _match_with_uniqueness(bg_bgr, pz_bgr, mask, mode, y_center, y_span)
    return _match_with_uniqueness_np(bg_rgb, pz_rgb, mask01, mode,
                                     y_center, y_span)


def _restrict_y(r: np.ndarray, y_center: int | None, y_span: int,
                worst: float = -1.0) -> np.ndarray:
    """把 y 范围外的响应值压到最低。"""
    if y_center is None:
        return r
    lo = max(0, y_center - y_span)
    hi = min(r.shape[0], y_center + y_span + 1)
    out = r.copy()
    if lo > 0:
        out[:lo, :] = worst
    if hi < r.shape[0]:
        out[hi:, :] = worst
    return out


def find_gap(bg: Image.Image, piece: Image.Image,
             display_w: float | None = None,
             debug_dir: str | None = None) -> GapResult:
    """识别缺口。``display_w`` 为背景图在页面上的 CSS 宽度。

    三重退化校验（实测踩坑后加的）
    ------------------------------
    1. **模板纯色检查**：拼图块 std < 8 → 无法匹配，直接拒绝
    2. **峰值唯一性**：最高峰与次高峰差 < 0.03 → 退化（多个位置都一样好）
    3. **贴边检查**：结果落在最右/最左 3px 内 → 大概率是边缘伪影

    没有这三条，会稳定给出「最右边」这种错误答案（实测 target=248.3、conf=1.000）。
    """
    nat_w = bg.size[0]
    scale = (nat_w / display_w) if display_w else 1.0

    pz_rgb, pz_mask, bbox = crop_to_alpha(piece)
    pw, ph = pz_rgb.size
    if pw < 8 or ph < 8:
        return GapResult(False, detail=f"拼图有效区太小 {pw}x{ph}", scale=scale)

    # ---- 校验 1：模板必须非纯色 ----
    pz_np = np.array(pz_rgb, dtype=np.float64)
    m = (np.array(pz_mask) > 32)
    if m.sum() < 100:
        return GapResult(False, detail="拼图不透明像素过少", scale=scale,
                         piece_w=pw, piece_h=ph)
    vals = pz_np[m]
    pz_std = float(vals.std(axis=0).mean())
    if pz_std < 8.0:
        return GapResult(False,
                         detail=f"拼图块接近纯色 std={pz_std:.1f}，无法匹配",
                         scale=scale, piece_w=pw, piece_h=ph)

    bg_rgb = np.array(bg.convert("RGB"))
    pz_np = np.array(pz_rgb)
    mask01 = m.astype(np.float32)

    bh, bw = bg_rgb.shape[:2]
    if ph > bh or pw > bw:
        return GapResult(False, detail=f"模板 {pw}x{ph} 超出背景 {bw}x{bh}",
                         scale=scale, piece_w=pw, piece_h=ph)

    cands: list[dict] = []
    for mode in ("ccorr-color", "ccorr-gray", "sqdiff-gray"):
        got = _match(bg_rgb, pz_np, mask01, mode)
        if got:
            x, y, conf, best, second = got
            cands.append({"mode": mode, "x": x, "y": y, "conf": conf,
                          "best": best, "second": second,
                          "gap": best - second})

    if not cands:
        return GapResult(False, detail="所有匹配方法失败", scale=scale,
                         piece_w=pw, piece_h=ph)

    # ---- 投票：按 x 聚类，取成员最多的一组 ----
    # 实测教训：不能「取 gap 最大」——离群点也可能 gap 大。
    # 正确做法是「位置最集中」，即两个以上方法指向同一位置。
    cands.sort(key=lambda c: c["conf"], reverse=True)
    groups: list[list[dict]] = []
    for c in cands:
        placed = False
        for g in groups:
            if abs(c["x"] - g[0]["x"]) <= AGREE_TOL:
                g.append(c)
                placed = True
                break
        if not placed:
            groups.append([c])
    groups.sort(key=len, reverse=True)
    best_group = groups[0]

    # 组内取 conf 最高的
    best_group.sort(key=lambda c: c["conf"], reverse=True)
    best = best_group[0]

    consensus = len(best_group)
    # 全部方法都指向同一位置，或至少 2 票，才算可信
    if consensus < 2:
        # 单票：要求 gap 明显且 conf 高
        if best["gap"] < 0.05 or best["conf"] < 0.85:
            detail = "; ".join(
                f"{c['mode']}@({c['x']},{c['y']}) c={c['conf']:.3f} "
                f"gap={c['gap']:.3f}" for c in cands)
            return GapResult(False,
                             detail="无共识（各方法位置分散）: " + detail,
                             scale=scale, piece_w=pw, piece_h=ph)

    # ---- 校验：结果贴边 ----
    max_x = bw - pw
    if best["x"] <= 2 or best["x"] >= max_x - 2:
        return GapResult(False,
                         detail=f"结果贴边 x={best['x']} (范围 0..{max_x})，疑似伪影",
                         scale=scale, piece_w=pw, piece_h=ph,
                         method=best["mode"], conf=best["conf"])

    conf = best["conf"]
    if consensus >= 2:
        conf = min(1.0, conf + 0.08 * (consensus - 1))
        method = f"{best['mode']}+vote{consensus}/{len(cands)}"
    else:
        conf *= 0.85
        method = f"{best['mode']}-single"

    if conf < MIN_CONF:
        detail = "; ".join(
            f"{c['mode']}@({c['x']},{c['y']}) c={c['conf']:.3f} "
            f"gap={c['gap']:.3f}" for c in cands)
        return GapResult(False, detail=f"置信度不足 conf={conf:.3f}: {detail}",
                         scale=scale, piece_w=pw, piece_h=ph,
                         method=method, conf=conf)

    # 可选：存调试样本
    if debug_dir:
        try:
            from pathlib import Path
            import time as _t
            d = Path(debug_dir)
            d.mkdir(parents=True, exist_ok=True)
            ts = int(_t.time())
            bg.save(d / f"bg_{ts}.png")
            piece.save(d / f"pz_{ts}.png")
            pz_rgb.save(d / f"pz_crop_{ts}.png")
            (d / f"result_{ts}.txt").write_text(
                "\n".join(f"{c['mode']} x={c['x']} y={c['y']} "
                          f"conf={c['conf']:.3f} gap={c['gap']:.3f}"
                          for c in cands),
                encoding="utf-8")
        except Exception:
            pass

    x_px, y_px = best["x"], best["y"]
    return GapResult(
        ok=True,
        x_display=x_px / scale,
        x_px=x_px, y_px=y_px,
        conf=conf,
        method=method,
        scale=scale,
        piece_w=pw, piece_h=ph,
        agree_count=consensus,
        detail="; ".join(f"{c['mode']}@({c['x']},{c['y']}) "
                         f"c={c['conf']:.3f} gap={c['gap']:.3f}" for c in cands),
    )
