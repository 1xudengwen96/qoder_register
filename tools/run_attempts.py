"""注册重试编排：每轮换一条代理跑完整注册，成功即停。

    python tools/run_attempts.py --rounds 4
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

ALIASES = [
    "postwar.ponchos0t@icloud.com",
    "bumpier-eves-0m@icloud.com",
    "bonfire_storm0r@icloud.com",
    "cinder.levels.0p@icloud.com",
    "tallish_papilla.0t@icloud.com",
    "hogan.collie-0t@icloud.com",
    "07-serial.extinct@icloud.com",
    "wiper-making-0y@icloud.com",
]


def run(cmd: list[str], timeout: int = 900) -> tuple[int, str]:
    p = subprocess.run(cmd, cwd=str(ROOT), capture_output=True, text=True,
                       encoding="utf-8", errors="replace", timeout=timeout)
    return p.returncode, (p.stdout or "") + (p.stderr or "")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--rounds", type=int, default=4)
    ap.add_argument("--code-timeout", type=int, default=420)
    args = ap.parse_args()

    used: set[str] = set()
    for i in range(1, args.rounds + 1):
        alias = next((a for a in ALIASES if a not in used), "")
        if not alias:
            print("别名用完了")
            break
        used.add(alias)
        print(f"\n========== 第 {i} 轮  {alias} ==========", flush=True)

        rc, out = run([sys.executable, "tools/rotate_proxy.py"])
        if rc != 0:
            print("代理池耗尽/不可用，自动补一批…", flush=True)
            run([sys.executable, "scripts/fetch_proxies.py"], timeout=120)
            run([sys.executable, "scripts/convert_proxies.py"], timeout=60)
            rc, out = run([sys.executable, "tools/rotate_proxy.py"])
        m = re.search(r"ip\s+(\S+)\s+(\S+)", out)
        print(f"代理: {m.group(1) if m else '?'} {m.group(2) if m else ''} "
              f"(rc={rc})", flush=True)
        if rc != 0:
            print("代理不可用，换下一条", flush=True)
            continue

        rc2, out2 = run([
            sys.executable, "tools/run_signup_cdp.py",
            "--mail-source", "relay", "--relay-address", alias,
            "--code-timeout", str(args.code_timeout),
            "--captcha-max", "6", "--keep-open",
        ])
        tail = "\n".join(out2.strip().splitlines()[-25:])
        print(tail, flush=True)
        if '"ok": true' in out2:
            print(f"\n*** 注册成功: {alias} ***", flush=True)
            return 0
    print("\n本轮没成功，看上面的阶段日志定位", flush=True)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
