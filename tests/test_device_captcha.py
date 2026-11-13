"""设备重置与打码模块测试：都在 dry-run / mock 层面，不碰真实环境。"""

from __future__ import annotations

import json
import unittest
import uuid
from pathlib import Path

from app.device.qoder_reset import (
    ALL_STEPS,
    QoderIdentityReset,
    ResetOptions,
    ResetReport,
)
from app.net.captcha_solver import (
    CaptchaResult,
    CaptchaSolver,
    CaptchaTask,
    LocalOCR,
    Provider,
)

TMP_ROOT = Path(__file__).resolve().parent / "_tmp"
TMP_ROOT.mkdir(parents=True, exist_ok=True)


def fake_qoder_dir() -> Path:
    """造一个假的 %APPDATA%\\Qoder 结构。"""
    d = TMP_ROOT / f"qoder_{uuid.uuid4().hex[:8]}"
    (d / "User" / "globalStorage").mkdir(parents=True, exist_ok=True)
    (d / "Cache").mkdir(exist_ok=True)
    (d / "Network Persistent State").write_text("x", encoding="utf-8")
    (d / "machineid").write_text("old-machine-id", encoding="utf-8")
    (d / "User" / "globalStorage" / "storage.json").write_text(
        json.dumps({
            "telemetry.machineId": "old_hash",
            "telemetry.devDeviceId": "old_dev",
            "auth.token": "secret-token",
            "editor.fontSize": 14,
        }),
        encoding="utf-8",
    )
    return d


class TestDeviceReset(unittest.TestCase):
    def test_dry_run_writes_nothing(self):
        d = fake_qoder_dir()
        before = (d / "machineid").read_text(encoding="utf-8")
        reset = QoderIdentityReset(app_dir=d, backup_dir=d / "_backup")
        rep = reset.run(ResetOptions(dry_run=True))
        self.assertIsInstance(rep, ResetReport)
        self.assertEqual((d / "machineid").read_text(encoding="utf-8"), before)
        self.assertTrue(any(a.startswith("[dry]") for a in rep.actions))

    def test_execute_changes_ids_and_sanitizes_storage(self):
        d = fake_qoder_dir()
        reset = QoderIdentityReset(app_dir=d, backup_dir=d / "_backup")
        rep = reset.run(ResetOptions(
            steps=("machine_id", "telemetry", "login"),
            dry_run=False, backup=True,
        ))
        self.assertTrue(rep.ok, rep.errors)
        # machineid 变了
        self.assertNotEqual(
            (d / "machineid").read_text(encoding="utf-8"), "old-machine-id")
        # storage.json 里的身份键被清，业务键保留
        data = json.loads((d / "User" / "globalStorage" / "storage.json")
                          .read_text(encoding="utf-8"))
        self.assertNotIn("auth.token", data)
        self.assertEqual(data.get("editor.fontSize"), 14)
        self.assertNotEqual(data.get("telemetry.machineId"), "old_hash")
        # 备份目录生成
        self.assertTrue(rep.backup_dir and Path(rep.backup_dir).exists())

    def test_system_fingerprint_not_hardcoded_darwin(self):
        d = fake_qoder_dir()
        reset = QoderIdentityReset(app_dir=d)
        rep = reset.run(ResetOptions(
            steps=("telemetry",), dry_run=False,
            platform_hint="Windows", timezone="Europe/London", locale="en-GB",
        ))
        data = json.loads((d / "User" / "globalStorage" / "storage.json")
                          .read_text(encoding="utf-8"))
        self.assertEqual(data["system.platform"], "win32")
        self.assertEqual(data["system.timezone"], "Europe/London")
        self.assertEqual(data["system.locale"], "en-GB")

    def test_missing_app_dir_reports_error(self):
        reset = QoderIdentityReset(app_dir=TMP_ROOT / "definitely_missing_xyz")
        rep = reset.run(ResetOptions(dry_run=True))
        self.assertFalse(rep.ok)
        self.assertTrue(any("不存在" in e for e in rep.errors))

    def test_all_steps_defined(self):
        self.assertEqual(len(ALL_STEPS), 9)
        self.assertIn("machine_id", ALL_STEPS)
        self.assertIn("chat", ALL_STEPS)

    def test_status_shape(self):
        d = fake_qoder_dir()
        reset = QoderIdentityReset(app_dir=d)
        st = reset.status()
        self.assertTrue(st["app_dir_exists"])
        self.assertEqual(st["machine_id"], "old-machine-id")
        self.assertIn("telemetry_machine_id", st)


class _AlwaysFail(Provider):
    name = "fail"
    price = {"image": 0.01}

    def __init__(self) -> None:
        super().__init__("k")

    def supports(self, kind: str) -> bool:
        return True

    def solve(self, task: CaptchaTask) -> CaptchaResult:
        from app.net.captcha_solver import CaptchaError
        raise CaptchaError("故意失败")


class _AlwaysOk(Provider):
    name = "ok"
    price = {"image": 0.002}

    def __init__(self) -> None:
        super().__init__("k")

    def supports(self, kind: str) -> bool:
        return True

    def solve(self, task: CaptchaTask) -> CaptchaResult:
        return CaptchaResult("TOKEN_OK", self.name, 0.002, 0.1)


class TestCaptchaSolver(unittest.TestCase):
    def test_fallback_to_next_provider(self):
        solver = CaptchaSolver([_AlwaysFail(), _AlwaysOk()],
                               prefer={"image": ["fail", "ok"]})
        res = solver.solve(CaptchaTask(kind="image", image_b64="xx"))
        self.assertEqual(res.token, "TOKEN_OK")
        self.assertAlmostEqual(solver.spent, 0.002)
        self.assertEqual(solver.solved, 1)

    def test_all_fail_raises(self):
        from app.net.captcha_solver import CaptchaError
        solver = CaptchaSolver([_AlwaysFail()], prefer={"image": ["fail"]})
        with self.assertRaises(CaptchaError):
            solver.solve(CaptchaTask(kind="image", image_b64="xx"))
        self.assertEqual(solver.failed, 1)

    def test_unconfigured_kind_skipped(self):
        from app.net.captcha_solver import CaptchaError
        solver = CaptchaSolver([_AlwaysOk()], prefer={"turnstile": ["ok"]})
        self.assertEqual(solver.solve(CaptchaTask(kind="turnstile")).token, "TOKEN_OK")

    def test_local_ocr_only_image(self):
        p = LocalOCR()
        self.assertTrue(p.supports("image"))
        self.assertFalse(p.supports("recaptcha_v2"))

    def test_cost_summary(self):
        solver = CaptchaSolver([_AlwaysOk()], prefer={"image": ["ok"]})
        solver.solve(CaptchaTask(kind="image"))
        self.assertIn("打码成功 1 次", solver.cost_summary())


if __name__ == "__main__":
    unittest.main(verbosity=2)
