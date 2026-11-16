"""按代理的真实出口创建比特环境：时区/语言跟着出口走。

流程：取一条未用代理 → 测出口(IP/国家/时区) → 用该时区建环境 →
干净启动 → 复测 lastIp/时区一致性 → 打印 env_id。

    python tools/new_env_for_proxy.py                 # 名字自动 qoder_acct_NN
    python tools/new_env_for_proxy.py --name my_env
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.config import get, load_config  # noqa: E402
from app.net.http import no_env_session  # noqa: E402
from app.net.proxy import ProxyLedger, ProxyPool, acquire_once  # noqa: E402
from app.register import cdp_raw  # noqa: E402
from app.register.bitbrowser import (  # noqa: E402
    BitBrowserClient,
    build_fingerprint,
    build_proxy_config,
)

GEO = "https://api.ip.sb/geoip"


def probe(px) -> dict:
    with no_env_session() as s:
        r = s.get(GEO, proxies=px.requests_proxies(), timeout=30)
        return r.json()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--name", default="")
    ap.add_argument("--file", default="")
    ap.add_argument("--keep-open", action="store_true", default=True)
    args = ap.parse_args()

    cfg = load_config()
    pool = ProxyPool.from_file(args.file or get(cfg, "proxy.file", "data/proxies.txt"))
    ledger = ProxyLedger(get(cfg, "proxy.state_file", "data/.proxy_state.json"))

    px = None
    geo = {}
    for _ in range(min(6, len(pool.proxies))):
        cand = acquire_once(pool, ledger, check=False)
        if cand is None:
            print("没有未用代理了")
            return 2
        try:
            geo = probe(cand)
            px = cand
            break
        except Exception as exc:
            print(f"  跳过 {cand.label}: {type(exc).__name__}")
    if px is None:
        print("连续 6 条都不通")
        return 2

    tz = geo.get("timezone") or "Asia/Singapore"
    country = geo.get("country_code") or ""
    ip = geo.get("ip") or ""
    print(f"代理 {px.label} 出口 {ip} {country} {tz}")

    client = BitBrowserClient()
    name = args.name or f"qoder_acct_{int(time.time()) % 10000}"
    fp = build_fingerprint(
        ua=("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/155.0.8059.86 Safari/537.36"),
        timezone=tz, languages=["en-US", "en"],
        hardware_concurrency=8, device_memory=8)
    fp["isIpv6"] = False
    fp["isIpCreateTimeZone"] = False       # 显式钉死，避免创建瞬间的检测结果覆盖
    fp["isIpCreatePosition"] = False
    fp["position"] = ""
    fp["isIpCreateLanguage"] = False
    fp["isIpCreateDisplayLanguage"] = False
    fp["languages"] = "en-US,en"
    fp["displayLanguages"] = "en-US,en"

    env = client.create_env(name=name, fingerprint=fp, proxy_raw=px.raw,
                            remark="qoder_acct")
    print(f"PASS 新建环境 {env.id} tz={tz}")

    client.prepare_fresh_launch(env.id, "about:blank")
    launch = client.open_env(env.id)
    print(f"PASS 启动 CDP={launch.cdp_http}")

    time.sleep(3)
    verify = {}
    for _ in range(10):
        try:
            with cdp_raw.CDP(launch.cdp_http) as page:
                page.enable()
                page.navigate("about:blank", timeout=20)
                verify = page.evaluate(cdp_raw.PROBE_JS) or {}
            break
        except Exception:
            time.sleep(2)

    e2 = client.get_env(env.id)
    print(json.dumps({
        "env_id": env.id, "name": name,
        "proxy": f"{e2.host}:{e2.port}", "proxy_user": e2.proxy_user,
        "ip": verify.get("ip"), "country": verify.get("ipCountry"),
        "tz": verify.get("timezone"), "ipTz": verify.get("ipTz"),
        "lastIp": e2.raw.get("lastIp"),
        "tz_match": verify.get("timezone") == verify.get("ipTz"),
    }, ensure_ascii=False, indent=2))
    ledger.mark(px, env=env.id, ip=ip, note="env-created")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
