"""IPDEEP 端到端验证：认证 → 国家列表 → 提取代理。

用法：
    python tools/verify_ipdeep.py --key X --secret Y
    python tools/verify_ipdeep.py --key X --secret Y --extract --country US
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.net.ipdeep import IpdeepClient, IpdeepError  # noqa: E402


def show(obj, limit: int = 1200) -> str:
    return json.dumps(obj, ensure_ascii=False, indent=2)[:limit]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--key", required=True)
    ap.add_argument("--secret", required=True)
    ap.add_argument("--host", default="https://api.ipdeep.cn")
    ap.add_argument("--proxy", default="http://127.0.0.1:7897")
    ap.add_argument("--extract", action="store_true")
    ap.add_argument("--country", default="US")
    ap.add_argument("--num", type=int, default=1)
    ap.add_argument("--duration", type=int, default=30)
    ap.add_argument("--aes", default="hex32", choices=["hex32", "digest16"])
    args = ap.parse_args()

    c = IpdeepClient(args.key, args.secret, host=args.host,
                     proxy=args.proxy, aes_mode=args.aes)
    checks: list[tuple[str, bool, str]] = []

    # ---- 1. 国家列表（验证认证）----
    print("=== 1. 国家列表（动态住宅）===")
    try:
        cs = c.get_countries("dynamicResidential")
        checks.append(("认证 + 国家列表", bool(cs), f"{len(cs)} 个国家"))
        print(f"共 {len(cs)} 个。前 10 个：")
        for x in cs[:10]:
            print(f"  {x}")
        # 找 US
        us = [x for x in cs if str(x).upper().find("US") >= 0]
        if us:
            print(f"\n美国相关项：{us[:3]}")
    except IpdeepError as exc:
        checks.append(("认证 + 国家列表", False, f"code={exc.code} {exc}"))
        print(f"失败 code={exc.code} {exc}")
        print(show(exc.raw))

    # ---- 2. 静态 IP 数 ----
    print("\n=== 2. 静态 IP 数量 ===")
    try:
        n = c.get_ip_num()
        checks.append(("静态 IP 数", True, show(n)[:100]))
        print(show(n))
    except IpdeepError as exc:
        checks.append(("静态 IP 数", False, f"code={exc.code} {exc}"))
        print(f"失败 code={exc.code} {exc}")

    # ---- 3. 提取代理 ----
    if args.extract:
        print(f"\n=== 3. 提取代理（{args.country} × {args.num}，粘性 {args.duration} 分钟）===")
        try:
            ps = c.extract_dynamic_ip(
                num=args.num, country_code=args.country,
                rotation_type="stickiness", duration=args.duration,
                extract_type="authentication",
            )
            checks.append(("提取代理", bool(ps), f"{len(ps)} 条"))
            for p in ps:
                print(f"  {p.url}")
                print(f"    host={p.host}:{p.port} user={p.username} "
                      f"proto={p.protocol} expire={p.expire_at}")
                print(f"    原始: {show(p.raw, 400)}")
        except IpdeepError as exc:
            checks.append(("提取代理", False, f"code={exc.code} {exc}"))
            print(f"失败 code={exc.code} {exc}")
            print(show(exc.raw))

    print("\n=== 结果 ===")
    passed = 0
    for name, ok, detail in checks:
        print(f"[{'PASS' if ok else 'FAIL'}] {name:<22} {detail[:110]}")
        passed += ok
    print(f"\n{passed}/{len(checks)} 通过")
    return 0 if passed == len(checks) else 2


if __name__ == "__main__":
    raise SystemExit(main())
