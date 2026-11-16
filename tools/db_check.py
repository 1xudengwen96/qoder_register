"""看注册落库结果与 token 字段现状。"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.db import get_engine  # noqa: E402

import sqlite3  # noqa: E402


def main() -> int:
    db = ROOT / "data" / "qoder.db"
    print("db:", db, db.exists())
    if not db.exists():
        return 1
    con = sqlite3.connect(db)
    cur = con.cursor()
    cols = [r[1] for r in cur.execute("PRAGMA table_info(account)")]
    print("account 列:", cols)
    rows = list(cur.execute(
        "select id,email,password,username,state,credits,register_ip,"
        "length(coalesce(token_json,'')) from account"))
    for r in rows:
        print("  ", r)
    con.close()

    reg = ROOT / "data" / "registered.jsonl"
    if reg.exists():
        print("\nregistered.jsonl:")
        for line in reg.read_text(encoding="utf-8").splitlines():
            print("  ", json.dumps(json.loads(line), ensure_ascii=False)[:200])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
