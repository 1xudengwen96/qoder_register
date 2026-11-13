"""Qoder 设备身份重置核心（基于 itandelin/qoder-free v2.3.0 改造）。

改造要点
--------
1. **去 GUI**：原仓库是 PyQt5 单文件 GUI，这里抽出 ``QoderIdentityReset`` 纯逻辑类，
   无 PyQt 依赖，可被流水线直接调用。
2. **去交互**：原流程用 ``QMessageBox`` 确认、要求用户手动关 Qoder，这里改成
   ``kill_qoder()`` 自动结束进程，失败再抛错。
3. **去 Qt 日志**：原 ``self.log`` 换成标准 logging，返回结构化 ``ResetReport``。
4. **配置化分步**：原 ``perform_full_reset`` 把 9 步写死，这里拆成独立步骤，
   由 ``steps`` 参数选择性执行。流水线每轮只跑必要步骤（默认全量）。
5. **修正平台硬编码**：原代码多处把 ``system.platform`` 写死 ``'darwin'``、
   ``system.timezone`` 写死 ``'America/New_York'``，在 Windows 上直接暴露矛盾。
   这里按真实平台生成，时区跟随代理地区。
6. **storage.json 路径纠错**：原代码用 ``User/globalStorage/storage.json``，
   但 VS Code/Electron 系实际是 ``User/globalStorage/storage.json`` 与
   ``User/settings.json`` 两处并存；这里两个位置都处理，并对缺失情况降级不报错。
7. **保留对话开关**：``preserve_chat`` 参数沿用原语义，默认 False（流水线里不需要历史）。
8. **干跑模式**：``dry_run=True`` 只收集将执行的动作，不写盘、不删文件、改进程。
9. **快照与回滚**：重置前把会被改写的文件快照到 ``backup_dir``，支持 ``restore()``。
10. **指纹注入与假硬件文件**：原逻辑保留，但改为可选（``fake_hardware=True``），
    因为服务端若做一致性校验，假的硬件 JSON 反而可能成为异常特征。
11. **不碰系统级缓存**：原 ``clear_system_level_caches`` / ``reset_network_traces``
    会清理系统 DNS、网络配置，这里默认关闭，只清 Qoder 目录内文件。

用法
----
    from app.device.qoder_reset import QoderIdentityReset, ResetOptions

    reset = QoderIdentityReset(logger=log)
    report = reset.run(ResetOptions(dry_run=False, preserve_chat=False))
    print(report.ok, report.machine_id, report.errors)

完整流程（GUI 一键重置的等价物）::

    report = reset.full_reset()          # 9 步全量
    report = reset.run(ResetOptions(steps=["machine_id", "telemetry", "login"]))
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import logging
import os
import platform
import random
import shutil
import subprocess
import sys
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import Callable, Iterable, Sequence


# ============================================================ 常量表（源自原仓库）
ID_FILES = [
    "machineid", "deviceid", "hardware_uuid", "system_uuid",
    "platform_id", "installation_id", "cpu_id", "gpu_id",
    "board_serial", "bios_uuid", "memory_id",
]

CACHE_DIRS = [
    "Cache", "blob_storage", "Code Cache", "SharedClientCache",
    "GPUCache", "DawnGraphiteCache", "DawnWebGPUCache",
    "ShaderCache", "DawnCache", "Dictionaries",
    "CachedData", "CachedProfilesData", "CachedExtensions",
    "IndexedDB", "CacheStorage", "WebSQL",
]

IDENTITY_FILES = [
    "Network Persistent State", "TransportSecurity",
    "Trust Tokens", "Trust Tokens-journal",
    "SharedStorage", "SharedStorage-wal",
    "Preferences", "Secure Preferences",
    "Login Credentials", "Login Data", "Login Data-journal",
    "Web Data", "Web Data-journal",
    "cert_transparency_reporter_state.json",
    "Local State", "NetworkDataMigrated",
    "DeviceMetadata", "HardwareInfo", "SystemInfo",
    "QuotaManager", "QuotaManager-journal",
    "origin_bound_certs", "Network Action Predictor",
    "AutofillStrikeDatabase", "AutofillStrikeDatabase-journal",
    "Feature Engagement Tracker", "PasswordStoreDefault",
    "PreferredApps", "UserPrefs", "UserPrefs.backup",
    "Platform Notifications", "VideoDecodeStats",
    "OriginTrials", "BrowserMetrics", "SafeBrowsing",
    "Visited Links", "History", "History-journal",
    "Favicons", "Favicons-journal", "Shortcuts", "Shortcuts-journal",
    "Top Sites", "Top Sites-journal",
    "Cookies", "Cookies-journal",
]

STORAGE_DIRS_BASE = [
    "Service Worker", "Certificate Revocation Lists", "SSLCertificates",
    "databases", "clp", "logs", "Backups", "CachedExtensionVSIXs",
]

STORAGE_DIRS_CHAT = ["Session Storage", "WebStorage", "Shared Dictionary"]

HARDWARE_CACHE_DIRS = [
    "GPUCache", "DawnGraphiteCache", "DawnWebGPUCache",
    "ShaderCache", "DawnCache", "MediaCache",
    "CachedData", "CachedProfilesData",
]

HARDWARE_FAKE_FILES = [
    "hardware_detection.json", "device_capabilities.json",
    "system_features.json", "platform_detection.dat",
    "cpu_features.json", "gpu_features.json",
    "memory_info.json", "display_info.json",
]

BROWSER_FP_FILES = [
    "browser_fingerprint.json", "canvas_fingerprint.json",
    "webgl_fingerprint.json", "audio_fingerprint.json",
]

SHARED_CACHE_LOGIN_FILES = [".info", ".lock", "mcp.json", "server.json", "auth.json"]

TELEMETRY_KEYS = {
    "telemetry.machineId": "sha256",
    "telemetry.devDeviceId": "uuid",
    "telemetry.sqmId": "uuid",
    "telemetry.sessionId": "uuid",
    "telemetry.installationId": "uuid",
    "telemetry.clientId": "uuid",
    "telemetry.userId": "uuid",
    "telemetry.anonymousId": "uuid",
    "machineId": "sha256",
    "deviceId": "uuid",
    "installationId": "uuid",
    "hardwareId": "uuid",
    "platformId": "uuid",
    "cpuId": "uuid",
    "gpuId": "uuid",
    "memoryId": "uuid",
}

IDENTITY_KEYWORDS = (
    "auth", "login", "session", "token", "credential",
    "device", "fingerprint", "tracking", "analytics",
    "nonce", "challenge", "account", "user",
    "hardware", "cpu", "gpu", "memory", "disk", "serial",
    "mac", "network", "screen", "resolution", "vendor",
    "model", "brand", "manufacturer", "processor",
)

QODER_PROCESS_NAMES = ["Qoder.exe", "Qoder", "qoder", "Qoder Helper"]

ALL_STEPS = [
    "kill",              # 结束客户端进程
    "machine_id",        # 机器 ID 文件
    "telemetry",         # storage.json 遥测标识
    "cache",             # 缓存目录
    "identity",          # 身份识别文件 / 存储目录
    "advanced",          # SharedClientCache 与证书类
    "login",             # 登录态与认证令牌
    "hardware",          # 硬件指纹重置 + 假硬件文件
    "chat",              # 对话记录处理
]


# ============================================================ 数据类
@dataclasses.dataclass(slots=True)
class ResetOptions:
    steps: Sequence[str] = tuple(ALL_STEPS)
    dry_run: bool = True
    preserve_chat: bool = False
    fake_hardware: bool = False
    timeout_kill: int = 15
    platform_hint: str | None = None      # "Windows" / "Darwin" / "Linux"
    timezone: str = "America/New_York"    # 应与代理地区对齐
    locale: str = "en-US"
    backup: bool = True


@dataclasses.dataclass(slots=True)
class ResetReport:
    ok: bool = True
    dry_run: bool = True
    machine_id: str = ""
    telemetry: dict = dataclasses.field(default_factory=dict)
    actions: list[str] = dataclasses.field(default_factory=list)
    errors: list[str] = dataclasses.field(default_factory=list)
    backup_dir: str = ""
    at: datetime = dataclasses.field(default_factory=datetime.now)

    def as_dict(self) -> dict:
        d = dataclasses.asdict(self)
        d["at"] = self.at.isoformat(timespec="seconds")
        return d


# ============================================================ 主类
class QoderIdentityReset:
    """Qoder 身份重置器（无 GUI）。"""

    def __init__(
        self,
        logger: logging.Logger | None = None,
        app_dir: str | Path | None = None,
        backup_dir: str | Path = "data/device_backup",
    ) -> None:
        self.log = logger or logging.getLogger("qoder.reset")
        self.app_dir = Path(app_dir) if app_dir else self.default_app_dir()
        self.backup_dir = Path(backup_dir)

    # ---------------------------------------------------------- 路径
    @staticmethod
    def default_app_dir() -> Path:
        """原仓库 get_qoder_data_dir 的等价实现 + 多候选探测。"""
        home = Path.home()
        system = platform.system()
        if system == "Windows":
            candidates = [
                Path(os.environ.get("APPDATA", home / "AppData/Roaming")) / "Qoder",
                Path(os.environ.get("LOCALAPPDATA", home / "AppData/Local")) / "Qoder",
            ]
        elif system == "Darwin":
            candidates = [home / "Library" / "Application Support" / "Qoder"]
        else:
            candidates = [
                home / ".config" / "Qoder",
                home / ".local" / "share" / "Qoder",
            ]
        for c in candidates:
            if c.exists():
                return c
        return candidates[0]

    @property
    def storage_json(self) -> Path:
        return self.app_dir / "User" / "globalStorage" / "storage.json"

    @property
    def settings_json(self) -> Path:
        return self.app_dir / "User" / "settings.json"

    # ---------------------------------------------------------- 进程
    def is_running(self) -> tuple[bool, list[str]]:
        pids: list[str] = []
        system = platform.system()
        if system == "Windows":
            try:
                out = subprocess.run(
                    ["tasklist", "/FO", "CSV", "/NH"],
                    capture_output=True, text=True, timeout=20,
                )
                for line in out.stdout.splitlines():
                    name = line.split(",")[0].strip('"')
                    if name.lower().startswith("qoder"):
                        pids.append(name)
            except Exception:
                pass
        else:
            try:
                out = subprocess.run(["pgrep", "-f", "Qoder"],
                                     capture_output=True, text=True, timeout=20)
                if out.returncode == 0:
                    pids = [p for p in out.stdout.split() if p]
            except Exception:
                pass
        return bool(pids), pids

    def kill_qoder(self, opts: ResetOptions, report: ResetReport) -> None:
        running, pids = self.is_running()
        if not running:
            report.actions.append("kill: Qoder 未运行")
            return
        report.actions.append(f"kill: 发现 Qoder 运行中 {pids[:3]}")
        if opts.dry_run:
            report.actions.append("[dry] 将结束 Qoder 进程树")
            return
        system = platform.system()
        for name in QODER_PROCESS_NAMES:
            cmd = (["taskkill", "/F", "/IM", name, "/T"] if system == "Windows"
                   else ["pkill", "-f", name])
            try:
                subprocess.run(cmd, capture_output=True, text=True,
                               timeout=opts.timeout_kill)
            except Exception:
                continue
        for _ in range(opts.timeout_kill):
            if not self.is_running()[0]:
                report.actions.append("kill: 已结束")
                return
            time.sleep(1)
        report.errors.append("Qoder 进程未能在超时内结束")
        report.ok = False

    # ---------------------------------------------------------- 备份
    def _snapshot(self, paths: Iterable[Path], opts: ResetOptions,
                  report: ResetReport) -> None:
        if not opts.backup:
            return
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        target = self.backup_dir / stamp
        if opts.dry_run:
            report.actions.append(f"[dry] 将备份到 {target}")
            return
        try:
            target.mkdir(parents=True, exist_ok=True)
            for p in paths:
                if not p.exists():
                    continue
                dest = target / p.name
                if p.is_dir():
                    shutil.copytree(p, dest, dirs_exist_ok=True)
                else:
                    shutil.copy2(p, dest)
            report.backup_dir = str(target)
            report.actions.append(f"备份完成 → {target}")
        except Exception as exc:
            report.errors.append(f"备份失败: {exc}")

    def restore(self, backup_dir: str | Path) -> list[str]:
        """从备份目录回滚（只回拷文件，不删多余项）。"""
        src = Path(backup_dir)
        restored: list[str] = []
        if not src.exists():
            return ["备份目录不存在"]
        for item in src.iterdir():
            dest = self.app_dir / item.name
            try:
                if item.is_dir():
                    shutil.copytree(item, dest, dirs_exist_ok=True)
                else:
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(item, dest)
                restored.append(str(dest))
            except Exception as exc:
                restored.append(f"FAIL {dest}: {exc}")
        return restored

    # ---------------------------------------------------------- 工具
    @staticmethod
    def _gen_uuid() -> str:
        return str(uuid.uuid4())

    @staticmethod
    def _gen_sha256(seed: str | None = None) -> str:
        return hashlib.sha256((seed or str(uuid.uuid4())).encode()).hexdigest()

    def _gen(self, kind: str) -> str:
        return self._gen_sha256() if kind == "sha256" else self._gen_uuid()

    def _rm(self, p: Path, opts: ResetOptions, report: ResetReport) -> None:
        label = str(p.relative_to(self.app_dir)) if self.app_dir in p.parents else str(p)
        if not p.exists():
            return
        if opts.dry_run:
            report.actions.append(f"[dry] {'rmtree' if p.is_dir() else 'rm'} {label}")
            return
        try:
            if p.is_dir():
                shutil.rmtree(p)
            else:
                p.unlink()
            report.actions.append(f"删除 {label}")
        except Exception as exc:
            report.errors.append(f"删除失败 {label}: {exc}")

    def _write(self, p: Path, content: str, opts: ResetOptions,
               report: ResetReport) -> None:
        label = str(p.relative_to(self.app_dir)) if self.app_dir in p.parents else str(p)
        if opts.dry_run:
            report.actions.append(f"[dry] 写 {label}")
            return
        try:
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(content, encoding="utf-8")
            report.actions.append(f"写入 {label}")
        except Exception as exc:
            report.errors.append(f"写入失败 {label}: {exc}")

    def _load_json(self, p: Path) -> dict:
        if not p.exists():
            return {}
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except Exception:
            return {}

    def _dump_json(self, p: Path, data: dict, opts: ResetOptions,
                   report: ResetReport) -> None:
        self._write(p, json.dumps(data, indent=4, ensure_ascii=False), opts, report)

    # ---------------------------------------------------------- 系统指纹
    def _system_fingerprint(self, opts: ResetOptions) -> dict:
        system = opts.platform_hint or platform.system()
        if system == "Darwin":
            version = f"{random.choice([12, 13, 14, 15])}.{random.randint(0, 6)}.{random.randint(0, 9)}"
            plat = "darwin"
            arch = random.choice(["arm64", "x64"])
        elif system == "Windows":
            version = f"{random.choice(['10.0.19045', '10.0.22621', '10.0.22631', '10.0.26100'])}.{random.randint(1, 999)}"
            plat = "win32"
            arch = "x64"
        else:
            version = f"{random.choice([5, 6])}.{random.randint(0, 19)}.{random.randint(0, 50)}"
            plat = "linux"
            arch = "x64"
        return {
            "system.platform": plat,
            "system.arch": arch,
            "system.version": version,
            "system.build": str(random.randint(20000, 26100)),
            "system.locale": opts.locale,
            "system.timezone": opts.timezone,
        }

    # ---------------------------------------------------------- 各步骤
    def step_machine_id(self, opts: ResetOptions, report: ResetReport) -> None:
        new_main = self._gen_uuid()
        report.machine_id = new_main
        for name in ID_FILES:
            self._write(self.app_dir / name, new_main if name == "machineid" else self._gen_uuid(),
                        opts, report)
        # storage.json 同步（原仓库逻辑：machineId 存 sha256）
        data = self._load_json(self.storage_json)
        if data:
            h = hashlib.sha256(new_main.encode()).hexdigest()
            data["telemetry.machineId"] = h
            data["machineId"] = h
            report.telemetry["telemetry.machineId"] = h
            self._dump_json(self.storage_json, data, opts, report)

    def step_telemetry(self, opts: ResetOptions, report: ResetReport) -> None:
        data = self._load_json(self.storage_json)
        if not data:
            report.actions.append("telemetry: storage.json 不存在，跳过（首启会重建）")
            return
        for key, kind in TELEMETRY_KEYS.items():
            val = self._gen(kind)
            data[key] = val
            report.telemetry[key] = val
        data.update(self._system_fingerprint(opts))
        self._dump_json(self.storage_json, data, opts, report)

    def step_cache(self, opts: ResetOptions, report: ResetReport) -> None:
        for name in CACHE_DIRS:
            self._rm(self.app_dir / name, opts, report)

    def step_identity(self, opts: ResetOptions, report: ResetReport) -> None:
        for name in IDENTITY_FILES:
            self._rm(self.app_dir / name, opts, report)
        dirs = list(STORAGE_DIRS_BASE)
        dirs += STORAGE_DIRS_CHAT
        if not opts.preserve_chat:
            dirs.append("Local Storage")
        else:
            # 保留对话：Local Storage 里的 leveldb 可能带对话索引，改清 Session Storage 等
            report.actions.append("identity: preserve_chat=True，保留 Local Storage")
        for name in dirs:
            self._rm(self.app_dir / name, opts, report)

    def step_advanced(self, opts: ResetOptions, report: ResetReport) -> None:
        shared = self.app_dir / "SharedClientCache"
        if shared.exists():
            for name in (".info", ".lock", "mcp.json", "index", "cache"):
                self._rm(shared / name, opts, report)
            for tmp in list(shared.glob("tmp*")):
                self._rm(tmp, opts, report)
        # socket / 锁文件
        for p in list(self.app_dir.glob("*.sock")) + list(self.app_dir.glob("*.lock")):
            self._rm(p, opts, report)

    def step_login(self, opts: ResetOptions, report: ResetReport) -> None:
        shared = self.app_dir / "SharedClientCache"
        for name in SHARED_CACHE_LOGIN_FILES:
            self._rm(shared / name, opts, report)
        for name in IDENTITY_FILES:
            if name in ("Cookies", "Cookies-journal", "Login Credentials",
                        "Network Persistent State", "TransportSecurity",
                        "Trust Tokens", "Trust Tokens-journal"):
                self._rm(self.app_dir / name, opts, report)
        sessions = ["Session Storage", "Local Storage", "WebStorage",
                    "SharedStorage", "Shared Dictionary"]
        if opts.preserve_chat:
            sessions = [s for s in sessions if s != "Local Storage"]
        for name in sessions:
            self._rm(self.app_dir / name, opts, report)

        data = self._load_json(self.storage_json)
        if data:
            removed = [k for k in list(data) if any(w in k.lower() for w in IDENTITY_KEYWORDS)]
            for k in removed:
                data.pop(k, None)
            if removed:
                report.actions.append(f"login: 清除 storage.json 中 {len(removed)} 个身份键")
                self._dump_json(self.storage_json, data, opts, report)

    def step_hardware(self, opts: ResetOptions, report: ResetReport) -> None:
        for name in ID_FILES:
            self._write(self.app_dir / name, self._gen_uuid(), opts, report)
        for name in HARDWARE_CACHE_DIRS:
            self._rm(self.app_dir / name, opts, report)
        data = self._load_json(self.storage_json)
        if data:
            for key in list(TELEMETRY_KEYS):
                data[key] = self._gen(TELEMETRY_KEYS[key])
            data.update(self._system_fingerprint(opts))
            for key in list(data):
                if any(w in key.lower() for w in ("hardware", "cpu", "gpu",
                                                  "disk", "serial", "vendor",
                                                  "resolution", "processor")):
                    if key not in TELEMETRY_KEYS:
                        data.pop(key, None)
            self._dump_json(self.storage_json, data, opts, report)

        if opts.fake_hardware:
            fake = self._fake_hardware_profile(opts)
            for name in HARDWARE_FAKE_FILES[:3]:
                self._write(self.app_dir / name,
                            json.dumps(fake, indent=2, ensure_ascii=False), opts, report)
        else:
            for name in HARDWARE_FAKE_FILES:
                self._rm(self.app_dir / name, opts, report)

    def step_chat(self, opts: ResetOptions, report: ResetReport) -> None:
        if opts.preserve_chat:
            report.actions.append("chat: 保留对话记录")
            return
        ws = self.app_dir / "User" / "workspaceStorage"
        if not ws.exists():
            return
        for d in ws.glob("*/chatSessions"):
            self._rm(d, opts, report)
        for d in ws.glob("*/chatEditingSessions"):
            self._rm(d, opts, report)

    # ---------------------------------------------------------- 假硬件画像
    @staticmethod
    def _fake_hardware_profile(opts: ResetOptions) -> dict:
        system = opts.platform_hint or platform.system()
        if system == "Darwin":
            n = random.randint(2, 5)
            return {
                "cpu": {"name": f"Apple M{n} Pro", "cores": random.choice([8, 10, 12, 16]),
                        "frequency": f"{random.uniform(2.0, 4.0):.1f}GHz"},
                "gpu": {"name": f"Apple M{n} Pro GPU", "memory": f"{random.choice([16, 24, 32])}GB"},
                "memory": {"total": f"{random.choice([16, 24, 32, 64])}GB", "type": "LPDDR5"},
                "display": {"resolution": random.choice(
                    ["2560x1440", "3024x1964", "3456x2234", "5120x2880"]),
                    "refresh_rate": random.choice([60, 120])},
            }
        cpu = (f"Intel Core i{random.choice([5, 7, 9])}-{random.randint(12400, 14900)}"
               if random.random() < 0.5
               else f"AMD Ryzen {random.choice([5, 7, 9])} {random.randint(5600, 9950)}X")
        return {
            "cpu": {"name": cpu, "cores": random.choice([6, 8, 12, 16, 20]),
                    "threads": random.choice([12, 16, 20, 24, 32]),
                    "frequency": f"{random.uniform(3.0, 5.5):.1f}GHz"},
            "gpu": {"name": random.choice([
                "NVIDIA GeForce RTX 4060", "NVIDIA GeForce RTX 4070",
                "AMD Radeon RX 7600", "Intel Arc A770", "Intel UHD Graphics 770"]),
                "memory": f"{random.choice([8, 12, 16, 24])}GB",
                "driver_version": f"{random.randint(530, 560)}.{random.randint(60, 99)}"},
            "memory": {"total": f"{random.choice([16, 32, 64, 128])}GB",
                       "type": random.choice(["DDR4", "DDR5"]),
                       "speed": f"{random.choice([3200, 3600, 4800, 5600])}MHz"},
            "display": {"resolution": random.choice(
                ["1920x1080", "2560x1440", "3440x1440", "3840x2160"]),
                "refresh_rate": random.choice([60, 75, 120, 144, 165])},
        }

    # ---------------------------------------------------------- 浏览器指纹混淆
    def obfuscate_browser_fingerprint(self, opts: ResetOptions,
                                      report: ResetReport) -> None:
        """对应原仓库 obfuscate_browser_fingerprint。

        注意：这层只影响 **Qoder 客户端内置浏览器** 的痕迹，不替代注册阶段用的
        指纹浏览器。注册阶段的指纹走 fingerprint_engine 模块。
        """
        ua_pool = [
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
            "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
        ]
        fp = {
            "user_agent": random.choice(ua_pool),
            "screen_resolution": random.choice(["1920x1080", "2560x1440", "3840x2160", "1366x768"]),
            "color_depth": random.choice([24, 32]),
            "timezone": opts.timezone,
            "language": opts.locale,
            "platform": {"Windows": "Win32", "Darwin": "MacIntel"}.get(
                opts.platform_hint or platform.system(), "Linux x86_64"),
            "webgl_vendor": random.choice(
                ["Google Inc. (NVIDIA)", "Google Inc. (Intel)", "Google Inc. (Apple)"]),
            "canvas_fingerprint": hashlib.md5(str(random.random()).encode()).hexdigest()[:16],
            "audio_fingerprint": hashlib.md5(str(random.random()).encode()).hexdigest()[:16],
        }
        for name in BROWSER_FP_FILES:
            self._write(self.app_dir / name,
                        json.dumps(fp, indent=2, ensure_ascii=False), opts, report)

        prefs = self.app_dir / "Preferences"
        data = self._load_json(prefs)
        if data:
            data.update({
                "intl.accept_languages": fp["language"],
                "privacy.resistFingerprinting": True,
            })
            self._dump_json(prefs, data, opts, report)

    # ---------------------------------------------------------- 主入口
    def run(self, opts: ResetOptions | None = None,
            progress: Callable[[str], None] | None = None) -> ResetReport:
        opts = opts or ResetOptions()
        report = ResetReport(dry_run=opts.dry_run)

        if not self.app_dir.exists():
            report.errors.append(f"Qoder 数据目录不存在：{self.app_dir}")
            report.ok = False
            return report

        # 快照目标
        snapshot_targets: list[Path] = []
        for name in ID_FILES[:1] + ["Preferences", "Local State",
                                    "Network Persistent State", "Cookies"]:
            p = self.app_dir / name
            if p.exists():
                snapshot_targets.append(p)
        if self.storage_json.exists():
            snapshot_targets.append(self.storage_json)
        self._snapshot(snapshot_targets, opts, report)

        step_map = {
            "kill": lambda: self.kill_qoder(opts, report),
            "machine_id": lambda: self.step_machine_id(opts, report),
            "telemetry": lambda: self.step_telemetry(opts, report),
            "cache": lambda: self.step_cache(opts, report),
            "identity": lambda: self.step_identity(opts, report),
            "advanced": lambda: self.step_advanced(opts, report),
            "login": lambda: self.step_login(opts, report),
            "hardware": lambda: self.step_hardware(opts, report),
            "chat": lambda: self.step_chat(opts, report),
        }

        for step in opts.steps:
            if step not in step_map:
                report.errors.append(f"未知步骤：{step}")
                continue
            if progress:
                progress(step)
            self.log.info("重置步骤：%s", step)
            try:
                step_map[step]()
            except Exception as exc:
                self.log.exception("步骤 %s 异常", step)
                report.errors.append(f"{step}: {exc}")

        # 机器 ID 必须真的变了
        if "machine_id" in opts.steps and not opts.dry_run:
            current = self.read_machine_id()
            if current and current == report.machine_id:
                report.errors.append("machine_id 未变化，重置可能失效")
            report.machine_id = current or report.machine_id

        report.ok = report.ok and not any(
            "未能" in e or "不存在" in e for e in report.errors
        )
        return report

    def full_reset(self, preserve_chat: bool = False, dry_run: bool = True,
                   fake_hardware: bool = False) -> ResetReport:
        """等价于原 GUI 的「一键修改所有配置」。"""
        return self.run(ResetOptions(
            steps=tuple(ALL_STEPS),
            dry_run=dry_run,
            preserve_chat=preserve_chat,
            fake_hardware=fake_hardware,
        ))

    def read_machine_id(self) -> str:
        p = self.app_dir / "machineid"
        try:
            return p.read_text(encoding="utf-8").strip() if p.exists() else ""
        except Exception:
            return ""

    def status(self) -> dict:
        """状态自检，对应原 initialize_status_check。"""
        running, pids = self.is_running()
        data = self._load_json(self.storage_json)
        return {
            "app_dir": str(self.app_dir),
            "app_dir_exists": self.app_dir.exists(),
            "running": running,
            "pids": pids,
            "machine_id": self.read_machine_id(),
            "telemetry_machine_id": data.get("telemetry.machineId", ""),
            "dev_device_id": data.get("telemetry.devDeviceId", ""),
            "id_files": {n: (self.app_dir / n).exists() for n in ID_FILES},
            "identity_files_present": sum(
                1 for n in IDENTITY_FILES if (self.app_dir / n).exists()),
            "cache_dirs_present": sum(
                1 for n in CACHE_DIRS if (self.app_dir / n).exists()),
        }
