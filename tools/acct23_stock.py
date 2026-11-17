"""acct23 领取成功后入库：state -> STOCK，记录 token/credits。"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.config import load_config  # noqa: E402
from app.db import init_engine, session_scope, create_all  # noqa: E402
from app.models import Account, AccountState  # noqa: E402
from app.claim.claimer import Claimer  # noqa: E402


def main() -> int:
    cfg = load_config(ROOT / "config" / "config.yaml")
    init_engine("sqlite:///data/qoder.db")
    create_all()

    tok = Claimer().harvester.harvest()
    print(f"token source={tok.source} prefix={tok.token[:20]}")

    with session_scope() as s:
        acc = s.query(Account).filter(
            Account.email == "bumpier-eves-0m@icloud.com").first()
        if acc is None:
            print("没找到 acct23")
            return 1
        acc.state = AccountState.STOCK
        acc.credits = 100
        acc.plan = "Free"
        if tok:
            acc.token_json = json.dumps(tok.as_dict(), ensure_ascii=False)
        print(f"acct23 id={acc.id} -> STOCK credits=100 plan=Free")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
