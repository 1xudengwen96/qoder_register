"""从 device 成功页里找 qoder-app:// 深链，并用系统协议投递给客户端。"""

from __future__ import annotations

import json
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import secrets  # noqa: E402
from app.register import cdp_raw  # noqa: E402
from app.register.bitbrowser import BitBrowserClient  # noqa: E402

JS = r"""(() => {
  const out = {hrefs: [], html: ''};
  document.querySelectorAll('a[href]').forEach(a => {
    const h = a.getAttribute('href') || '';
    if (h.startsWith('qoder-app://') || h.includes('qoder-app')) {
      out.hrefs.push(h);
    }
  });
  const txt = document.documentElement.outerHTML || '';
  const m = txt.match(/qoder-app:\/\/[^\s"'<>]+/g) || [];
  out.found = Array.from(new Set(m)).slice(0, 5);
  return out;
})()"""


def main() -> int:
    client = BitBrowserClient()
    launch = client.open_env(secrets.get("bitbrowser.env_id"))
    pages = [t for t in cdp_raw.http_targets(launch.cdp_http)
             if t.get("type") == "page"]
    page = next((p for p in pages if "selectAccounts" in (p.get("url") or "")),
                None)
    if not page:
        print("没有 selectAccounts 页")
        return 1
    with cdp_raw.CDP(launch.cdp_http, ws_url=page["webSocketDebuggerUrl"],
                     timeout=45) as cdp:
        cdp.enable()
        data = cdp.evaluate(JS) or {}
        print(json.dumps(data, ensure_ascii=False)[:600])

    links = list(data.get("found") or []) + list(data.get("hrefs") or [])
    links = [l for l in links if l.startswith("qoder-app://")]
    if not links:
        print("页面里没有 qoder-app 深链")
        return 1
    deep = links[0]
    print("交给系统协议:", deep[:160])
    subprocess.Popen(["cmd", "/c", "start", "", deep],
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    time.sleep(8)

    from app.claim.token_harvester import harvest_token

    tok = harvest_token()
    if tok:
        print(f"PASS 客户端 token: {tok.token[:16]}… source={tok.source}")
        return 0
    print("客户端还没落 token")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
