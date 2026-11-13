"""2Captcha 阿里云实测：余额 → 建任务 → 拿 token。

用法：
    python tools/test_twocaptcha.py
    python tools/test_twocaptcha.py --solve
"""

from __future__ import annotations

import os

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.net.twocaptcha_aliyun import (  # noqa: E402
    QODER_ALIYUN,
    TwoCaptchaAliyun,
    TwoCaptchaError,
)

API_KEY = os.environ.get("TWOCAPTCHA_KEY", "")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--key", default=API_KEY)
    ap.add_argument("--solve", action="store_true", help="真的建任务解析")
    ap.add_argument("--proxy", default="http://127.0.0.1:7897")
    args = ap.parse_args()

    c = TwoCaptchaAliyun(args.key, proxy=args.proxy)
    checks: list[tuple[str, bool, str]] = []

    # ---- 1. 余额 ----
    print("=== 1. 账户余额 ===")
    try:
        bal = c.balance()
        checks.append(("API Key 有效", True, f"余额 ${bal:.4f}"))
        print(f"  余额: ${bal:.4f}")
        if bal < 0.01:
            print("  ⚠ 余额偏低，可能不够解析")
    except TwoCaptchaError as exc:
        checks.append(("API Key 有效", False, f"code={exc.code} {exc}"))
        print(f"  失败: code={exc.code} {exc}")
        print(f"  原始: {json.dumps(exc.raw, ensure_ascii=False)[:300]}")

    print("\n=== 参数（Qoder 实测）===")
    for k, v in QODER_ALIYUN.items():
        print(f"  {k:<12} = {v}")

    # ---- 2. 建任务 ----
    if args.solve:
        print("\n=== 2. 建阿里云任务 ===")
        tid = ""
        try:
            tid = c.create_aliyun_task()
            checks.append(("建任务成功", True, f"taskId={tid}"))
            print(f"  taskId = {tid}")
        except TwoCaptchaError as exc:
            checks.append(("建任务成功", False, f"code={exc.code} {exc}"))
            print(f"  失败: code={exc.code} {exc}")
            print(f"  原始: {json.dumps(exc.raw, ensure_ascii=False)[:600]}")

        # ---- 3. 等结果 ----
        if tid:
            print("\n=== 3. 等待解析结果（最多 300s）===")
            try:
                sol = c.wait_result(tid)
                checks.append(("拿到 token", bool(sol.token), f"{sol.elapsed:.1f}s"))
                print(f"  耗时: {sol.elapsed:.1f}s")
                print(f"  u_atoken: {sol.token[:60]}..." if sol.token else "  无 token")
                print(f"  u_asig  : {sol.sig[:60]}..." if sol.sig else "  无 sig")
                print(f"  完整 solution keys: "
                      f"{list((sol.raw.get('solution') or {}).keys())}")
                print(f"\n  原始返回:\n{json.dumps(sol.raw, ensure_ascii=False, indent=2)[:1500]}")
            except TwoCaptchaError as exc:
                checks.append(("拿到 token", False, f"code={exc.code} {exc}"))
                print(f"  失败: code={exc.code} {exc}")
                print(f"  原始: {json.dumps(exc.raw, ensure_ascii=False)[:500]}")

    # ---- 汇总 ----
    print("\n=== 结果 ===")
    passed = 0
    for name, ok, detail in checks:
        print(f"[{'PASS' if ok else 'FAIL'}] {name:<16} {detail[:100]}")
        passed += ok
    print(f"\n{passed}/{len(checks)} 通过")
    return 0 if passed == len(checks) else 2


if __name__ == "__main__":
    raise SystemExit(main())
