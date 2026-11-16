"""端到端验证 iCloud 别名转发：QQ SMTP 发信 → 别名 → 转发回 QQ → IMAP 收。

    python tools/relay_send_test.py --to canvass08.fugues@icloud.com
    python tools/relay_send_test.py --to 别名 --code 654321 --timeout 180

输出会列出这封信里出现的所有地址 / 收件人头，用来确定匹配规则该扫哪个头。
"""

from __future__ import annotations

import argparse
import smtplib
import sys
import time
from email.header import decode_header, make_header
from email.mime.text import MIMEText
from email.utils import formatdate
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.config import get, load_config, to_plain  # noqa: E402
from app.mail.relay_qq import ADDR_RE, ICloudRelayQQProvider, RelayMatchRules  # noqa: E402

MARK = "RELAY-TEST"


def build(cfg):
    ic = get(cfg, "mail.icloud_relay")
    agg = to_plain(get(ic, "aggregator_imap", {}) or {})
    rules = RelayMatchRules.from_config(to_plain(get(ic, "match", {}) or {}) or {})
    prov = ICloudRelayQQProvider(
        relay_addresses_file=get(ic, "relay_addresses_file", ""),
        imap_host=str(agg.get("host") or "imap.qq.com"),
        imap_port=int(agg.get("port") or 993),
        imap_user=str(agg.get("user") or ""),
        imap_password=str(agg.get("password") or ""),
        folder=str(agg.get("folder") or "INBOX"),
        rules=rules,
    )
    return prov, str(agg.get("user") or ""), str(agg.get("password") or "")


def send(user: str, password: str, to: str, code: str) -> str:
    subject = f"{MARK} {int(time.time())}"
    body = (f"Qoder verification code: {code}\n\n"
            f"This mail was sent to {to} and forwarded here.\n"
            f"marker={subject}\n")
    msg = MIMEText(body, "plain", "utf-8")
    msg["Subject"] = subject
    msg["From"] = user
    msg["To"] = to
    msg["Date"] = formatdate(localtime=True)
    with smtplib.SMTP_SSL("smtp.qq.com", 465, timeout=30) as s:
        s.login(user, password)
        s.sendmail(user, [to], msg.as_string())
    print(f"已发送: {user} → {to}  subject={subject}")
    return subject


def watch(prov: ICloudRelayQQProvider, subject_hint: str, alias: str,
          timeout: int) -> int:
    deadline = time.time() + timeout
    print(f"等转发回来（最多 {timeout}s）…")
    while time.time() < deadline:
        try:
            with prov.inbox() as box:
                for msg in reversed(box.recent(40)):
                    subj = msg.subject or ""
                    if subject_hint not in subj:
                        continue
                    blob = " ".join([msg.raw_headers or "",
                                     " ".join(msg.to),
                                     msg.body_text or ""])
                    addrs = sorted({a.lower() for a in ADDR_RE.findall(blob)})
                    print("\nPASS 收到转发")
                    print(f"  subject : {subj}")
                    print(f"  sender  : {msg.sender}")
                    print(f"  to      : {msg.to}")
                    print(f"  头/正文里的全部地址: {addrs}")
                    print(f"  别名命中: {alias.lower() in addrs}")
                    print("\n---- 原始头（前 20 行）----")
                    for line in (msg.raw_headers or "").splitlines()[:20]:
                        print("  " + line[:120])
                    return 0
        except Exception as exc:
            print(f"  轮询异常 {type(exc).__name__}: {exc}")
        time.sleep(8)
    print("FAIL 超时没收到转发：检查 iCloud 设置里该别名是否转发到本 QQ 邮箱")
    return 1


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--to", default="", help="目标别名（默认取池里第一个）")
    ap.add_argument("--code", default="654321")
    ap.add_argument("--timeout", type=int, default=180)
    ap.add_argument("--no-watch", action="store_true", help="只发不等")
    args = ap.parse_args()

    cfg = load_config()
    prov, user, password = build(cfg)
    if not user or not password:
        print("聚合箱账号或授权码为空")
        return 2
    alias = args.to or (prov.identities(1)[0] if prov.identities() else "")
    if not alias:
        print("没有别名，先填 data/icloud_relay.txt")
        return 2

    subject = send(user, password, alias, args.code)
    if args.no_watch:
        return 0
    return watch(prov, MARK, alias, args.timeout)


if __name__ == "__main__":
    raise SystemExit(main())
