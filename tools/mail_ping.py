"""邮箱 IMAP 连通性巡检：逐个试，记录走代理 / 直连 是否可用。

    python tools/mail_ping.py --limit 15
    python tools/mail_ping.py --index 0 --index 1
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import secrets  # noqa: E402
from app.mail.outlook_oauth import OutlookMailbox, load_accounts  # noqa: E402

MAIL_FILE = "D:/Backup/Downloads/outlook.txt"
PROXY = "http://127.0.0.1:7897"


def probe(acc, proxy: str | None, retries: int = 1) -> tuple[bool, str]:
    last = ""
    for i in range(retries):
        box = None
        try:
            box = OutlookMailbox(acc, proxy=proxy)
            box.refresh_access_token()
            box.connect()
            box.select("INBOX")
            uids = box.list_uids()
            return True, f"{len(uids)} 封"
        except Exception as exc:
            last = f"{type(exc).__name__}: {exc}"
        finally:
            if box is not None:
                try:
                    box.close()
                except Exception:
                    pass
        if i + 1 < retries:
            time.sleep(3)
    return False, last


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--mail-file",
                    default=secrets.get("mail.outlook_file", MAIL_FILE))
    ap.add_argument("--limit", type=int, default=15)
    ap.add_argument("--index", type=int, action="append", default=None)
    ap.add_argument("--proxy", default=secrets.get("mail.imap_proxy", PROXY))
    ap.add_argument("--direct", action="store_true", help="同时对直连做对照")
    args = ap.parse_args()

    accs = load_accounts(args.mail_file)
    idxs = args.index if args.index else list(range(min(args.limit, len(accs))))
    good: list[int] = []
    for i in idxs:
        acc = accs[i]
        t0 = time.time()
        ok, detail = probe(acc, args.proxy, retries=1)
        line = f"[{'PASS' if ok else 'FAIL'}] #{i:<2} {acc.email:42s} " \
               f"代理 {detail[:60]} ({time.time() - t0:.1f}s)"
        if args.direct:
            ok2, detail2 = probe(acc, None, retries=1)
            line += f" | 直连 {'PASS' if ok2 else 'FAIL'} {detail2[:40]}"
        print(line, flush=True)
        if ok:
            good.append(i)
    print(f"\n可用: {len(good)}/{len(idxs)} -> {good}")
    return 0 if good else 1


if __name__ == "__main__":
    raise SystemExit(main())
