"""读取指定 UID 的邮件完整内容（找验证码）。

用法：
    python tools/read_mail.py --index 5 --uid 9
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.mail.outlook_oauth import OutlookMailbox, load_accounts  # noqa: E402

MAIL_FILE = "D:/Backup/Downloads/outlook.txt"
PROXY = "http://127.0.0.1:7897"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--index", type=int, required=True)
    ap.add_argument("--uid", required=True)
    ap.add_argument("--folder", default="INBOX")
    args = ap.parse_args()

    accs = load_accounts(MAIL_FILE)
    acc = accs[args.index]
    print(f"=== {acc.email}  UID={args.uid} ===\n")

    box = OutlookMailbox(acc, proxy=PROXY)
    try:
        box.refresh_access_token()
        box.connect()
        box.select(args.folder)
        msg = box.fetch(args.uid.encode() if isinstance(args.uid, str) else args.uid)
        if msg is None:
            print("读不到邮件")
            return 2

        print(f"发件人: {msg.sender}")
        print(f"主题  : {msg.subject}")
        print(f"时间  : {msg.received_at}")
        print(f"收件人: {msg.to}")
        print("=" * 70)

        body = msg.body_text or ""
        html = msg.body_html or ""
        blob = f"{msg.subject}\n{body}\n{html}"

        print("=== 纯文本正文 ===")
        print(body[:3000] if body else "(空)")
        print("\n=== 正文里的 6 位数字 ===")
        six = re.findall(r"(?<!\d)(\d{6})(?!\d)", blob)
        print(f"  {six[:15]}")

        print("\n=== 正文里所有 4~8 位数字 ===")
        nums = re.findall(r"\b\d{4,8}\b", blob)
        print(f"  {nums[:25]}")

        # HTML 里找"code"附近的数字
        print("\n=== HTML 里 code 附近的内容 ===")
        for m in re.finditer(r"(?i)(code|验证码|verification)", html):
            s = max(0, m.start() - 150)
            e = min(len(html), m.end() + 250)
            frag = re.sub(r"\s+", " ", html[s:e])
            print(f"  ...{frag[:350]}...")
            print()

        # 保存 HTML 供分析
        out = ROOT / "data" / f"mail_{args.uid}.html"
        out.write_text(html or body, encoding="utf-8")
        print(f"\n已保存原文: {out}")
    finally:
        box.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
