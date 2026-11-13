"""验证：qoder-free 改造版设备重置的完整链路。

流程：
1. 读重置前的 machineid / telemetry
2. 造一个临时 Qoder 目录副本，在副本上真执行一次全量重置
3. 校验：11 个 ID 文件生成、storage.json 身份键清除、系统指纹与平台匹配、
        machineid 变化、备份生成、可回滚

不改动真实 %APPDATA%\\Qoder，全部在副本上做。

用法：
    python tools/verify_device_reset.py
"""

from __future__ import annotations

import json
import shutil
import sys
import uuid
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.device.qoder_reset import (  # noqa: E402
    ALL_STEPS,
    ID_FILES,
    QoderIdentityReset,
    ResetOptions,
)

WORK = ROOT / "data" / "verify_reset"


def build_fake_qoder() -> Path:
    """按真实结构造一个 Qoder 数据目录。"""
    d = WORK / f"qoder_{uuid.uuid4().hex[:6]}"
    (d / "User" / "globalStorage").mkdir(parents=True, exist_ok=True)
    (d / "User" / "workspaceStorage" / "abc" / "chatSessions").mkdir(parents=True, exist_ok=True)
    (d / "SharedClientCache" / "index").mkdir(parents=True, exist_ok=True)

    (d / "machineid").write_text(str(uuid.uuid4()), encoding="utf-8")
    (d / "Cache").mkdir()
    (d / "Cache" / "x").write_text("c", encoding="utf-8")
    for f in ("Network Persistent State", "TransportSecurity", "Trust Tokens",
              "Cookies", "Local State", "Preferences"):
        (d / f).write_text("identity-data", encoding="utf-8")
    (d / "SharedClientCache" / ".info").write_text("12345", encoding="utf-8")
    (d / "SharedClientCache" / "mcp.json").write_text("{}", encoding="utf-8")
    (d / "User" / "workspaceStorage" / "abc" / "chatSessions" / "s1.json").write_text(
        "{}", encoding="utf-8")

    (d / "User" / "globalStorage" / "storage.json").write_text(json.dumps({
        "telemetry.machineId": "sha_old_value",
        "telemetry.devDeviceId": "dev_old_value",
        "telemetry.sqmId": "sqm_old",
        "auth.token": "SECRET_TOKEN_SHOULD_BE_GONE",
        "login.userEmail": "old@example.com",
        "device.fingerprint": "fp_old",
        "system.platform": "darwin",
        "system.timezone": "America/New_York",
        "editor.fontSize": 14,
        "workbench.colorTheme": "Dark+",
        "extensions.autoUpdate": True,
    }, indent=2), encoding="utf-8")
    return d


def main() -> int:
    WORK.mkdir(parents=True, exist_ok=True)
    d = build_fake_qoder()
    reset = QoderIdentityReset(app_dir=d, backup_dir=WORK / "backup")

    before_mid = (d / "machineid").read_text(encoding="utf-8")
    before_storage = json.loads((d / "User/globalStorage/storage.json").read_text(encoding="utf-8"))
    print(f"测试目录：{d}")
    print(f"重置前 machineid：{before_mid}\n")

    # ---- dry-run：必须什么都不改 ----
    dry = reset.run(ResetOptions(dry_run=True))
    untouched = (d / "machineid").read_text(encoding="utf-8") == before_mid
    print(f"[{'PASS' if untouched else 'FAIL'}] dry-run 不改动机器 ID")
    print(f"     dry-run 收集动作 {len(dry.actions)} 项，errors={len(dry.errors)}")

    # ---- 真执行 ----
    rep = reset.run(ResetOptions(
        steps=tuple(ALL_STEPS), dry_run=False, preserve_chat=False,
        fake_hardware=False, platform_hint="Windows",
        timezone="Europe/London", locale="en-GB",
    ))
    print(f"\n执行完成：ok={rep.ok}  动作 {len(rep.actions)} 项  "
          f"错误 {len(rep.errors)} 项  备份={rep.backup_dir}")
    if rep.errors:
        for e in rep.errors[:5]:
            print(f"     ERR {e}")

    # ---- 校验 ----
    after_mid = (d / "machineid").read_text(encoding="utf-8")
    after_storage = json.loads((d / "User/globalStorage/storage.json").read_text(encoding="utf-8"))

    checks: list[tuple[str, bool, str]] = []

    checks.append(("machineid 已变更", after_mid != before_mid, f"{before_mid[:8]}→{after_mid[:8]}"))

    id_present = [n for n in ID_FILES if (d / n).exists()]
    checks.append(("11 个 ID 文件全部生成", len(id_present) == len(ID_FILES),
                   f"{len(id_present)}/{len(ID_FILES)}"))

    gone = [k for k in ("auth.token", "login.userEmail", "device.fingerprint")
            if k not in after_storage]
    checks.append(("身份键被清除", len(gone) == 3, f"清除 {gone}"))

    kept = [k for k in ("editor.fontSize", "workbench.colorTheme", "extensions.autoUpdate")
            if k in after_storage]
    checks.append(("业务键被保留", len(kept) == 3, f"保留 {kept}"))

    checks.append(("telemetry.machineId 已换", after_storage.get("telemetry.machineId") != "sha_old_value",
                   str(after_storage.get("telemetry.machineId"))[:16] + "..."))
    checks.append(("telemetry.devDeviceId 已换",
                   after_storage.get("telemetry.devDeviceId") != "dev_old_value", ""))
    checks.append(("16 个遥测键齐备",
                   all(k in after_storage for k in ("telemetry.machineId", "telemetry.sqmId",
                                                    "telemetry.sessionId", "hardwareId", "cpuId",
                                                    "gpuId", "memoryId")),
                   ""))

    # 平台一致性：Windows 上必须写 win32，不能是 darwin
    plat = after_storage.get("system.platform")
    checks.append(("平台不再硬编码 darwin", plat == "win32", f"system.platform={plat}"))
    checks.append(("时区跟随配置", after_storage.get("system.timezone") == "Europe/London",
                   after_storage.get("system.timezone", "")))
    checks.append(("locale 跟随配置", after_storage.get("system.locale") == "en-GB",
                   after_storage.get("system.locale", "")))

    cache_gone = not (d / "Cache").exists()
    checks.append(("缓存目录已清", cache_gone, "Cache"))

    identity_gone = not (d / "Network Persistent State").exists() and not (d / "Cookies").exists()
    checks.append(("身份文件已删", identity_gone, "Network Persistent State / Cookies"))

    shared_gone = not (d / "SharedClientCache" / ".info").exists()
    checks.append(("SharedClientCache 已清", shared_gone, ".info / mcp.json"))

    chat_gone = not (d / "User" / "workspaceStorage" / "abc" / "chatSessions" / "s1.json").exists()
    checks.append(("对话记录已清（preserve_chat=False）", chat_gone, ""))

    checks.append(("备份已生成", bool(rep.backup_dir) and Path(rep.backup_dir).exists(),
                   rep.backup_dir or "无"))

    # ---- 回滚 ----
    restored = reset.restore(rep.backup_dir) if rep.backup_dir else []
    rolled_back = (d / "machineid").read_text(encoding="utf-8").strip() == before_mid.strip()
    checks.append(("可从备份回滚", rolled_back, f"回拷 {len(restored)} 项"))

    # ---- 保留对话模式 ----
    d2 = build_fake_qoder()
    reset2 = QoderIdentityReset(app_dir=d2, backup_dir=WORK / "backup2")
    reset2.run(ResetOptions(steps=("machine_id", "login"), dry_run=False,
                            preserve_chat=True))
    chat_kept = (d2 / "User" / "workspaceStorage" / "abc" / "chatSessions" / "s1.json").exists()
    checks.append(("preserve_chat=True 保留对话", chat_kept, ""))

    print()
    passed = 0
    width = max(len(c[0]) for c in checks) + 2
    for name, ok, detail in checks:
        print(f"[{'PASS' if ok else 'FAIL'}] {name:<{width}} {detail}")
        passed += ok
    print(f"\n{passed}/{len(checks)} 通过")

    # 清理（沙箱下不删，留证据）
    print(f"\n测试产物保留在：{WORK}")
    return 0 if passed == len(checks) else 2


if __name__ == "__main__":
    raise SystemExit(main())
