"""测 IPDEEP 会话粘性时长：用户名里的 ``sessiontime-N`` 改成 N 分钟试。

IPDEEP 的网关用「用户名编码配置」：``<uid>-res-country-US-session-XXXX-sessiontime-5``。
``sessiontime-5`` 表示 5 分钟粘性；过期后这条代理会失效（实测表现为
``SSL EOF`` / 页面 fetch 失败）。这里验证能不能直接改大。

    python tools/ipdeep_session_test.py --url "https://api.ipdeep.com/..."
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import requests  # noqa: E402

from app.net.http import no_env_session  # noqa: E402
from app.net.ipdeep import parse_proxy_payload  # noqa: E402

TARGETS = ("https://api.ip.sb/geoip", "https://qoder.com/")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", required=True)
    ap.add_argument("--country", default="US")
    ap.add_argument("--times", default="5,15,30,60")
    args = ap.parse_args()

    with no_env_session() as s0:
        r = s0.get(args.url, timeout=30)
    r.raise_for_status()
    p = parse_proxy_payload(r.text)[0]
    print(f"原始用户名: {p.username}")

    sess = no_env_session()
    for minutes in [x.strip() for x in args.times.split(",") if x.strip()]:
        user = re.sub(r"sessiontime-\d+", f"sessiontime-{minutes}", p.username)
        user = re.sub(r"country-[A-Za-z]{2}", f"country-{args.country}", user)
        proxy = f"http://{user}:{p.password}@{p.host}:{p.port}"
        px = {"http": proxy, "https": proxy}
        print(f"\nsessiontime={minutes}  user={user}")
        for target in TARGETS:
            try:
                resp = sess.get(target, proxies=px, timeout=30)
                text = resp.text.strip().replace("\n", " ")[:80]
                print(f"  {target:28s} HTTP {resp.status_code}  {text}")
            except Exception as exc:
                print(f"  {target:28s} FAIL {type(exc).__name__}: {str(exc)[:70]}")
    sess.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
