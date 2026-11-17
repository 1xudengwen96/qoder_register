"""滑块图片的取图/解码/裁剪工具（2Captcha 的输入预处理）。

2026-11-17 起：自研缺口识别（OpenCV 三方法投票 / numpy 模板匹配）已
全部移除，识别只走 2Captcha。本模块只保留三件事：

1. 解码 ``img.src``（data URL 或远程 URL，保留 alpha）
2. 按 URL 加载图片（refresh 换题后 src 会变成 ``*.aliyuncs.com``）
3. 裁到 alpha bbox —— 52×200 画布 81% 透明，不裁会把大片透明
   发给 2Captcha，影响识别

踩坑记录（保留给取图逻辑）
--------------------------
1. **不能用 locator.screenshot() 取拼图块**
   Playwright 截图会把透明处与背景**合成**，alpha 丢失。

2. **必须直接解码 img.src**
   ``#aliyunCaptcha-puzzle`` 是 ``<img src="data:image/png;base64,...">``，
   src 里就是带 alpha 的完整 PNG（52×200）。

3. **必须裁到 alpha bbox**
   那个 52×200 画布有 **81% 是透明的**，真实内容只有中间 52×54。
"""

from __future__ import annotations

import base64
import io

import numpy as np
from PIL import Image


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
    """裁到 alpha 不透明 bbox。返回 (rgb, alpha掩膜, bbox)。

    bbox[1]（y0）同时是「拼图块在原画布中的 y」—— 2Captcha 返回的 y
    应与之接近，偏差过大说明识别不可信（调用方据此过滤）。
    """
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
