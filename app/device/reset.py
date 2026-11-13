"""设备标识重置。

核心顺序（顺序错了就白干）：
  1. 结束客户端进程；
  2. 删 / 改 Machine ID、Telemetry ID；
  3. 清硬件指纹缓存；
  4. 改 MAC 地址（网卡高级属性 Network Address）；
  5. 改磁盘卷序列号（Windows 用 fsutil 仅能改非系统卷，或在卷层改写）；
  6. 删 SafeStore 文件；
  7. 重开客户端前校验 Machine ID 确实变了（否则工具已过时）。

所有动作默认 dry_run=True，只打印将要做什么，确认后再真跑。
"""

from __future__ import annotations

import dataclasses
import os
import platform
import re
import shutil
import subprocess
import time
import uuid
from datetime import datetime
from pathlib import Path


# ---------------------------------------------------------------- 路径探测
def _roaming() -> Path:
    return Path(os.environ.get("APPDATA", Path.home() / "AppData/Roaming"))


def _local() -> Path:
    return Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData/Local"))


CANDIDATE_DIRS = [
    _roaming() / "Qoder",
    _roaming() / "Qoder" / "User",
    _local() / "Qoder",
    _local() / "Programs" / "Qoder",
    Path(os.environ.get("USERPROFILE", str(Path.home()))) / ".qoder",
]

MACHINE_ID_FILES = [
    "machine.id", "machineId", "machine_id", "device.json", "device.id",
    "storage.json", "telemetry.json",
]

SAFESTORE_NAMES = ["SafeStore", "SafeStore.bin", "safe_store", "safe-store.json"]


@dataclasses.dataclass(slots=True)
class ResetResult:
    machine_id: str
    telemetry_id: str
    mac_address: str | None
    volume_serial: str | None
    removed: list[str]
    dry_run: bool
    at: datetime = dataclasses.field(default_factory=datetime.now)

    def as_dict(self) -> dict:
        return dataclasses.asdict(self)


class DeviceResetter:
    def __init__(
        self,
        kill_processes: list[str] | None = None,
        reset_items: list[str] | None = None,
        app_dirs: list[str] | None = None,
        browser_cache_dirs: list[str] | None = None,
    ) -> None:
        self.kill_processes = kill_processes or ["Qoder.exe"]
        self.reset_items = set(
            reset_items or ["machine_id", "telemetry_id", "hardware_cache",
                            "mac", "volume_serial", "safestore"]
        )
        self.app_dirs = [Path(p) for p in (app_dirs or [])] or CANDIDATE_DIRS
        self.browser_cache_dirs = [Path(p) for p in (browser_cache_dirs or [])]

    # ---- 1. 进程 ----------------------------------------------------------
    def kill_client(self, dry_run: bool) -> list[str]:
        killed: list[str] = []
        for proc in self.kill_processes:
            cmd = ["taskkill", "/F", "/IM", proc, "/T"] if platform.system() == "Windows" \
                else ["pkill", "-f", proc]
            if dry_run:
                killed.append(f"[dry] {' '.join(cmd)}")
                continue
            try:
                out = subprocess.run(cmd, capture_output=True, text=True, timeout=20)
                killed.append(f"{proc}: rc={out.returncode} {out.stdout.strip()[:80]}")
            except Exception as exc:  # 进程不存在时不报错
                killed.append(f"{proc}: skip ({exc})")
        return killed

    # ---- 2/3/6. 文件标识 --------------------------------------------------
    def _iter_app_files(self):
        for d in self.app_dirs:
            if not d.exists():
                continue
            for p in d.rglob("*"):
                if p.is_file():
                    yield p

    def clear_file_identity(self, dry_run: bool) -> list[str]:
        """删 Machine ID / Telemetry 文件、硬件指纹缓存、SafeStore。"""
        removed: list[str] = []
        for p in self._iter_app_files():
            name = p.name.lower()
            hit = (
                ("machine_id" in self.reset_items and any(k in name for k in map(str.lower, MACHINE_ID_FILES)))
                or ("telemetry_id" in self.reset_items and "telemetry" in name)
                or ("safestore" in self.reset_items and p.name in SAFESTORE_NAMES)
                or ("hardware_cache" in self.reset_items and (p.suffix in {".cache", ".dat", ".bin"} and "hardware" in name))
            )
            if not hit:
                continue
            if dry_run:
                removed.append(f"[dry] delete {p}")
                continue
            try:
                p.unlink()
                removed.append(str(p))
            except Exception as exc:
                removed.append(f"FAIL {p}: {exc}")

        for cache in self.browser_cache_dirs:
            if cache.exists():
                if dry_run:
                    removed.append(f"[dry] rmtree {cache}")
                else:
                    shutil.rmtree(cache, ignore_errors=True)
                    removed.append(f"rmtree {cache}")
        return removed

    def write_new_machine_id(self, dry_run: bool) -> str:
        """生成新的 Machine ID 并写回（若原文件存在则覆盖，否则新建到主目录）。"""
        new_id = str(uuid.uuid4())
        target_dir = next((d for d in self.app_dirs if d.exists()), self.app_dirs[0])
        target = target_dir / "machine.id"
        if dry_run:
            return new_id
        try:
            target_dir.mkdir(parents=True, exist_ok=True)
            target.write_text(new_id, encoding="utf-8")
        except Exception:
            pass
        return new_id

    def write_new_telemetry_id(self, dry_run: bool) -> str:
        new_id = str(uuid.uuid4())
        target_dir = next((d for d in self.app_dirs if d.exists()), self.app_dirs[0])
        target = target_dir / "telemetry.json"
        if dry_run:
            return new_id
        try:
            target_dir.mkdir(parents=True, exist_ok=True)
            target.write_text(
                '{"telemetryId":"%s","enabled":false}' % new_id, encoding="utf-8"
            )
        except Exception:
            pass
        return new_id

    # ---- 4. MAC -----------------------------------------------------------
    def list_adapters(self) -> list[str]:
        if platform.system() != "Windows":
            return []
        try:
            out = subprocess.run(
                ["powershell", "-NoProfile", "-Command",
                 "Get-NetAdapter | Select-Object -ExpandProperty Name"],
                capture_output=True, text=True, timeout=20,
            )
            return [x.strip() for x in out.stdout.splitlines() if x.strip()]
        except Exception:
            return []

    def set_mac(self, adapter: str, mac: str, dry_run: bool) -> str:
        """通过注册表 NetworkAddress 改 MAC。"""
        if dry_run:
            return f"[dry] adapter={adapter} mac={mac}"
        ps = (
            f"Set-NetAdapterAdvancedProperty -Name '{adapter}' "
            f"-RegistryKeyword 'NetworkAddress' -RegistryValue '{mac.replace(':', '').upper()}'"
        )
        try:
            out = subprocess.run(["powershell", "-NoProfile", "-Command", ps],
                                 capture_output=True, text=True, timeout=30)
            return f"{adapter}: rc={out.returncode} {out.stderr.strip()[:120]}"
        except Exception as exc:
            return f"{adapter}: FAIL {exc}"

    @staticmethod
    def random_mac() -> str:
        """本地管理位 + 单播位，避开厂商前缀。"""
        raw = bytearray(uuid.uuid4().bytes[:6])
        raw[0] = (raw[0] | 0x02) & 0xFE
        return ":".join(f"{b:02X}" for b in raw)

    # ---- 5. 卷序列号 ------------------------------------------------------
    def set_volume_serial(self, drive: str, serial: str, dry_run: bool) -> str:
        """Windows 下 fsutil 允许对非系统卷设置卷序列号。"""
        if platform.system() != "Windows":
            return "skip: 非 Windows"
        cmd = ["fsutil", "volume", "setvolumeserialnumber", serial, drive]
        if dry_run:
            return f"[dry] {' '.join(cmd)}"
        try:
            out = subprocess.run(cmd, capture_output=True, text=True, timeout=20)
            return f"{drive}: rc={out.returncode} {out.stdout.strip()[:80]} {out.stderr.strip()[:80]}"
        except Exception as exc:
            return f"{drive}: FAIL {exc}"

    # ---- 7. 校验 ----------------------------------------------------------
    def read_machine_id(self) -> str | None:
        for d in self.app_dirs:
            f = d / "machine.id"
            if f.exists():
                try:
                    return f.read_text(encoding="utf-8").strip()
                except Exception:
                    continue
        return None

    # ---- 主流程 -----------------------------------------------------------
    def perform(
        self,
        dry_run: bool = True,
        adapter: str | None = None,
        drive: str = "E:",
    ) -> ResetResult:
        removed: list[str] = []
        removed += self.kill_client(dry_run)
        time.sleep(0.5 if not dry_run else 0)

        removed += self.clear_file_identity(dry_run)

        new_machine = self.write_new_machine_id(dry_run) \
            if "machine_id" in self.reset_items else ""
        new_telemetry = self.write_new_telemetry_id(dry_run) \
            if "telemetry_id" in self.reset_items else ""

        mac: str | None = None
        if "mac" in self.reset_items:
            target_adapter = adapter or (self.list_adapters() or ["以太网"])[0]
            mac = self.random_mac()
            removed.append(self.set_mac(target_adapter, mac, dry_run))

        vol: str | None = None
        if "volume_serial" in self.reset_items:
            vol = f"{uuid.uuid4().int % 0xFFFFFFFF:08X}"
            removed.append(self.set_volume_serial(drive, vol, dry_run))

        return ResetResult(
            machine_id=new_machine,
            telemetry_id=new_telemetry,
            mac_address=mac,
            volume_serial=vol,
            removed=removed,
            dry_run=dry_run,
        )

    def verify_reset(self, before: str | None) -> bool:
        """重置后校验：Machine ID 必须与重置前不同。"""
        after = self.read_machine_id()
        if after is None:
            return False
        return after != before
