"""0.4.x 平铺布局的"安装身份"清理（qoder_reset 43 步没覆盖的部分）。

目标：让服务端把这台客户端当成"全新安装"——
- first-launch-onboarding.v1.json：客户端身份卡（serialNumber 已 claim，老安装铁证）
- .updaterId：安装级稳定 UUID
- main.sqlite*：任务/会话/账号历史主库（12.7MB）
- chat-session-turn-payload-buffer.sqlite*：历史对话 payload
- remote-control.sqlite* / collaboration-message-cache.sqlite* /
  sessionMigration.sqlite / memoryMigration.sqlite：旧状态库
- qoder-data.v1.ext.json / auth-profile-overlays.v1.dat：旧数据文件
- updateNotificationSeen.v1.json：旧通知状态

动作前先备份到 data/device_backup/<时间戳>/。
"""
from __future__ import annotations

import shutil
import subprocess
import sys
import time
from pathlib import Path

APP = Path(r"C:\Users\Administrator\AppData\Roaming\com.qoder.app.stable")
BACKUP = Path(r"d:\qoder_register\data\device_backup")

# 文件级清理（glob 模式，删主文件 + -shm/-wal）
FILES = [
    "first-launch-onboarding.v1.json",
    ".updaterId",
    "main.sqlite",
    "main.sqlite-shm",
    "main.sqlite-wal",
    "chat-session-turn-payload-buffer.sqlite",
    "chat-session-turn-payload-buffer.sqlite-shm",
    "chat-session-turn-payload-buffer.sqlite-wal",
    "remote-control.sqlite",
    "remote-control.sqlite-shm",
    "remote-control.sqlite-wal",
    "collaboration-message-cache.sqlite",
    "collaboration-message-cache.sqlite-shm",
    "collaboration-message-cache.sqlite-wal",
    "sessionMigration.sqlite",
    "memoryMigration.sqlite",
    "qoder-data.v1.ext.json",
    "auth-profile-overlays.v1.dat",
    "updateNotificationSeen.v1.json",
    "DIPS",
    "DIPS-wal",
    # 登录态也清（重登录走全流程，触发首登逻辑）
    "auth.v1.dat",
]


def main() -> int:
    # 1. 杀客户端（不清缓存一致性没保证）
    subprocess.run(["taskkill", "/F", "/IM", "Qoder.exe", "/T"],
                   capture_output=True)
    time.sleep(4)
    if subprocess.run(["tasklist", "/FI", "IMAGENAME eq Qoder.exe"],
                      capture_output=True).stdout:
        print("Qoder 还在跑，中止")
        return 1

    # 2. 备份 + 删除
    stamp = time.strftime("%Y%m%d_%H%M%S")
    bdir = BACKUP / f"{stamp}_install"
    bdir.mkdir(parents=True, exist_ok=True)
    removed, missing = [], []
    for name in FILES:
        p = APP / name
        if not p.exists():
            missing.append(name)
            continue
        try:
            shutil.copy2(p, bdir / name)
        except Exception as exc:
            print(f"备份失败 {name}: {exc}")
            return 1
        try:
            p.unlink()
            removed.append(name)
        except Exception as exc:
            print(f"删除失败 {name}: {exc}")
            return 1

    print(f"备份 → {bdir}")
    print(f"已删 {len(removed)} 项: {', '.join(removed)}")
    if missing:
        print(f"本来就没有: {', '.join(missing)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
