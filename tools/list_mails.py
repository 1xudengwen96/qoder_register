"""列出邮箱所有邮件标题，确认 Qoder 验证邮件是否到达。

用法：
    python tools/list_mails.py --index 5
    python tools/list_mails.py --email X@outlook.com --all
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.mail.outlook_oauth import OutlookMailbox, load_accounts  # noqa: E402

MAIL_FILE = "D:/Backup/Downloads/outlook.txt"
PROXY = "http://127.0.0.1:7897"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--email", default="")
    ap.add_argument("--index", type=int, default=-1)
    ap.add_argument("--all", action="store_true", help="列出全部（默认最近20封）")
    ap.add_argument("--folder", default="INBOX")
    args = ap.parse_args()

    accs = load_accounts(MAIL_FILE)
    acc = None
    if args.email:
        acc = next((a for a in accs if a.email.lower() == args.email.lower()), None)
    elif args.index >= 0:
        acc = accs[args.index] if args.index < len(accs) else None
    if acc is None:
        print("找不到邮箱")
        return 2

    print(f"=== {acc.email}  文件夹={args.folder} ===\n")
    box = OutlookMailbox(acc, proxy=PROXY)
    try:
        box.refresh_access_token()
        box.connect()
        try:
            box.select(args.folder)
        except Exception as exc:
            print(f"选文件夹失败: {exc}")
            return 2

        uids = box.list_uids()
        print(f"共 {len(uids)} 封\n")

        sel = uids if args.all else uids[-20:]
        print(f"{'UID':<6} {'时间':<26} {'发件人':<42} 主题")
        print("-" * 120)
        for u in sel:
            msg = box.fetch(u)
            if msg is None:
                continue
            dt = msg.received_at.strftime("%Y-%m-%d %H:%M:%S") if msg.received_at else ""
            sender = (msg.sender or "")[:40]
            subj = (msg.subject or "")[:50]
            print(f"{str(msg.uid):<6} {dt:<26} {sender:<42} {subj}")

        # 统计是否有 qoder 相关
        qoder_cnt = 0
        for u in uids:
            msg = box.fetch(u)
            if msg and "qoder" in f"{msg.sender}{msg.subject}".lower():
                qoder_cnt += 1
        print(f"\n含 'qoder' 的邮件（发件人或主题）: {qoder_cnt} 封")

        # 列出所有文件夹
        print("\n=== 邮箱文件夹列表 ===")
        try:
            import imaplib
            typ, data = box._conn.list()
            for line in data or []:
                print(f"  {line.decode('utf-8', 'ignore')}")
        except Exception as exc:
            print(f"  列文件夹失败: {exc}")

    finally:
        box.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
