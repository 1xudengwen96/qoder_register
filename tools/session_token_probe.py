"""从注册成功的浏览器会话里找 token：cookie / localStorage / sessionStorage。

    python tools/session_token_probe.py
    python tools/session_token_probe.py --api /api/v1/users/me
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import secrets  # noqa: E402
from app.register import cdp_raw  # noqa: E402
from app.register.bitbrowser import BitBrowserClient  # noqa: E402

JS_STORAGE = r"""(() => {
  const out = {cookie: document.cookie || '', local: {}, session: {}, url: location.href};
  try {
    for (let i = 0; i < localStorage.length; i++) {
      const k = localStorage.key(i);
      const v = localStorage.getItem(k) || '';
      out.local[k] = v.length > 120 ? v.slice(0, 120) + '…' : v;
    }
  } catch (e) {}
  try {
    for (let i = 0; i < sessionStorage.length; i++) {
      const k = sessionStorage.key(i);
      const v = sessionStorage.getItem(k) || '';
      out.session[k] = v.length > 120 ? v.slice(0, 120) + '…' : v;
    }
  } catch (e) {}
  return out;
})()"""


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--env-id", default=secrets.get("bitbrowser.env_id", ""))
    ap.add_argument("--api", default="")
    args = ap.parse_args()

    client = BitBrowserClient()
    launch = client.open_env(args.env_id)
    endpoint = launch.cdp_http
    pages = [t for t in cdp_raw.http_targets(endpoint) if t.get("type") == "page"]
    page = next((t for t in pages if "qoder.com" in (t.get("url") or "")),
                pages[0] if pages else None)
    if not page:
        print("没有页面")
        return 1

    with cdp_raw.CDP(endpoint, ws_url=page["webSocketDebuggerUrl"]) as cdp:
        cdp.enable()
        print("当前页:", cdp.current_url())
        data = cdp.evaluate(JS_STORAGE) or {}
        print("\ncookie:", (data.get("cookie") or "")[:300])
        print("\nlocalStorage:")
        for k, v in (data.get("local") or {}).items():
            print(f"  {k} = {v}")
        print("\nsessionStorage:")
        for k, v in (data.get("session") or {}).items():
            print(f"  {k} = {v}")

        # 所有 cookie（含 httpOnly）
        try:
            res = cdp.call("Network.getCookies",
                           {"urls": ["https://qoder.com", "https://www.qoder.com"]})
            print("\nCDP cookies:")
            for c in res.get("cookies") or []:
                val = c.get("value", "")
                print(f"  {c.get('name')}: {val[:60]}{'…' if len(val) > 60 else ''} "
                      f"(httpOnly={c.get('httpOnly')}, domain={c.get('domain')})")
        except Exception as exc:
            print("取 cookie 失败:", exc)

        if args.api:
            js = ("(async () => { const r = await fetch(%s, {credentials: 'include'});"
                  " const t = await r.text(); return r.status + ' ' + t.slice(0,300); })()"
                  % json.dumps(args.api))
            print("\nAPI:", cdp.evaluate(js, await_promise=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
