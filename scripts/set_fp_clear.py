"""把「启动前清理」写进 browserFingerPrint（顶层字段客户端不认），并验证效果。"""

from __future__ import annotations

import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app import secrets  # noqa: E402
from app.register import cdp_raw  # noqa: E402
from app.register.bitbrowser import BitBrowserClient  # noqa: E402


def main() -> int:
    env_id = sys.argv[1] if len(sys.argv) > 1 else secrets.get("bitbrowser.env_id")
    c = BitBrowserClient()
    if c.get_env(env_id).status == 1:
        c.close_env(env_id)
        time.sleep(3)

    env = c.get_env(env_id)
    fp = dict(env.raw.get("browserFingerPrint") or {})
    fp.update({
        "clearCookiesBeforeLaunch": True,
        "clearCacheFilesBeforeLaunch": True,
        "clearHistoriesBeforeLaunch": True,
    })
    c.update_env(env_id, browserFingerPrint=fp)

    env2 = c.get_env(env_id)
    fp2 = env2.raw.get("browserFingerPrint") or {}
    print("fp clearCookies:", fp2.get("clearCookiesBeforeLaunch"),
          "cache:", fp2.get("clearCacheFilesBeforeLaunch"),
          "history:", fp2.get("clearHistoriesBeforeLaunch"))

    # 验证：开浏览器后访问注册页，应该看到表单而不是被重定向到 /account/profile
    launch = c.open_env(env_id)
    time.sleep(6)
    pages = [t for t in cdp_raw.http_targets(launch.cdp_http)
             if t.get("type") == "page"]
    page = next((t for t in pages if "qoder" in (t.get("url") or "")),
                pages[0] if pages else None)
    if not page:
        print("没有页面")
        return 1
    with cdp_raw.CDP(launch.cdp_http, ws_url=page["webSocketDebuggerUrl"],
                     timeout=60) as cdp:
        cdp.enable()
        cdp.navigate("https://qoder.com/users/sign-up", timeout=45)
        time.sleep(4)
        url = cdp.current_url()
        has_form = bool(cdp.evaluate(
            "!!document.querySelector('#basic_firstName')"))
        cookies = cdp.call("Network.getCookies",
                           {"urls": ["https://qoder.com"]}).get("cookies") or []
        names = [x["name"] for x in cookies]
        print(f"注册页: {url}")
        print(f"有表单: {has_form}   cookie: {names}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
