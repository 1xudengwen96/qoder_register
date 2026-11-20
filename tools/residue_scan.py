"""扫描 Qoder 客户端数据目录，清点"旧账号/旧使用"残留。

重点找 43 步重置没覆盖的位置（服务端可能可见的上报面）：
- User/workspaceStorage/*/state.vscdb：工作区 SQLite（最近任务/工作区历史）
- User/globalStorage：VS Code 全局状态（含旧遥测键）
- 顶层平铺文件：登录态 / 安装标识
"""
from __future__ import annotations

import json
import sqlite3
import sys
import time
from pathlib import Path

APP = Path(r"C:\Users\Administrator\AppData\Roaming\com.qoder.app.stable")
RESET_TS = time.mktime(time.strptime("2026-11-17 15:41:40",
                                     "%Y-%m-%d %H:%M:%S"))


def fmt_ts(p: Path) -> str:
    m = p.stat().st_mtime
    tag = "旧(重置前)" if m < RESET_TS - 300 else "新(重置后)"
    return f"{time.strftime('%H:%M:%S', time.localtime(m))} {tag}"


def top_files() -> None:
    print("== 顶层平铺（登录态/设备/安装标识） ==")
    for p in sorted(APP.glob("*")):
        if p.is_file():
            print(f"  {p.name:<42} {p.stat().st_size:>9,} B  {fmt_ts(p)}")


def global_storage() -> None:
    print("\n== User/globalStorage ==")
    d = APP / "User" / "globalStorage"
    if not d.exists():
        print("  不存在")
        return
    for p in sorted(d.glob("*")):
        if p.is_dir():
            n = sum(1 for _ in p.rglob("*"))
            print(f"  [dir] {p.name:<36} {n} 项  {fmt_ts(p)}")
        else:
            print(f"  {p.name:<42} {p.stat().st_size:>9,} B  {fmt_ts(p)}")


def workspace_state() -> None:
    print("\n== User/workspaceStorage（工作区 SQLite，最近任务就在这） ==")
    d = APP / "User" / "workspaceStorage"
    if not d.exists():
        print("  不存在")
        return
    for ws in sorted(d.iterdir()):
        if not ws.is_dir():
            continue
        vsc = ws / "state.vscdb"
        meta = ws / "workspace.json"
        folder = ""
        if meta.exists():
            try:
                folder = json.loads(meta.read_text(encoding="utf-8")).get(
                    "folder", "")
            except Exception:
                pass
        if vsc.exists():
            print(f"  {ws.name[:10]}  {folder or '(无 workspace.json)'}"
                  f"  vscdb={vsc.stat().st_size:,} B  {fmt_ts(vsc)}")
        else:
            subs = [c.name for c in ws.iterdir()][:6]
            print(f"  {ws.name[:10]}  {folder or subs}  无vscdb  {fmt_ts(ws)}")


def vscdb_keys() -> None:
    print("\n== state.vscdb 键抽样（找旧账号/旧任务键） ==")
    for vsc in sorted((APP / "User" / "workspaceStorage").glob("*/state.vscdb")):
        try:
            con = sqlite3.connect(f"file:{vsc}?mode=ro", uri=True)
            rows = con.execute(
                "SELECT key FROM ItemTable").fetchall()
        except Exception as exc:
            print(f"  {vsc.parent.name[:10]}: 读失败 {exc}")
            continue
        hits = [k for k, in rows if any(
            w in k.lower() for w in (
                "task", "chat", "history", "account", "auth", "login",
                "agent", "recent", "session"))]
        print(f"  {vsc.parent.name[:10]}: 共 {len(rows)} 键，命中关键词 {len(hits)} 条")
        for k in hits[:12]:
            print(f"      {k[:110]}")
        con.close()


def leveldb_peek() -> None:
    print("\n== 顶层 Local Storage / Session Storage 存在性（identity/login 步应已删） ==")
    for name in ("Local Storage", "Session Storage", "WebStorage",
                 "SharedClientCache", "Cache", "Code Cache", "blob_storage"):
        p = APP / name
        print(f"  {name:<20} {'存在 ' + str(sum(1 for _ in p.rglob('*'))) + ' 项' if p.exists() else '不存在(已删)'}")


def main() -> int:
    if not APP.exists():
        print("数据目录不存在")
        return 1
    top_files()
    global_storage()
    workspace_state()
    vscdb_keys()
    leveldb_peek()
    return 0


if __name__ == "__main__":
    sys.exit(main())
