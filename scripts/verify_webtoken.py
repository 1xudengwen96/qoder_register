"""验证 webtoken：从当前登录态抓会话 → 解析资料/额度 → 落库。"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import secrets  # noqa: E402
from app.register import cdp_raw  # noqa: E402
from app.register.bitbrowser import BitBrowserClient  # noqa: E402
from app.register.webtoken import save_session, snapshot_session  # noqa: E402


def main() -> int:
    c = BitBrowserClient()
    launch = c.open_env(secrets.get("bitbrowser.env_id"))
    pages = [t for t in cdp_raw.http_targets(launch.cdp_http)
             if t.get("type") == "page"]
    page = next((t for t in pages if "qoder.com" in (t.get("url") or "")),
                pages[0])
    with cdp_raw.CDP(launch.cdp_http, ws_url=page["webSocketDebuggerUrl"],
                     timeout=90) as cdp:
        cdp.enable()
        print("页面:", cdp.current_url())
        snap = snapshot_session(cdp)
    prof = snap.get("api_sample") or {}
    print(f"api_ok={snap.get('api_ok')} uid={snap.get('uid')} "
          f"cookie={len(snap.get('session_cookie') or '')}B")
    for p in (snap.get("probe") or []):
        print(f"  {p.get('status')} {p.get('path')} {(p.get('body') or '')[:80]}")
    if not snap.get("api_ok"):
        print("当前未登录/接口不可用，跳过落库")
        return 1

    # 从 /api/v1/me 拿邮箱
    import json as _json
    email = ""
    for p in snap.get("probe") or []:
        if p.get("path") == "/api/v1/me" and p.get("status") == 200:
            email = _json.loads(p["body"]).get("email", "")
    print("账号:", email)
    saved = save_session(email or "unknown@local", snap)
    print(f"落库: jsonl={saved['jsonl']} db_id={saved['db_id']}")
    print("profile:", json.dumps(saved["payload"].get("profile"),
                                 ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
