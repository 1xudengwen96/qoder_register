"""批量领取已 verified 账号：串行 设备重置 → 客户端自动登录 → 抓 token → 核额度。

用法：
    $env:PYTHONPATH='.;.pylibs'
    python tools/claim_batch.py            # 领全部待领
    python tools/claim_batch.py --limit 1  # 先领 1 个验证链路
    python tools/claim_batch.py --ids 62,63
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / ".pylibs"))

from app import secrets  # noqa: E402
from app.config import get, load_config  # noqa: E402
from app.db import init_engine, session_scope  # noqa: E402
from app.models import Account, AccountState  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--ids", default="")
    args = ap.parse_args()

    secrets.export_env()
    cfg = load_config()
    init_engine(get(cfg, "storage.url", "sqlite:///data/qoder.db"))

    with session_scope() as s:
        q = s.query(Account).filter(Account.state == AccountState.VERIFIED)
        if args.ids:
            want = {int(x) for x in args.ids.split(",") if x.strip()}
            q = q.filter(Account.id.in_(want))
        rows = q.order_by(Account.id.asc()).all()
        ids = [(a.id, a.email) for a in rows]
    if args.limit:
        ids = ids[:args.limit]
    print(f"待领 {len(ids)} 个: {[i for i, _ in ids]}", flush=True)
    if not ids:
        return 0

    from app.orchestrator import Pipeline
    pipe = Pipeline(cfg)
    ok_n = 0
    try:
        for aid, email in ids:
            print(f"\n===== 领取 #{aid} {email} =====", flush=True)
            t0 = time.time()
            rep = pipe.claim_only(aid)
            print(f"结果: ok={rep.ok} stage={rep.stage} "
                  f"credits={rep.credits} msg={rep.message} "
                  f"({time.time() - t0:.0f}s)", flush=True)
            ok_n += 1 if rep.ok else 0
    finally:
        pipe.shutdown_browsers()
    print(f"\n领取完成: 成功 {ok_n}/{len(ids)}", flush=True)
    return 0 if ok_n == len(ids) else 2


if __name__ == "__main__":
    sys.exit(main())