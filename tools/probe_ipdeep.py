"""IPDEEP 开放 API 实测：先打通认证，再提代理。

用法：
    python tools/probe_ipdeep.py --key XXXX --secret YYYY
    python tools/probe_ipdeep.py --key XXXX --secret YYYY --extract
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.net.ipdeep import IpdeepClient, IpdeepError  # noqa: E402


def show(obj) -> str:
    return json.dumps(obj, ensure_ascii=False, indent=2)[:1500]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--key", required=True)
    ap.add_argument("--secret", required=True)
    ap.add_argument("--base", default="https://api.ipdeep.cn")
    ap.add_argument("--extract", action="store_true", help="尝试提一个代理")
    ap.add_argument("--country", default="US")
    ap.add_argument("--num", type=int, default=1)
    ap.add_argument("--duration", type=int, default=30)
    args = ap.parse_args()

    c = IpdeepClient(args.key, args.secret, base=args.base)
    checks: list[tuple[str, bool, str]] = []

    # ---- 1. 余额（最简单的鉴权验证）----
    print("=== 1. 账户余额 ===")
    try:
        bal = c.balance()
        checks.append(("AES 签名 + 认证通过", True, show(bal)[:200]))
        print(show(bal))
    except IpdeepError as exc:
        checks.append(("AES 签名 + 认证通过", False,
                       f"code={exc.code} {exc}"))
        print(f"失败：code={exc.code} {exc}")
        print(f"原始响应：{show(exc.raw)}")

    # ---- 2. 流量 ----
    print("\n=== 2. 剩余流量 ===")
    try:
        tr = c.traffic()
        checks.append(("查询流量", True, ""))
        print(show(tr))
    except IpdeepError as exc:
        checks.append(("查询流量", False, f"code={exc.code} {exc}"))
        print(f"失败：code={exc.code} {exc}")

    # ---- 3. 国家列表 ----
    print("\n=== 3. 可用国家（动态住宅）===")
    try:
        cs = c.countries("dynamicResidential")
        checks.append(("查国家列表", True, f"{len(cs)} 个"))
        print(f"共 {len(cs)} 个，前 8 个：")
        for x in cs[:8]:
            print(f"  {x}")
    except IpdeepError as exc:
        checks.append(("查国家列表", False, f"code={exc.code} {exc}"))
        print(f"失败：code={exc.code} {exc}")

    # ---- 4. 提代理 ----
    if args.extract:
        print(f"\n=== 4. 提取代理（{args.country} × {args.num}，粘性 {args.duration} 分钟）===")
        try:
            proxies = c.generate_proxy(
                num=args.num, country_code=args.country,
                rotation_type="stickiness", duration=args.duration,
            )
            checks.append(("提取代理", bool(proxies), f"{len(proxies)} 条"))
            for p in proxies:
                print(f"  {p.url}")
                print(f"    host={p.host} port={p.port} user={p.username} "
                      f"proto={p.protocol} expire={p.expire_at}")
        except IpdeepError as exc:
            checks.append(("提取代理", False, f"code={exc.code} {exc}"))
            print(f"失败：code={exc.code} {exc}")
            print(f"原始：{show(exc.raw)}")

    print("\n=== 结果 ===")
    passed = 0
    for name, ok, detail in checks:
        print(f"[{'PASS' if ok else 'FAIL'}] {name:<24} {detail[:120]}")
        passed += ok
    print(f"\n{passed}/{len(checks)} 通过")
    return 0 if passed == len(checks) else 2


if __name__ == "__main__":
    raise SystemExit(main())
