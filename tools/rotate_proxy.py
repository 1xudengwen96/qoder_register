"""从 data/proxies.txt 取一条未使用的代理，写进比特环境并启动验证。

「一条一用」：取用即记账，不会重复用同一条。

    python tools/rotate_proxy.py                # 用配置里的环境
    python tools/rotate_proxy.py --env-id X --keep-open
    python tools/rotate_proxy.py --reuse-index 2   # 复用第 3 条（不记账）
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import secrets  # noqa: E402
from app.config import get, load_config  # noqa: E402
from app.net.proxy import ProxyLedger, ProxyPool, acquire_once  # noqa: E402
from app.register import cdp_raw  # noqa: E402
from app.register.bitbrowser import (  # noqa: E402
    BitBrowserClient,
    BitBrowserError,
    build_proxy_config,
)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--env-id", default=secrets.get("bitbrowser.env_id", ""))
    ap.add_argument("--file", default="")
    ap.add_argument("--region", default="", help="按地区过滤（留空=不限）")
    ap.add_argument("--reuse-index", type=int, default=-1)
    ap.add_argument("--keep-open", action="store_true", default=True)
    ap.add_argument("--no-verify", action="store_true")
    args = ap.parse_args()

    cfg = load_config()
    path = args.file or get(cfg, "proxy.file", "data/proxies.txt")
    pool = ProxyPool.from_file(path)
    ledger = ProxyLedger(get(cfg, "proxy.state_file", "data/.proxy_state.json"))
    print(f"代理池: {pool.summary()}")

    if args.reuse_index >= 0:
        px = pool.proxies[args.reuse_index % len(pool.proxies)]
        print(f"复用 #{args.reuse_index} {px.label}（不记账）")
    else:
        px = acquire_once(pool, ledger, region=(args.region or None),
                          check=False)
        if px is None:
            print("没有未使用的代理了，往 data/proxies.txt 加新条目")
            return 2
        print(f"取用新代理 {px.label}")
        print(f"剩余未用: {ledger.remaining([p.raw for p in pool.proxies])}")

    client = BitBrowserClient()
    if not client.health():
        print("比特浏览器 API 不通")
        return 2
    env = client.get_env(args.env_id)
    if env.status == 1:
        client.close_env(args.env_id)
        time.sleep(3)

    fields = build_proxy_config(px.raw)
    client.update_env(args.env_id, **fields)
    shown = {k: ("***" if "assword" in k else v) for k, v in fields.items()}
    print("写入环境:", json.dumps(shown, ensure_ascii=False))
    client.prepare_fresh_launch(args.env_id, "about:blank")

    try:
        launch = client.open_env(args.env_id)
    except BitBrowserError as exc:
        print(f"启动失败: {exc} code={exc.code}")
        return 1
    print(f"已启动 CDP={launch.cdp_http}")

    probe = {}
    if not args.no_verify:
        deadline = time.time() + 30
        while time.time() < deadline:
            try:
                with cdp_raw.CDP(launch.cdp_http) as page:
                    page.enable()
                    page.navigate("about:blank", timeout=20)
                    probe = page.evaluate(cdp_raw.PROBE_JS) or {}
                break
            except Exception:
                time.sleep(2)
        e2 = client.get_env(args.env_id)
        print("\n---- 出口实测 ----")
        for k in ("ip", "ipCountry", "ipTz", "timezone", "languages", "webdriver"):
            print(f"  {k:12s} {probe.get(k)}")
        print(f"  lastIp       {e2.raw.get('lastIp')} ({e2.raw.get('lastCountry')})")
        if probe.get("ipError"):
            print(f"  ipError      {probe['ipError']}")
        if not probe.get("ip"):
            print("\n这条代理没出网，换下一条")
            return 1
        ledger.mark(px, env=args.env_id, ip=str(probe.get("ip", "")),
                    note="used")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
