"""验证 Qoder 机器身份是否可改造。

背景（来自 docs/FINDINGS-2026-11-09.md）：
  C:\\Users\\<用户>\\.qoder\\.bin\\umid-win32-x64-<hash>\\runtime-info.exe
      官方机器身份上报程序，纯本地计算，无网络请求
      输出：machineToken / machineType / machineCode / vmInfo

  C:\\Users\\<用户>\\.qoder\\.auth\\
      machine_id   36 字节 UUID  ← 关键
      id           36 字节 UUID

  已验证：machine_id 创建时间 = runtime-info.exe 首次运行时间 + 1 秒
  → machine_id 是该程序首次运行时生成的随机 UUID

本脚本验证：改 machine_id 能不能让 machineToken 变化
  → 能变 = 可造任意多套身份 = 一台机器领多份
  → 不能变 = machineToken 由硬件派生，改文件无效

用法：
    python tools/verify_machine_identity.py            # 只读检查
    python tools/verify_machine_identity.py --mutate   # 做改造实验（会自动备份回滚）
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import time
import uuid
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

QODER_HOME = Path.home() / ".qoder"
AUTH_DIR = QODER_HOME / ".auth"
MACHINE_ID_FILE = AUTH_DIR / "machine_id"
ACCOUNT_ID_FILE = AUTH_DIR / "id"


def find_runtime_info() -> Path | None:
    base = QODER_HOME / ".bin"
    if not base.exists():
        return None
    for p in base.rglob("runtime-info.exe"):
        return p
    return None


def run_runtime_info(exe: Path, timeout: int = 30) -> dict | None:
    try:
        r = subprocess.run([str(exe)], capture_output=True, text=True,
                           timeout=timeout, cwd=str(exe.parent))
    except Exception as exc:
        print(f"  运行失败：{exc}")
        return None
    out = (r.stdout or "").strip()
    if not out:
        print(f"  无输出。stderr={r.stderr[:200]}")
        return None
    try:
        return json.loads(out)
    except Exception:
        print(f"  非 JSON 输出：{out[:300]}")
        return None


def read_text(p: Path) -> str:
    try:
        return p.read_text(encoding="utf-8").strip()
    except Exception:
        return ""


def describe(info: dict) -> None:
    vm = info.get("vmInfo") or {}
    print(f"    machineToken : {info.get('machineToken', '')[:40]}...")
    print(f"    machineType  : {info.get('machineType')}")
    print(f"    machineCode  : {info.get('machineCode')}")
    print(f"    isVm         : {vm.get('isVm')}  brand={vm.get('brand')} "
          f"percentage={vm.get('percentage')} vmTypeCode={vm.get('vmTypeCode')}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--mutate", action="store_true", help="做改造实验")
    ap.add_argument("--keep", action="store_true", help="实验后不恢复")
    args = ap.parse_args()

    checks: list[tuple[str, bool, str]] = []

    print("=== 1. 定位官方组件 ===")
    exe = find_runtime_info()
    checks.append((".bin/runtime-info.exe 存在", exe is not None, str(exe)))
    if exe is None:
        print("找不到 runtime-info.exe")
        return 2
    print(f"  exe  : {exe}")
    print(f"  大小 : {exe.stat().st_size} bytes")

    checks.append((".auth/machine_id 存在", MACHINE_ID_FILE.exists(),
                   str(MACHINE_ID_FILE)))
    checks.append((".auth/id 存在", ACCOUNT_ID_FILE.exists(), str(ACCOUNT_ID_FILE)))

    mid_before = read_text(MACHINE_ID_FILE)
    aid = read_text(ACCOUNT_ID_FILE)
    print(f"  machine_id : {mid_before}")
    print(f"  account id : {aid}")

    # ---- 基线 ----
    print("\n=== 2. 基线指纹 ===")
    base = run_runtime_info(exe)
    if base is None:
        return 2
    describe(base)
    checks.append(("能读到 machineToken", bool(base.get("machineToken")), ""))

    is_vm = bool((base.get("vmInfo") or {}).get("isVm"))
    checks.append(("当前不是虚拟机", not is_vm,
                   f"isVm={is_vm} brand={(base.get('vmInfo') or {}).get('brand')}"))

    # ---- 重复运行是否稳定 ----
    print("\n=== 3. 重复运行稳定性 ===")
    again = run_runtime_info(exe)
    stable = again is not None and again.get("machineToken") == base.get("machineToken")
    checks.append(("同一 machine_id 输出稳定", stable,
                   "两次 token 相同" if stable else "两次 token 不同"))
    print(f"    第二次 machineToken: {str(again.get('machineToken',''))[:40] if again else 'N/A'}...")

    # ---- 改造实验 ----
    if args.mutate:
        print("\n=== 4. 改造实验：改 machine_id 后 token 是否变化 ===")

        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        backup_dir = ROOT / "data" / "machine_identity_backup"
        backup_dir.mkdir(parents=True, exist_ok=True)
        backup = backup_dir / f"machine_id.{stamp}.bak"
        shutil.copy2(MACHINE_ID_FILE, backup)
        print(f"    已备份 → {backup}")

        new_mid = str(uuid.uuid4())
        try:
            MACHINE_ID_FILE.write_text(new_mid, encoding="utf-8")
            print(f"    新 machine_id: {new_mid}")
        except Exception as exc:
            print(f"    写入失败：{exc}")
            return 2

        time.sleep(0.5)
        mutated = run_runtime_info(exe)
        if mutated is None:
            print("    改造后读取失败，回滚")
            shutil.copy2(backup, MACHINE_ID_FILE)
            return 2

        print("    改造后：")
        describe(mutated)

        changed_token = mutated.get("machineToken") != base.get("machineToken")
        changed_type = mutated.get("machineType") != base.get("machineType")
        changed_code = mutated.get("machineCode") != base.get("machineCode")

        checks.append(("machineToken 随 machine_id 变化", changed_token,
                       "变了=可造多套身份" if changed_token else "没变=由硬件派生"))
        checks.append(("machineType 变化", changed_type, ""))
        checks.append(("machineCode 变化", changed_code, ""))

        # 回滚
        if not args.keep:
            shutil.copy2(backup, MACHINE_ID_FILE)
            time.sleep(0.3)
            restored = run_runtime_info(exe)
            ok_back = restored is not None and \
                restored.get("machineToken") == base.get("machineToken")
            checks.append(("回滚后 token 恢复原值", ok_back, ""))
            print(f"\n    已回滚 machine_id → {mid_before}")
            if restored:
                print(f"    回滚后 machineToken: {str(restored.get('machineToken',''))[:40]}...")
        else:
            print(f"\n    保留新 machine_id（--keep）: {new_mid}")

    # ---- VM 判定说明 ----
    print("\n=== 5. VM 判定 ===")
    vm = base.get("vmInfo") or {}
    if vm.get("isVm"):
        print(f"    ⚠ 当前被判为虚拟机：brand={vm.get('brand')} "
              f"percentage={vm.get('percentage')}")
        print("    官方规则：虚拟机不发放新人积分，必须先解决")
        print("    原因通常是 hypervisorlaunchtype=Auto（WSL2 遗留）")
        print("    处理：bcdedit /set hypervisorlaunchtype off 然后重启")
    else:
        print("    ✓ 未判虚拟机，可以领积分")

    # ---- 汇总 ----
    print("\n=== 结果 ===")
    passed = 0
    width = max(len(x[0]) for x in checks) + 2
    for name, ok, detail in checks:
        print(f"[{'PASS' if ok else 'FAIL'}] {name:<{width}} {detail}")
        passed += ok
    print(f"\n{passed}/{len(checks)} 通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
