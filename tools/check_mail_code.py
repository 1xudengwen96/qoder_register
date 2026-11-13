"""检查指定邮箱最近邮件的完整内容，确认验证码提取是否正确。

用法：
    python tools/check_mail_code.py --email X@outlook.com
    python tools/check_mail_code.py --index 5
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.mail.outlook_oauth import (  # noqa: E402
    OutlookMailbox,
    load_accounts,
)

MAIL_FILE = "D:/Backup/Downloads/outlook.txt"
PROXY = "http://127.0.0.1:7897"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--email", default="")
    ap.add_argument("--index", type=int, default=-1)
    ap.add_argument("--limit", type=int, default=3)
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

    print(f"=== {acc.email} ===\n")
    box = OutlookMailbox(acc, proxy=PROXY)
    try:
        box.refresh_access_token()
        box.connect()
        box.select("INBOX")
        uids = box.list_uids()
        print(f"INBOX 共 {len(uids)} 封，检查最近 {args.limit} 封\n")

        for u in uids[-args.limit:][::-1]:
            msg = box.fetch(u)
            if msg is None:
                continue
            print("=" * 70)
            print(f"UID    : {msg.uid}")
            print(f"发件人 : {msg.sender}")
            print(f"主题   : {msg.subject}")
            print(f"收件人 : {msg.to}")
            print(f"时间   : {msg.received_at}")
            print("-" * 70)

            body = msg.body_text or ""
            html = msg.body_html or ""
            blob = f"{msg.subject}\n{body}\n{html}"

            # 找所有数字串
            nums = re.findall(r"\b\d{4,8}\b", blob)
            print(f"正文里的数字串(4~8位): {nums[:20]}")

            # 6位数字
            six = re.findall(r"(?<!\d)(\d{6})(?!\d)", blob)
            print(f"6位数字: {six[:10]}")

            # 打印正文前 1200 字
            print("\n--- 正文（前 1200 字）---")
            print((body or html)[:1200])
            print()
    finally:
        box.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
