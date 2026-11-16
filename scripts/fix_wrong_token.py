"""纠正：把误挂到新账号上的旧 token 清掉，状态回退到 REGISTERED。"""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DB = ROOT / "data" / "qoder.db"
MT = Path.home() / "AppData/Roaming/Qoder/SharedClientCache/cache/machine_token.json"


def main() -> int:
    con = sqlite3.connect(DB)
    cur = con.cursor()
    rows = list(cur.execute("select id,email,state,token_json from account"))
    import json

    local_tok = ""
    if MT.exists():
        local_tok = json.loads(MT.read_text(encoding="utf-8")).get("token", "")
    print(f"本地 machine_token 前 16 位: {local_tok[:16]}")

    for aid, email, state, tok in rows:
        if not tok:
            print(f"  {aid} {email} state={state} token=(空)")
            continue
        try:
            tv = json.loads(tok)
            inner = tv.get("token", "") if isinstance(tv, dict) else str(tv)
        except Exception:
            inner = tok
        same = bool(local_tok) and inner[:16] == local_tok[:16]
        print(f"  {aid} {email} state={state} token 前 16={inner[:16]} "
              f"与本地同源={same}")
        if same:
            cur.execute(
                "update account set token_json=NULL, state='REGISTERED', "
                "claimed_at=NULL, credits=0 where id=?", (aid,))
            print(f"    → 已清空误挂 token，状态回 REGISTERED")
    con.commit()
    for r in cur.execute("select id,email,state,length(coalesce(token_json,''))"
                         " from account"):
        print("  最终:", r)
    con.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
