"""设备身份画像（identity_profile）与浏览器匹配模式测试：全离线，不碰真实 Qoder 目录。

覆盖：
- 指纹浏览器三铁律：同 seed 稳定 / 跨 seed 不同 / 字段强一致；
- profile_from_browser 与注册段 FingerprintProfile 逐字段对齐；
- qoder_reset 画像模式落盘值与稳定性；原随机方案行为不变；
- orchestrator 的 device.identity_mode 开关；
- dry-run 不写盘。
"""

from __future__ import annotations

import hashlib
import json
import unittest
import uuid as _uuid
from pathlib import Path

from app.device.identity_profile import (
    DeviceIdentityProfile,
    make_device_profile,
    profile_from_browser,
)
from app.device.qoder_reset import (
    ID_FILES,
    TELEMETRY_KEYS,
    QoderIdentityReset,
    ResetOptions,
    ResetReport,
)
from app.register.fingerprint_engine import make_profile

TMP_ROOT = Path(__file__).resolve().parent / "_tmp"
TMP_ROOT.mkdir(parents=True, exist_ok=True)


def fake_qoder_dir(tag: str = "idp") -> Path:
    d = TMP_ROOT / f"qoder_{tag}_{_uuid.uuid4().hex[:8]}"
    (d / "User" / "globalStorage").mkdir(parents=True, exist_ok=True)
    (d / "machineid").write_text("old-machine-id", encoding="utf-8")
    (d / "User" / "globalStorage" / "storage.json").write_text(
        json.dumps({
            "telemetry.machineId": "old_hash",
            "telemetry.devDeviceId": "old_dev",
            "editor.fontSize": 14,
        }),
        encoding="utf-8",
    )
    return d


# ============================================================ 画像本身
class TestIdentityProfile(unittest.TestCase):
    def test_same_seed_same_profile(self):
        a = make_device_profile(seed="acct1", timezone="Asia/Singapore")
        b = make_device_profile(seed="acct1", timezone="Asia/Singapore")
        self.assertEqual(a.as_dict(), b.as_dict())

    def test_diff_seed_diff_device(self):
        a = make_device_profile(seed="acct1")
        b = make_device_profile(seed="acct2")
        self.assertNotEqual(a.machine_id, b.machine_id)
        self.assertNotEqual(a.telemetry["telemetry.devDeviceId"],
                            b.telemetry["telemetry.devDeviceId"])
        self.assertNotEqual(a.system["system.version"], b.system["system.version"])

    def test_no_seed_random_each_time(self):
        a = make_device_profile()
        b = make_device_profile()
        self.assertNotEqual(a.machine_id, b.machine_id)

    def test_full_coverage_of_reset_constants(self):
        p = make_device_profile(seed="acct1")
        # telemetry 全键覆盖、machineId 与 machineid 同源
        self.assertEqual(set(p.telemetry), set(TELEMETRY_KEYS))
        mhash = hashlib.sha256(p.machine_id.encode()).hexdigest()
        self.assertEqual(p.telemetry["telemetry.machineId"], mhash)
        self.assertEqual(p.telemetry["machineId"], mhash)
        # ID 文件全覆盖且 machineid 与主 ID 一致
        self.assertEqual(set(p.id_file_ids), set(ID_FILES))
        self.assertEqual(p.id_file_ids["machineid"], p.machine_id)
        # 主 ID 是合法 uuid
        _uuid.UUID(p.machine_id)

    def test_internal_consistency(self):
        p = make_device_profile(seed="acct1")
        # Win 机器不配 Apple GPU
        if p.system["system.platform"] == "win32":
            self.assertNotIn("Apple", p.hardware["gpu"]["name"])
            self.assertNotIn("Apple", p.hardware["cpu"]["name"])
        # 浏览器指纹与 system 字段同源
        self.assertEqual(p.browser_fp["timezone"],
                         p.system["system.timezone"])
        self.assertEqual(p.browser_fp["language"],
                         p.system["system.locale"])


# ============================================================ 浏览器匹配
class TestBrowserMatch(unittest.TestCase):
    def test_fields_align_with_browser_profile(self):
        fp = make_profile(region="us", seed="acct42")
        dp = profile_from_browser(fp)
        # 种子沿用账号 tag
        self.assertEqual(dp.seed, "acct42")
        # 时区/语言/平台与注册环境一致
        self.assertEqual(dp.system["system.timezone"], fp.timezone)
        self.assertEqual(dp.system["system.locale"], fp.locale)
        self.assertEqual(dp.system["system.platform"], "win32")
        # CPU 线程数 = navigator.hardwareConcurrency
        self.assertEqual(dp.hardware["cpu"]["threads"], fp.hardware_concurrency)
        # 内存 = deviceMemory
        self.assertEqual(dp.hardware["memory"]["total"], f"{fp.device_memory}GB")
        # GPU 名与注册环境 WebGL renderer 同源
        self.assertIn(dp.hardware["gpu"]["name"], fp.webgl_renderer)
        # 分辨率 = 注册环境视口
        self.assertEqual(dp.hardware["display"]["resolution"],
                         f"{fp.viewport_w}x{fp.viewport_h}")
        # 内置浏览器指纹 UA / vendor 与注册环境一致
        self.assertEqual(dp.browser_fp["user_agent"], fp.user_agent)
        self.assertEqual(dp.browser_fp["webgl_vendor"], fp.webgl_vendor)
        self.assertEqual(dp.browser_fp["platform"], fp.platform)

    def test_stable_across_rebuilds(self):
        a = profile_from_browser(make_profile(region="us", seed="acct9"))
        b = profile_from_browser(make_profile(region="us", seed="acct9"))
        self.assertEqual(a.as_dict(), b.as_dict())

    def test_match_bitbrowser_env_params(self):
        """设备画像必须与实际发给比特浏览器建环境的指纹参数（bb_cdp._fingerprint_for）
        逐字段一致 —— 这是「匹配指纹浏览器」的最终校验。"""
        from app.register.bb_cdp import _fingerprint_for

        tag, region = "acct42", "us"
        bb_fp = _fingerprint_for(region, tag)          # 注册段发始比特的画像
        dp = profile_from_browser(make_profile(region=region, seed=tag))
        # 与比特环境一致的维度全部对齐
        self.assertEqual(dp.browser_fp["user_agent"], bb_fp["ua"])
        self.assertEqual(dp.hardware["display"]["resolution"],
                         f"{bb_fp['screenWidth']}x{bb_fp['screenHeight']}")
        self.assertEqual(dp.system["system.timezone"], bb_fp["timeZone"])
        self.assertEqual(dp.system["system.locale"], bb_fp["language"][0])
        self.assertEqual(dp.hardware["cpu"]["threads"],
                         bb_fp["hardwareConcurrency"])
        self.assertEqual(int(dp.hardware["memory"]["total"].rstrip("GB")),
                         bb_fp["deviceMemory"])
        self.assertIn("Windows", bb_fp["ua"])
        self.assertEqual(dp.system["system.platform"], "win32")

    def test_mac_platform(self):
        fp = make_profile(region="us", platform_hint="mac", seed="acct7")
        dp = profile_from_browser(fp)
        self.assertEqual(dp.system["system.platform"], "darwin")
        self.assertEqual(dp.system["system.arch"], "arm64")
        self.assertIn(dp.hardware["gpu"]["name"], fp.webgl_renderer)
        self.assertTrue(dp.hardware["cpu"]["name"].startswith("Apple"))
        self.assertNotIn("threads", dp.hardware["cpu"])   # mac 无超线程语义

    def test_diff_accounts_diff_devices(self):
        a = profile_from_browser(make_profile(seed="acct1"))
        b = profile_from_browser(make_profile(seed="acct2"))
        self.assertNotEqual(a.machine_id, b.machine_id)


# ============================================================ 重置落盘
class TestResetWithProfile(unittest.TestCase):
    def _run(self, d: Path, prof, steps=("machine_id", "telemetry", "hardware"),
             **kw) -> ResetReport:
        reset = QoderIdentityReset(app_dir=d, backup_dir=d / "_backup")
        return reset.run(ResetOptions(
            steps=steps, dry_run=False, backup=False, profile=prof, **kw))

    def test_profile_values_written(self):
        d = fake_qoder_dir()
        fp = make_profile(region="us", seed="acct7")
        prof = profile_from_browser(fp)
        rep = self._run(d, prof, fake_hardware=True)
        self.assertTrue(rep.ok, rep.errors)
        # machineid / 各 ID 文件全部来自画像
        self.assertEqual((d / "machineid").read_text(encoding="utf-8").strip(),
                         prof.machine_id)
        self.assertEqual((d / "deviceid").read_text(encoding="utf-8").strip(),
                         prof.id_file_ids["deviceid"])
        # storage.json：telemetry 全套画像值 + system 与注册画像一致
        data = json.loads(
            (d / "User" / "globalStorage" / "storage.json")
            .read_text(encoding="utf-8"))
        self.assertEqual(data["telemetry.devDeviceId"],
                         prof.telemetry["telemetry.devDeviceId"])
        self.assertEqual(data["telemetry.machineId"],
                         prof.telemetry["telemetry.machineId"])
        self.assertEqual(data["system.timezone"], fp.timezone)
        self.assertEqual(data["system.locale"], fp.locale)
        self.assertEqual(data["system.platform"], "win32")
        # 假硬件 JSON 来自画像（GPU/CPU/内存与注册环境对齐）
        hw = json.loads((d / "hardware_detection.json").read_text(encoding="utf-8"))
        self.assertEqual(hw, prof.hardware)
        self.assertIn(hw["gpu"]["name"], fp.webgl_renderer)

    def test_stable_across_resets(self):
        d = fake_qoder_dir()
        prof = profile_from_browser(make_profile(region="us", seed="acct9"))
        self._run(d, prof)
        first = (d / "machineid").read_text(encoding="utf-8").strip()
        data1 = json.loads(
            (d / "User" / "globalStorage" / "storage.json")
            .read_text(encoding="utf-8"))
        # 第二次重置（同 seed 画像）：同一台"物理机"，指纹不漂移
        self._run(d, prof)
        self.assertEqual((d / "machineid").read_text(encoding="utf-8").strip(),
                         first)
        data2 = json.loads(
            (d / "User" / "globalStorage" / "storage.json")
            .read_text(encoding="utf-8"))
        self.assertEqual(data1["telemetry.devDeviceId"],
                         data2["telemetry.devDeviceId"])
        self.assertEqual(data1["system.timezone"], data2["system.timezone"])

    def test_original_scheme_unchanged(self):
        d1, d2 = fake_qoder_dir("r1"), fake_qoder_dir("r2")
        for d in (d1, d2):
            self._run(d, None)
        # 原方案（profile=None）：两次重置身份必然不同
        self.assertNotEqual(
            (d1 / "machineid").read_text(encoding="utf-8"),
            (d2 / "machineid").read_text(encoding="utf-8"))
        data = json.loads(
            (d1 / "User" / "globalStorage" / "storage.json")
            .read_text(encoding="utf-8"))
        self.assertNotEqual(data["telemetry.machineId"], "old_hash")
        self.assertEqual(data.get("editor.fontSize"), 14)   # 业务键保留

    def test_dry_run_untouched_with_profile(self):
        d = fake_qoder_dir()
        before = (d / "machineid").read_text(encoding="utf-8")
        prof = profile_from_browser(make_profile(seed="acct3"))
        reset = QoderIdentityReset(app_dir=d)
        rep = reset.run(ResetOptions(dry_run=True, profile=prof))
        self.assertEqual((d / "machineid").read_text(encoding="utf-8"), before)
        self.assertTrue(any(a.startswith("[dry]") for a in rep.actions))

    def test_browser_fp_deterministic(self):
        d = fake_qoder_dir()
        reset = QoderIdentityReset(app_dir=d)
        for _ in range(2):
            prof = profile_from_browser(make_profile(seed="acct5"))
            reset.obfuscate_browser_fingerprint(
                ResetOptions(dry_run=False, profile=prof), ResetReport())
        fp1 = json.loads((d / "browser_fingerprint.json").read_text(encoding="utf-8"))
        prof = profile_from_browser(make_profile(seed="acct5"))
        self.assertEqual(fp1["canvas_fingerprint"],
                         prof.browser_fp["canvas_fingerprint"])
        self.assertEqual(fp1["audio_fingerprint"],
                         prof.browser_fp["audio_fingerprint"])


# ============================================================ 编排开关
class TestOrchestratorMode(unittest.TestCase):
    def _pipe(self, cfg):
        from app.orchestrator import Pipeline   # 局部导入：只依赖模块定义

        p = object.__new__(Pipeline)            # 不跑 __init__（不开 db/邮箱层）
        p.cfg = cfg
        return p

    def test_default_off(self):
        p = self._pipe({})
        self.assertIsNone(p._device_profile_for(1, "us"))
        p = self._pipe({"device": {"identity_mode": ""}})
        self.assertIsNone(p._device_profile_for(1, "us"))

    def test_browser_mode_matches_registration_seed(self):
        p = self._pipe({"device": {"identity_mode": "browser"}})
        prof = p._device_profile_for(42, "us")
        # 与注册段同一 seed 重建的浏览器画像逐字段一致
        bfp = make_profile(region="us", seed="acct42")
        self.assertIsInstance(prof, DeviceIdentityProfile)
        self.assertEqual(prof.seed, "acct42")
        self.assertEqual(prof.system["system.timezone"], bfp.timezone)
        self.assertEqual(prof.system["system.locale"], bfp.locale)
        self.assertEqual(prof.browser_fp["user_agent"], bfp.user_agent)
        self.assertEqual(prof.hardware["cpu"]["threads"],
                         bfp.hardware_concurrency)

    def test_bad_mode_falls_back(self):
        p = self._pipe({"device": {"identity_mode": "whatever"}})
        self.assertIsNone(p._device_profile_for(1, "us"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
