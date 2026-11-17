"""acct23 额度核对：复用注册环境（不重写代理）读 usage / profile 页。"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.register.bitbrowser import BitBrowserClient  # noqa: E402
from app.register.cdp_page import new_page  # noqa: E402
from app.claim.credit_page import (  # noqa: E402
    PROFILE_URL, USAGE_URL, parse_plan, parse_usage,
)


def main() -> int:
    client = BitBrowserClient()
    env = next((e for e in client.list_all_envs() if e.name == "acct23"), None)
    launch = client.open_env(env.id)
    page = new_page(launch.cdp_http, "about:blank")

    page.goto(USAGE_URL, wait_ms=5000)
    time.sleep(2)
    usage = parse_usage(page.text())
    page.goto(PROFILE_URL, wait_ms=4000)
    time.sleep(1)
    prof = page.text()
    usage.plan = parse_plan(prof) or ("Pro Trial" if "Pro Trial" in prof else "")

    out = {"credits": usage.as_dict(), "plan": usage.plan,
           "source": usage.source}
    print(json.dumps(out, ensure_ascii=False, indent=2))
    (ROOT / "data" / "acct23_credits.json").write_text(
        json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
