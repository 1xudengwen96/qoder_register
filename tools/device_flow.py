"""设备登录一条龙：客户端复制链接 → 浏览器登录 → 立刻 Continue → 抓 token 落库。

挑战有效期很短（实测放十几分钟就「登录等待已超时」），所以必须一口气做完。

    python tools/device_flow.py --email medley.address_0m@icloud.com
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import secrets  # noqa: E402
from app.config import load_config  # noqa: E402
from app.net.proxy import parse_line  # noqa: E402
from app.register import cdp_raw  # noqa: E402
from app.register.bitbrowser import BitBrowserClient  # noqa: E402
from app.register.cdp_page import CDPPage  # noqa: E402


def clipboard() -> str:
    p = subprocess.run(["powershell", "-NoProfile", "-Command",
                        "Get-Clipboard -Raw"], capture_output=True, text=True,
                       timeout=15, encoding="utf-8", errors="replace")
    return (p.stdout or "").strip()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--email", default="")
    ap.add_argument("--password", default="")
    ap.add_argument("--account", default="0")
    ap.add_argument("--login-timeout", type=int, default=150)
    args = ap.parse_args()

    email, password = args.email, args.password
    if not email:
        first = (ROOT / "data" / "accounts.txt").read_text(encoding="utf-8").splitlines()[0]
        email, password = first.split("----")[:2]
    link = clipboard()
    if "qoder.com" not in link:
        print("剪贴板没有登录链接，先在客户端点「立即登录」→「复制登录链接」")
        return 2
    print(f"账号 {email}\n链接 {link[:100]}…")

    client = BitBrowserClient()
    env = client.get_env(secrets.get("bitbrowser.env_id"))
    proxy_raw = (f"http://{env.proxy_user}:{env.proxy_password}@"
                 f"{env.host}:{env.port}") if env.host else ""
    px = parse_line(proxy_raw) if proxy_raw else None

    from app.claim import ui_login

    t0 = time.time()
    out = ui_login.drive_login(
        load_config(), link, email, password,
        env_name=env.name, region="", timeout=args.login_timeout,
        artifacts_dir=str(ROOT / "data" / "artifacts"), proxy=px,
        close_when_done=False)
    print(f"drive_login {time.time()-t0:.0f}s ->",
          json.dumps(out.as_dict(), ensure_ascii=False)[:300])

    final = str(getattr(out, "final_url", "") or "")
    if "selectAccounts" not in final:
        print("没到 selectAccounts，停在:", final[:140])
        return 1

    # 立刻点 Continue
    launch = client.open_env(env.id)
    pages = [t for t in cdp_raw.http_targets(launch.cdp_http)
             if t.get("type") == "page"]
    page = next((p for p in pages if "selectAccounts" in (p.get("url") or "")), None)
    if not page:
        print("找不到 selectAccounts 页面")
        return 1
    with cdp_raw.CDP(launch.cdp_http, ws_url=page["webSocketDebuggerUrl"],
                     timeout=60) as raw:
        raw.enable()
        pg = CDPPage.__new__(CDPPage)
        pg.cdp = raw
        pg.default_timeout = 60
        print("点 Continue:", pg.click_text("Continue", tag="button"))
        time.sleep(5)
        print("之后 URL:", pg.url()[:120])
        print("文本:", pg.text()[:160].replace("\n", " | "))

    time.sleep(6)
    from app.claim.token_harvester import harvest_token

    tok = harvest_token()
    if not tok:
        time.sleep(6)
        tok = harvest_token()
    if not tok:
        print("客户端还没落 token")
        return 1

    print(f"PASS 客户端 token: {tok.token[:16]}… source={tok.source}")
    from tools.device_login import save_client_token  # type: ignore

    save_client_token(email, tok)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
