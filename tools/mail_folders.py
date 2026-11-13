"""看某账号的 INBOX / Junk 最近邮件，判断 Qoder 验证信到底有没有到。"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import secrets  # noqa: E402
from app.mail.outlook_oauth import OutlookMailbox, load_accounts  # noqa: E402


def main() -> int:
    idx = int(sys.argv[1]) if len(sys.argv) > 1 else 0
    accs = load_accounts(secrets.get("mail.outlook_file",
                                    "D:/Backup/Downloads/outlook.txt"))
    acc = accs[idx]
    proxy = secrets.get("mail.imap_proxy", "http://127.0.0.1:7897")
    box = OutlookMailbox(acc, proxy=proxy, timeout=25)
    box.refresh_access_token()
    box.connect()
    print(f"=== {acc.email} ===")
    for folder in ("INBOX", "Junk"):
        try:
            n = box.select(folder)
            print(f"[{folder}] select -> {n}")
        except Exception as exc:
            print(f"[{folder}] 打不开: {type(exc).__name__}: {exc}")
            continue
        try:
            uids = box.list_uids()
        except Exception as exc:
            print(f"[{folder}] 列 UID 失败: {type(exc).__name__}")
            continue
        print(f"\n[{folder}] 共 {len(uids)} 封，最近 8 封：")
        for u in uids[-8:]:
            msg = box.fetch(u)
            if msg is None:
                continue
            print(f"  - {(msg.subject or '(无主题)')[:60]:60s} | "
                  f"{(msg.sender or '')[:45]}")
    try:
        box.close()
    except Exception:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
