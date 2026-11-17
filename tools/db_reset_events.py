"""查 acct23 设备重置段的 event 详情（errors 前 10 条）+ DeviceProfile 记录。"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.db import init_engine, session_scope, create_all  # noqa: E402
from app.models import Account, DeviceProfile, EventLog  # noqa: E402

init_engine("sqlite:///data/qoder.db")
create_all()

with session_scope() as s:
    acc = s.query(Account).filter(
        Account.email == "bumpier-eves-0m@icloud.com").first()
    print(f"acct id={acc.id} state={acc.state} device_profile_id={acc.device_profile_id}")
    prof = (s.query(DeviceProfile).filter(
        DeviceProfile.account_id == acc.id).order_by(DeviceProfile.id.desc())
        .first())
    if prof:
        print(f"DeviceProfile: machine_id={prof.machine_id} "
              f"telemetry={prof.telemetry_id} claim_count={prof.claim_count} "
              f"reset_at={prof.reset_at}")
    else:
        print("DeviceProfile: 无记录（设备重置段可能没建）")
    evs = (s.query(EventLog).filter(EventLog.account_id == acc.id)
           .order_by(EventLog.id.asc()).all())
    for e in evs:
        print(f"event[{e.id}] stage={e.stage} level={e.level} "
              f"msg={e.message}")
        if e.stage in ("reset", "claim") and e.payload:
            print(f"        payload={e.payload[:700]}")
