"""滑块 CDP 模块修复的单元测试（2026-11-17 取证结论）。

覆盖三个定案 bug 的修复：
1. travel_for_target：加速曲线 g(t)=0.077t+0.00355t² 的逆变换
   （受控实验标定的三个实测点必须精确还原）
2. identify：2Captcha x/y 是缺口中心 → 左沿 = (x - pw/2) × scale
   （旧版按左沿换算每轮恒偏 +25.5×scale px）
3. identify 的钳制上限（disp_w - pw×scale）
"""
from __future__ import annotations

import unittest
from types import SimpleNamespace

from PIL import Image

from app.register import captcha_vision as cv
from app.register.aliyun_slider_cdp import (
    _DRAG_A,
    _DRAG_B,
    AliyunSliderCDP,
    travel_for_target,
)


def g(t: float) -> float:
    """受控实验标定的拖拽映射（2026-11-17 实测三点精确拟合）。"""
    return _DRAG_A * t + _DRAG_B * t * t


class TestTravelForTarget(unittest.TestCase):
    """曲线逆变换：g(travel_for_target(x)) 必须还原 x。"""

    # (滑块位移, 拼图 left) —— 现场实测三点
    MEASURED = [(100.0, 43.20), (150.0, 91.42), (200.0, 157.40)]

    def test_measured_points_roundtrip(self):
        for t, left in self.MEASURED:
            with self.subTest(t=t):
                self.assertAlmostEqual(g(travel_for_target(left)), left,
                                       delta=0.15)

    def test_endpoints(self):
        self.assertEqual(travel_for_target(0.0), 0.0)
        self.assertEqual(travel_for_target(-5.0), 0.0)
        # g(260)=260 恰与轨道端点对齐（实测曲线的性质）
        self.assertAlmostEqual(g(travel_for_target(260.0)), 260.0, delta=0.5)

    def test_monotonic(self):
        prev = -1.0
        for left in range(0, 261, 10):
            t = travel_for_target(float(left))
            self.assertGreaterEqual(t, prev)
            prev = t


def _make_pz() -> Image.Image:
    """60x200 透明画布，51x52 拼图块画在 (1,107)（模拟真实 pz）。"""
    pz = Image.new("RGBA", (60, 200), (0, 0, 0, 0))
    block = Image.new("RGBA", (51, 52), (40, 90, 50, 255))
    pz.paste(block, (1, 107))
    return pz


class TestIdentifyCenterToLeft(unittest.TestCase):
    """2Captcha x/y 是缺口中心：左沿 = (x - pw/2) × scale。"""

    def _run_identify(self, sol_x: float, sol_y: float):
        bg = Image.new("RGB", (296, 200), (110, 110, 110))
        pz = _make_pz()
        data = {"bgBox": {"x": 482, "y": 205, "w": 300, "h": 200}}
        paid = SimpleNamespace(
            solve=lambda bg_img, pz_img: SimpleNamespace(
                ok=True, x=sol_x, y=sol_y, cost=0.0012,
                elapsed=15.0, error=""))
        solver = AliyunSliderCDP(None, paid_client=paid)
        return solver.identify((bg, pz, data))

    def test_center_to_left_conversion(self):
        # x=214 是缺口中心（2026-11-17 实测题：左沿 190、右沿 230 附近）
        pz_c, _mask, bbox = cv.crop_to_alpha(_make_pz())
        pw = pz_c.size[0]
        scale = 300.0 / 296.0
        expect = (214.0 - pw / 2) * scale
        res = self._run_identify(214.0, 36.0)
        x_left, method, cost = res
        self.assertAlmostEqual(x_left, expect, delta=0.01)
        self.assertIn("c=214", method)
        self.assertEqual(cost, 0.0012)
        # 不再是旧版的「按左沿直接换算」：那个值会偏 +25.5×scale
        old_wrong = 214.0 * scale
        self.assertGreater(old_wrong - x_left, 20.0)

    def test_clamp_to_limit(self):
        # 中心解释后左沿超过上限 (disp_w - pw*scale) 时被钳制
        pz_c, _mask, _bbox = cv.crop_to_alpha(_make_pz())
        pw = pz_c.size[0]
        scale = 300.0 / 296.0
        limit = 300.0 - pw * scale
        x_left, _m, _c = self._run_identify(290.0, 60.0)
        self.assertAlmostEqual(x_left, limit, delta=0.01)
        self.assertLessEqual(x_left, limit + 1e-6)

    def test_no_paid_client(self):
        solver = AliyunSliderCDP(None, paid_client=None)
        self.assertIsNone(solver.identify())


if __name__ == "__main__":
    unittest.main()
