"""验证 Outlook OAuth2 收信（手工 XOAUTH2 版）。"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.mail.outlook_oauth import check_account, load_accounts  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--file", default="D:/Backup/Downloads/outlook.txt")
    ap.add_argument("--limit", type=int, default=3)
    ap.add_argument("--proxy", default="http://127.0.0.1:7897",
                    help="留空 '' 表示不走代理")
    args = ap.parse_args()

    accs = load_accounts(args.file)
    if not accs:
        print(f"没读到账号：{args.file}")
        return 2

    proxy = args.proxy or None
    print(f"=== Outlook OAuth2 验证（共 {len(accs)} 个，测前 {args.limit} 个）===")
    print(f"代理: {proxy or '不走代理'}\n")

    results = []
    for i, acc in enumerate(accs[: args.limit], 1):
        print(f"[{i}] {acc.email}")
        r = check_account(acc, proxy=proxy)
        results.append(r)
        print(f"    token: {'OK' if r['token_ok'] else 'FAIL'}")
        print(f"    imap : {'OK' if r['imap_ok'] else 'FAIL'}")
        if r["imap_ok"]:
            print(f"    INBOX: {r['inbox_count']} 封")
            print(f"    最新主题: {r['latest_subject'] or '(无)'}")
        if r["error"]:
            print(f"    错误: {r['error'][:150]}")
        print()

    passed = sum(1 for r in results if r["imap_ok"])
    print("=== 结果 ===")
    for r in results:
        mark = "PASS" if r["imap_ok"] else "FAIL"
        detail = f"{r['inbox_count']} 封" if r["imap_ok"] else r["error"][:90]
        print(f"[{mark}] {r['email']:<45} {detail}")

    print(f"\n{passed}/{len(results)} 通过")
    return 0 if passed == len(results) else 2


if __name__ == "__main__":
    raise SystemExit(main())
