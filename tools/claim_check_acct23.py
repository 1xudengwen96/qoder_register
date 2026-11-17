"""acct23 领取收尾实测：采 token + 核对额度。"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.claim.claimer import Claimer  # noqa: E402
from app.claim.token_harvester import qoder_data_dir  # noqa: E402


def main() -> int:
    data_dir = qoder_data_dir()
    print(f"data_dir={data_dir}")
    mt = data_dir / "SharedClientCache" / "cache" / "machine_token.json"
    auth = data_dir / "auth.v1.dat"
    print(f"machine_token.json exists={mt.exists()} "
          f"mtime={mt.stat().st_mtime if mt.exists() else '-'}")
    print(f"auth.v1.dat exists={auth.exists()} "
          f"mtime={auth.stat().st_mtime if auth.exists() else '-'}")

    c = Claimer()
    tok = c.harvester.harvest()
    if not tok:
        print("harvest 未拿到 token")
        return 1
    print(f"token source={tok.source} len={len(tok.token)}")
    out = {"token_source": tok.source, "token_len": len(tok.token),
           "token_prefix": tok.token[:24]}
    credits, plan, expire = c.probe_credits(tok)
    out.update({"credits": credits, "plan": plan, "expire": str(expire)})
    print(json.dumps(out, ensure_ascii=False, indent=2))
    (ROOT / "data" / "acct23_token.json").write_text(
        json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
