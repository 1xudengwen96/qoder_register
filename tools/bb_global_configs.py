"""看比特浏览器客户端的全局配置里有没有「启动时打开工作台」之类的开关。"""

from __future__ import annotations

import json
import shutil
import sqlite3
import tempfile
from pathlib import Path

SRC = Path.home() / "AppData/Roaming/BitBrowser/db.sqlite"


def main() -> int:
    if not SRC.exists():
        print(f"没有 {SRC}")
        return 1
    tmp = Path(tempfile.gettempdir()) / "bb_global.sqlite"
    shutil.copy2(SRC, tmp)
    con = sqlite3.connect(tmp)
    cur = con.cursor()
    cols = [r[1] for r in cur.execute("PRAGMA table_info(global_configs)")]
    print("global_configs 列:", cols)
    for row in cur.execute("SELECT * FROM global_configs"):
        rec = dict(zip(cols, row))
        blob = json.dumps(rec, ensure_ascii=False, default=str)
        print("  ", blob[:300])
    con.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
