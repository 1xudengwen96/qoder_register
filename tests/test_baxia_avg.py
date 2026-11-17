"""baxia_cdp.screenshot_instructions 多帧平均的离线单测（mock CDPPage）。

背景：题干 canvas 是逐帧随机噪点动画，单帧截图打码员读不出描述
（2026-11-17 实测答错格）。修复：连拍 10 帧真实截图 → numpy 逐像素
平均（噪点零均值抵消，恒定文字/图案浮现）→ 自适应压 JPEG。
"""
from __future__ import annotations

import base64
import io
import unittest
from unittest import mock

from PIL import Image

from app.register.baxia_cdp import BaxiaClickCaptcha

RECT = {"x": 0.0, "y": 0.0, "w": 100.0, "h": 200.0}


def _png_bytes(img: Image.Image) -> str:
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return base64.b64encode(buf.getvalue()).decode("ascii")


def _frame(bg: int) -> Image.Image:
    """一帧：背景灰度交替（模拟随机噪点）+ 中央恒定红色块（模拟文字）。"""
    img = Image.new("RGB", (60, 40), (bg, bg, bg))
    px = img.load()
    for x in range(20, 40):
        for y in range(15, 25):
            px[x, y] = (200, 10, 10)
    return img


class FakeCDP:
    def __init__(self, frames_b64: list[str]):
        self.frames = frames_b64
        self.calls = 0

    def call(self, method: str, params: dict) -> dict:
        assert method == "Page.captureScreenshot", method
        assert params["clip"]["width"] == RECT["w"]
        assert params["clip"]["scale"] >= 1.0
        self.calls += 1
        return {"data": self.frames[self.calls % len(self.frames)]}


class FakePage:
    """按 baxia_cdp 用到的接口最小实现。"""

    def __init__(self, cdp: FakeCDP, rect: dict | None = RECT):
        self.cdp = cdp
        self._rect = rect

    def eval(self, js: str, **kw) -> dict:
        return self._rect if self._rect is not None else {}


class TestScreenshotInstructions(unittest.TestCase):

    def test_average_10_frames_denoises(self):
        """10 帧交替黑白噪点背景平均后→灰背景，红色块恒存。"""
        bgs = [60, 200] * 5            # 噪点：均值≈130
        frames_b64 = [_png_bytes(_frame(bg)) for bg in bgs]
        cdp = FakeCDP(frames_b64)
        bx = BaxiaClickCaptcha(FakePage(cdp))

        out = bx.screenshot_instructions(scale=2.0)
        self.assertIsNotNone(out)
        self.assertEqual(cdp.calls, 10)          # 确实连拍了 10 帧
        img = Image.open(io.BytesIO(out))
        self.assertEqual(img.format, "JPEG")
        # 噪点抵消：背景应接近灰（±60 容差，JPEG 有损）
        px = img.convert("RGB").load()
        w, h = img.size
        self.assertLess(abs(px[5, 5][0] - 130), 60)
        # 恒定图案保留：中央仍是红色主导
        cx, cy = w // 2, h // 2
        r, g, b = px[cx, cy]
        self.assertGreater(r, g + 30)
        self.assertGreater(r, b + 30)

    def test_screenshot_failure_returns_none(self):
        """任一帧截图异常 → 返回 None（调用方走 questionUrl 兜底）。"""
        cdp = mock.Mock()
        cdp.call = mock.Mock(side_effect=RuntimeError("cdp closed"))
        bx = BaxiaClickCaptcha(FakePage(cdp))

        self.assertIsNone(bx.screenshot_instructions())
        self.assertEqual(cdp.call.call_count, 1)  # 失败即刻退出

    def test_numpy_missing_falls_back_to_single_frame(self):
        """numpy 不可用时退回单帧（不再平均）。"""
        frames_b64 = [_png_bytes(_frame(90))] * 10
        cdp = FakeCDP(frames_b64)
        bx = BaxiaClickCaptcha(FakePage(cdp))

        import builtins
        real_import = builtins.__import__
        
        def fake_import(name, *a, **kw):
            if name == "numpy":
                raise ImportError("no numpy")
            return real_import(name, *a, **kw)
        
        builtins.__import__ = fake_import
        try:
            out = bx.screenshot_instructions()
        finally:
            builtins.__import__ = real_import
        self.assertIsNotNone(out)
        self.assertEqual(Image.open(io.BytesIO(out)).format, "JPEG")

    def test_no_iframe_returns_none(self):
        """没有 punish iframe（JS_IFRAME_RECT 空）→ None，不碰 CDP。"""
        cdp = mock.Mock()
        bx = BaxiaClickCaptcha(FakePage(cdp, rect={}))
        self.assertIsNone(bx.screenshot_instructions())
        cdp.call.assert_not_called()


if __name__ == "__main__":
    unittest.main()
