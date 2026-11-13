"""IMAP 单步调试：逐步打印，定位到底卡在哪一层。"""

from __future__ import annotations

import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import secrets  # noqa: E402
from app.mail.outlook_oauth import (  # noqa: E402
    OutlookMailbox,
    ProxiedIMAP4SSL,
    load_accounts,
)


def main() -> int:
    idx = int(sys.argv[1]) if len(sys.argv) > 1 else 3
    proxy = secrets.get("mail.imap_proxy", "http://127.0.0.1:7897")
    accs = load_accounts(secrets.get("mail.outlook_file",
                                     "D:/Backup/Downloads/outlook.txt"))
    acc = accs[idx]
    print(f"账号 #{idx} {acc.email}  proxy={proxy}")

    box = OutlookMailbox(acc, proxy=proxy, timeout=20)
    t = time.time()
    try:
        box.refresh_access_token()
        print(f"token OK {time.time() - t:.1f}s")
    except Exception as exc:
        print(f"token FAIL {type(exc).__name__}: {exc}")
        return 1

    print("--- 隧道连接 ---")
    t = time.time()
    try:
        conn = ProxiedIMAP4SSL(box.host, 993, proxy=proxy, timeout=20)
        print(f"隧道+TLS OK {time.time() - t:.1f}s  欢迎语={conn.welcome!r}")
    except Exception as exc:
        print(f"隧道 FAIL {type(exc).__name__}: {exc}")
        return 1

    print("--- XOAUTH2 ---")
    try:
        box._xoauth2(conn)
        print("认证 OK")
        print("SELECT:", conn.select("INBOX"))
        print("UID 数:", len(conn.uid("search", None, "ALL")[1][0].split()))
        conn.logout()
    except Exception as exc:
        print(f"认证/查询 FAIL {type(exc).__name__}: {exc}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
