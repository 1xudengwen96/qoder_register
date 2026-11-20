"""读 main.sqlite / auth-profile-overlays / .updaterId 等 0.4.x 残留，
确认旧账号标识（email/token/旧 machine_id）具体残留在哪。"""
from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

APP = Path(r"C:\Users\Administrator\AppData\Roaming\com.qoder.app.stable")
OUT = Path(__file__).resolve().parents[1] / "data" / "residue_deep.txt"
lines: list[str] = []


def w(s: str) -> None:
    lines.append(s)


def dump_small_files() -> None:
    w("== 小残留文件 ==")
    for name in (".updaterId", "auth-profile-overlays.v1.dat",
                 "updateNotificationSeen.v1.json",
                 "first-launch-onboarding.v1.json",
                 "windows-startup-compatibility.json",
                 "application-run-state.v1"):
        p = APP / name
        if not p.exists():
            continue
        try:
            raw = p.read_bytes()
            text = raw.decode("utf-8", "replace")
        except Exception as exc:
            text = f"读失败 {exc}"
        w(f"--- {name} ({len(raw)} B) ---")
        w(text[:600].replace("\n", " | "))
        w("")


def sqlite_tables(path: Path, label: str) -> list[str]:
    if not path.exists():
        w(f"{label}: 不存在")
        return []
    try:
        con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    except Exception as exc:
        w(f"{label}: 打不开 {exc}")
        return []
    try:
        rows = con.execute(
            "SELECT name FROM sqlite_master WHERE type='table'").fetchall()
    except Exception as exc:
        w(f"{label}: 表读失败 {exc}")
        con.close()
        return []
    names = [n for n, in rows]
    w(f"{label}: 表 {names}")
    con.close()
    return names


def scan_text_blob(path: Path, label: str, needles: list[str],
                   limit: int = 40) -> None:
    """在库里找含旧账号标识的行。"""
    if not path.exists():
        w(f"{label}: 不存在")
        return
    con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        tables = [n for n, in con.execute(
            "SELECT name FROM sqlite_master WHERE type='table'").fetchall()]
        for t in tables:
            try:
                cols = [c[1] for c in con.execute(
                    f'PRAGMA table_info("{t}")').fetchall()]
                cursor = con.execute(f'SELECT * FROM "{t}"')
                hits = 0
                for row in cursor:
                    blob = json.dumps(
                        [x if not isinstance(x, bytes) else x.decode(
                            "utf-8", "replace")[:200] for x in row],
                        ensure_ascii=False, default=str)
                    if any(n in blob for n in needles):
                        hits += 1
                        if hits <= 3:
                            w(f"  [{label}.{t}] 命中: {blob[:240]}")
                if hits:
                    w(f"  [{label}.{t}] 命中 {hits} 行 (cols={cols[:8]})")
            except Exception:
                continue
    finally:
        con.close()


def main() -> int:
    needles = [
        "bumpier-eves-0m",            # acct23（本账号，不算旧）
        "@icloud.com", "icloud.com",
        "dt-",                        # token 前缀
        "machine", "machineId",
        "55588b84",                   # 15:41 重置前的旧 machineid（注册前更老的）
    ]
    dump_small_files()
    w("\n== main.sqlite 12.7MB（主库：任务/会话/账号历史） ==")
    dump_small_files()  # placeholder 避免 double
    lines.pop()
    scan_text_blob(APP / "main.sqlite", "main.sqlite", needles)
    w("\n== chat-session-turn-payload-buffer.sqlite ==")
    scan_text_blob(APP / "chat-session-turn-payload-buffer.sqlite",
                   "chat-buffer", needles)
    w("\n== sessionMigration.sqlite（昨晚旧） ==")
    scan_text_blob(APP / "sessionMigration.sqlite", "sessionMig", needles)
    w("\n== qoder-data.v1.ext.json（30KB 旧） ==")
    p = APP / "qoder-data.v1.ext.json"
    if p.exists():
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
            w(f"顶层键: {list(data)[:20]}")
            blob = json.dumps(data, ensure_ascii=False)
            for n in needles:
                if n in blob:
                    w(f"  含 {n}")
        except Exception as exc:
            w(f"读失败 {exc}")
    OUT.write_text("\n".join(lines), encoding="utf-8")
    print(f"写入 {OUT} 共 {len(lines)} 行")
    return 0


if __name__ == "__main__":
    sys.exit(main())
