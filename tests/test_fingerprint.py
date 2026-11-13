"""指纹画像层单元测试：确定性、操作系统一致性、脚本覆盖点。"""

from __future__ import annotations

import unittest

from app.register.fingerprint_engine import (
    REGION_PROFILES,
    FingerprintProfile,
    make_profile,
    stealth_init_script,
)


class TestProfileDeterminism(unittest.TestCase):
    def test_same_seed_same_profile(self):
        a = make_profile(region="us", seed="acct_1")
        b = make_profile(region="us", seed="acct_1")
        self.assertEqual(a, b)

    def test_different_seed_different_profile(self):
        a = make_profile(region="us", seed="acct_1")
        b = make_profile(region="us", seed="acct_2")
        self.assertNotEqual(a.canvas_noise_seed, b.canvas_noise_seed)
        self.assertNotEqual(a.profile_id, b.profile_id)

    def test_canvas_seed_derived_from_seed(self):
        """canvas seed 必须由 seed 派生，不能独立随机。"""
        a1 = make_profile(region="us", seed="x1").canvas_noise_seed
        a2 = make_profile(region="us", seed="x1").canvas_noise_seed
        self.assertEqual(a1, a2)

    def test_non_deterministic_mode_uses_random(self):
        a = make_profile(region="us", seed="totally-new", deterministic=False)
        self.assertIsInstance(a, FingerprintProfile)
        self.assertGreater(a.canvas_noise_seed, 0)


class TestOsConsistency(unittest.TestCase):
    def test_windows_never_reports_apple_gpu(self):
        for i in range(40):
            p = make_profile(region="us", platform_hint="win", seed=f"w{i}")
            self.assertEqual(p.platform, "Win32")
            self.assertNotIn("Apple", p.webgl_vendor,
                             f"Windows 上出现 Apple 显卡：{p.webgl_vendor}")
            self.assertIn("Windows NT", p.user_agent)

    def test_mac_reports_apple_gpu(self):
        for i in range(10):
            p = make_profile(region="us", platform_hint="mac", seed=f"m{i}")
            self.assertEqual(p.platform, "MacIntel")
            self.assertIn("Apple", p.webgl_vendor)


class TestRegionAlignment(unittest.TestCase):
    def test_timezone_locale_from_region(self):
        for region, prof in REGION_PROFILES.items():
            for i in range(6):
                p = make_profile(region=region, seed=f"{region}{i}")
                self.assertIn(p.timezone, prof["timezones"], region)
                self.assertIn(p.locale, prof["locales"], region)

    def test_unknown_region_falls_back_to_us(self):
        p = make_profile(region="zz", seed="u1")
        self.assertIn(p.timezone, REGION_PROFILES["us"]["timezones"])


class TestStealthScript(unittest.TestCase):
    def test_script_contains_key_patches(self):
        p = make_profile(region="us", seed="s1")
        js = stealth_init_script(p)
        for token in ("webdriver", "window.chrome", "getImageData",
                      "37445", "37446",          # UNMASKED_VENDOR / RENDERER 枚举
                      "hardwareConcurrency", "deviceMemory",
                      "Intl.DateTimeFormat", "resolvedOptions"):
            self.assertIn(token, js, f"注入脚本缺 {token}")

    def test_noise_uses_coordinates_not_index(self):
        """噪声必须只依赖 (x,y,seed)，不能依赖数组下标 i —— 否则同设备两次采样不一致。"""
        js = stealth_init_script(make_profile(seed="s1"))
        # 在整个脚本里找噪声调用点
        self.assertIn("noise(x + col, y + row, seed)", js,
                      "Canvas 噪声未使用画布绝对坐标")
        self.assertNotIn("(i / 4) % w", js,
                         "Canvas 噪声仍依赖数组下标，会导致同设备采样漂移")

    def test_canvas_seed_embedded(self):
        p = make_profile(region="us", seed="embed_test")
        js = stealth_init_script(p)
        self.assertIn(str(p.canvas_noise_seed), js)
        self.assertIn(str(p.audio_noise_seed), js)

    def test_vendor_embedded(self):
        p = make_profile(region="us", platform_hint="win", seed="v1")
        js = stealth_init_script(p)
        self.assertIn(p.webgl_vendor, js)
        self.assertIn(p.webgl_renderer, js)


if __name__ == "__main__":
    unittest.main(verbosity=2)
