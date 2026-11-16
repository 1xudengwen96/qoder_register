"""客户端路径自动探测：不许依赖任何一台机器的硬编码路径。"""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from app.claim.client_path import (
    clean_env,
    exe_from_command,
    find_client,
    score_candidate,
)


def _fake_exe(path: Path, size_mb: float = 200.0, pak: bool = True) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("wb") as fh:          # 稀疏文件：只占元数据，不真写几百 MB
        fh.seek(int(size_mb * 1024 * 1024) - 1)
        fh.write(b"\0")
    if pak:
        (path.parent / "resources").mkdir(exist_ok=True)
        (path.parent / "resources" / "app.asar").write_bytes(b"x")
        (path.parent / "resources.pak").write_bytes(b"x")
    return path


class TestCommandParsing(unittest.TestCase):
    def test_quoted_path_with_args(self):
        cmd = r'"E:\Qoder IDE\Qoder IDE.exe" --open-url -- "%1"'
        self.assertEqual(exe_from_command(cmd), Path(r"E:\Qoder IDE\Qoder IDE.exe"))

    def test_unquoted_path(self):
        self.assertEqual(exe_from_command(r"C:\Qoder\qoder.exe --open-url"),
                         Path(r"C:\Qoder\qoder.exe"))

    def test_empty(self):
        self.assertIsNone(exe_from_command(""))
        self.assertIsNone(exe_from_command("no exe here"))


class TestScoring(unittest.TestCase):
    def test_launcher_small_is_positive(self):
        with tempfile.TemporaryDirectory() as d:
            exe = Path(d) / "Qoder Launcher.exe"
            exe.write_bytes(b"\0" * 120_000)      # 启动器就是几十 KB
            score, notes = score_candidate(exe)
        self.assertGreater(score, 0)
        self.assertIn("启动器入口", notes)

    def test_uninstaller_rejected(self):
        with tempfile.TemporaryDirectory() as d:
            exe = Path(d) / "Uninstall Qoder.exe"
            exe.write_bytes(b"\0" * 1024)
            score, _ = score_candidate(exe)
        self.assertLess(score, 0)

    def test_runtime_helper_rejected(self):
        with tempfile.TemporaryDirectory() as d:
            exe = Path(d) / "runtime-info.exe"
            exe.write_bytes(b"\0" * 400_000)
            score, _ = score_candidate(exe)
        self.assertLess(score, 0)

    def test_version_payload_penalised_vs_launcher(self):
        with tempfile.TemporaryDirectory() as d:
            base = Path(d) / ".qoder-versions" / "0.5.7"
            payload = _fake_exe(base / "Qoder.exe")
            s_payload, notes = score_candidate(payload)
        self.assertIn("版本载荷(优先用 launcher)", notes)
        self.assertGreater(s_payload, 0)


class TestDiscovery(unittest.TestCase):
    def test_finds_launcher_under_fake_localappdata(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            launcher = root / "Qoder" / "Qoder Launcher" / "Qoder Launcher.exe"
            launcher.parent.mkdir(parents=True, exist_ok=True)
            launcher.write_bytes(b"\0" * 120_000)
            _fake_exe(root / "Programs" / "Qoder" / "Qoder.exe")

            with mock.patch.dict(os.environ, {"LOCALAPPDATA": str(root)}, clear=False), \
                 mock.patch("app.claim.client_path._registry_command", return_value=""), \
                 mock.patch("app.claim.client_path._uninstall_candidates", return_value=[]):
                path, chosen, cands = find_client(include_scan=False)

        self.assertIsNotNone(path)
        self.assertEqual(path.name, "Qoder Launcher.exe")
        self.assertIn("launcher", chosen.source + str(path).lower())

    def test_registry_wins_when_points_to_real_exe(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            ide = _fake_exe(root / "SomeWhere" / "Qoder IDE.exe")
            with mock.patch("app.claim.client_path._registry_command",
                            return_value=f'"{ide}" --open-url -- "%1"'), \
                 mock.patch("app.claim.client_path._uninstall_candidates", return_value=[]), \
                 mock.patch.dict(os.environ, {"LOCALAPPDATA": str(root / "empty")}, clear=False):
                path, chosen, _ = find_client(include_scan=False)
        self.assertEqual(path, ide)
        self.assertTrue(chosen.source.startswith("registry"))

    def test_nothing_found(self):
        with tempfile.TemporaryDirectory() as d:
            with mock.patch("app.claim.client_path._registry_command", return_value=""), \
                 mock.patch("app.claim.client_path._uninstall_candidates", return_value=[]), \
                 mock.patch.dict(os.environ, {"LOCALAPPDATA": str(Path(d) / "nope")},
                                 clear=False):
                path, chosen, cands = find_client(include_scan=False)
        self.assertIsNone(path)
        self.assertIsNone(chosen)


class TestEnvCleaning(unittest.TestCase):
    def test_strips_electron_run_as_node(self):
        env = clean_env({"ELECTRON_RUN_AS_NODE": "1", "PATH": "x"})
        self.assertNotIn("ELECTRON_RUN_AS_NODE", env)
        self.assertEqual(env["PATH"], "x")

    def test_does_not_drop_other_vars(self):
        env = clean_env({"A": "1"})
        self.assertEqual(env.get("A"), "1")


if __name__ == "__main__":
    unittest.main()
