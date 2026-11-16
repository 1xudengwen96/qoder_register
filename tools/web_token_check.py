"""用浏览器里的 qoder_session_cookie 去调 API，验证这个 token 能不能用。"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import secrets  # noqa: E402
from app.net.http import no_env_session  # noqa: E402
from app.register import cdp_raw  # noqa: E402
from app.register.bitbrowser import BitBrowserClient  # noqa: E402

PATHS = (
    "/api/v1/users/me",
    "/api/v1/user/info",
    "/api/v1/users/profile",
    "/api/v1/credits",
    "/api/v1/user/credits",
    "/api/v1/plan",
)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--env-id", default=secrets.get("bitbrowser.env_id", ""))
    ap.add_argument("--proxy", default="", help="可选代理，如 http://127.0.0.1:7897")
    ap.add_argument("--use-env-proxy", action="store_true",
                    help="直接用比特环境里的代理（和浏览器同出口）")
    args = ap.parse_args()

    client = BitBrowserClient()
    endpoint = client.open_env(args.env_id).cdp_http
    pages = [t for t in cdp_raw.http_targets(endpoint) if t.get("type") == "page"]
    page = next((t for t in pages if "qoder.com" in (t.get("url") or "")), pages[0])
    with cdp_raw.CDP(endpoint, ws_url=page["webSocketDebuggerUrl"]) as cdp:
        cdp.enable()
        res = cdp.call("Network.getCookies",
                       {"urls": ["https://qoder.com", "https://www.qoder.com"]})
        cookies = {c["name"]: c["value"] for c in res.get("cookies") or []}
    if "qoder_session_cookie" not in cookies:
        print("没有 qoder_session_cookie")
        return 1
    print("拿到会话 cookie，长度:", len(cookies["qoder_session_cookie"]))
    print("qoderuid:", cookies.get("qoderuid"))

    cookie_hdr = "; ".join(f"{k}={v}" for k, v in cookies.items())
    proxies = None
    if args.proxy:
        proxies = {"http": args.proxy, "https": args.proxy}

    with no_env_session() as s:
        for path in PATHS:
            url = "https://qoder.com" + path
            try:
                r = s.get(url, headers={
                    "Cookie": cookie_hdr,
                    "Accept": "application/json, text/plain, */*",
                    "Accept-Language": "en-US,en;q=0.9",
                    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                                   "AppleWebKit/537.36 (KHTML, like Gecko) "
                                   "Chrome/155.0.8059.86 Safari/537.36"),
                }, proxies=proxies, timeout=25)
                body = r.text[:200].replace("\n", " ")
                print(f"{path:26s} {r.status_code} {body}")
            except Exception as exc:
                print(f"{path:26s} FAIL {type(exc).__name__}: {str(exc)[:60]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
