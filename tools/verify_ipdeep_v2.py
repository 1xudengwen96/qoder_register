"""验证修正后的 IPDEEP API（小写 action + PascalCase 参数）。"""

from __future__ import annotations

import os

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.net.ipdeep import IpdeepClient, IpdeepError  # noqa: E402

KEY = os.environ.get("IPDEEP_KEY", "")
SECRET = os.environ.get("IPDEEP_SECRET", "")


def show(x, n: int = 600) -> str:
    return json.dumps(x, ensure_ascii=False, indent=2)[:n] if not isinstance(x, str) else x[:n]


def main() -> int:
    c = IpdeepClient(KEY, SECRET)
    checks: list[tuple[str, bool, str]] = []

    tests = [
        ("账户余额", lambda: c.balance()),
        ("流量", lambda: c.traffic()),
        ("国家列表", lambda: c.countries("dynamicResidential")),
    ]

    for name, fn in tests:
        print(f"\n=== {name} ===")
        try:
            r = fn()
            checks.append((name, True, ""))
            print(show(r))
        except IpdeepError as exc:
            checks.append((name, False, f"code={exc.code} {exc}"))
            print(f"失败 code={exc.code} {exc}")
            if exc.raw:
                print(f"原始: {json.dumps(exc.raw, ensure_ascii=False)[:400]}")

    print("\n=== 结果 ===")
    passed = 0
    for name, ok, detail in checks:
        print(f"[{'PASS' if ok else 'FAIL'}] {name:<12} {detail[:100]}")
        passed += ok
    print(f"\n{passed}/{len(checks)} 通过  AES模式={c._aes_mode}")
    return 0 if passed == len(checks) else 2


if __name__ == "__main__":
    raise SystemExit(main())
