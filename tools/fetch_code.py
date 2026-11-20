"""从 outlook 清单邮箱取 Qoder 验证码，输出到控制台（人工注册辅助）。

用法：
    $env:PYTHONPATH='.;.pylibs'
    python tools/fetch_code.py --email PhilippinaSubia648@hotmail.com
    python tools/fetch_code.py --email xxx --wait 120   # 轮询等新码
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / ".pylibs"))

from app.mail.code_extract import extract_code, is_qoder_mail  # noqa: E402
from app.mail.outlook_oauth import OutlookMailbox, load_accounts  # noqa: E402


def find_code(box, baseline: set[str]) -> tuple[str, str]:
    for folder in ("INBOX", "Junk"):
        try:
            box.select(folder)
            uids = box.list_uids()
        except Exception:
            continue
        for u in reversed(uids[-30:]):
            key = u.decode() if isinstance(u, bytes) else str(u)
            if key in baseline:
                continue
            try:
                msg = box.fetch(u)
            except Exception:
                continue
            if msg is None:
                continue
            if not is_qoder_mail(msg.sender, msg.subject, msg.body_text):
                baseline.add(key)
                continue
            code = extract_code(msg.body_html or msg.body_text)
            if not code:
                code = extract_code(f"{msg.subject}\n{msg.body_html}")
            if code:
                return code, f"{folder} | {msg.subject}"
    return "", ""


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--email", required=True)
    ap.add_argument("--file", default=r"C:\Users\Administrator\Desktop\2.txt")
    ap.add_argument("--wait", type=int, default=0, help="轮询等新码的秒数")
    args = ap.parse_args()

    accs = load_accounts(args.file)
    acc = next((a for a in accs
                if a.email.lower() == args.email.lower()), None)
    if acc is None:
        print(f"清单里没有 {args.email}")
        return 1
    box = OutlookMailbox(acc)
    box.refresh_access_token()
    box.connect()
    # 基线：先记下现有邮件，--wait 模式只认新到的
    baseline: set[str] = set()
    for folder in ("INBOX", "Junk"):
        try:
            box.select(folder)
            for u in box.list_uids():
                baseline.add(u.decode() if isinstance(u, bytes) else str(u))
        except Exception:
            continue
    # 先找已有的最新验证码（可能已经到了）
    code, where = find_code(box, set())
    if code:
        print(f"验证码: {code}   （{where}）")
        if args.wait <= 0:
            return 0
    deadline = time.time() + args.wait
    seen = set(baseline)
    while time.time() < deadline:
        time.sleep(4)
        try:
            box.connect()
        except Exception:
            pass
        code, where = find_code(box, seen)
        if code:
            print(f"验证码: {code}   （{where}）", flush=True)
            return 0
        print(f"等新码中… 剩余 {int(deadline - time.time())}s", flush=True)
    print("超时未取到验证码")
    return 2


if __name__ == "__main__":
    sys.exit(main())