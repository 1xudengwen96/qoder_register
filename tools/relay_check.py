"""iCloud 隐藏地址 → QQ 聚合箱 联调工具。

用法：
    # 1) 看连接与最近邮件（确认授权码/文件夹/转发是否到位）
    python tools/relay_check.py

    # 2) 只看地址池
    python tools/relay_check.py --pool

    # 3) 查某个隐藏地址现在有没有验证码
    python tools/relay_check.py --address abcd1234@privaterelay.appleid.com

    # 4) 监听 300 秒，实时报告哪个地址收到了码（联调转发链路用）
    python tools/relay_check.py --watch 300

配置来自 config/config.yaml 的 mail.icloud_relay（含 QQ IMAP 与匹配规则）。
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.config import get, load_config, to_plain  # noqa: E402
from app.mail.relay_qq import (  # noqa: E402
    ICloudRelayQQProvider,
    RelayMatchRules,
    guess_code,
    looks_like_code_mail,
    recipient_candidates,
)


def build(cfg) -> ICloudRelayQQProvider:
    ic = get(cfg, "mail.icloud_relay")
    if not ic:
        raise SystemExit("配置里没有 mail.icloud_relay")
    agg = to_plain(get(ic, "aggregator_imap", {}) or {})
    rules = RelayMatchRules.from_config(
        to_plain(get(ic, "match", {}) or {}) or {})
    return ICloudRelayQQProvider(
        relay_addresses_file=get(ic, "relay_addresses_file", ""),
        imap_host=str(agg.get("host") or "imap.qq.com"),
        imap_port=int(agg.get("port") or 993),
        imap_user=str(agg.get("user") or ""),
        imap_password=str(agg.get("password") or ""),
        folder=str(agg.get("folder") or "INBOX"),
        imap_id=str(agg.get("imap_id") or ""),
        quota_total=int(get(ic, "quota_total", 500)),
        rules=rules,
    )


def show_pool(p: ICloudRelayQQProvider) -> None:
    addrs = p.identities()
    print(f"地址池: {len(addrs)} 个  (文件 {p.pool.path})")
    for i, a in enumerate(addrs, 1):
        print(f"  {i:03d}  {a}")
    if not addrs:
        print("  （空：把 data/icloud_relay.txt 里 # 开头的行替换成真实地址）")


def show_inbox(p: ICloudRelayQQProvider, limit: int) -> None:
    print(f"聚合箱: {p.imap_user or '(未填 user)'} @ {p.imap_host}:{p.imap_port}"
          f" / {p.folder}")
    print(f"规则: sender={p.rules.sender_allow} "
          f"subject={p.rules.subject_contains} code={p.rules.code_regex}")
    try:
        with p.inbox() as box:
            uids = box.uid_list()
            print(f"连接 OK，共 {len(uids)} 封，最近 {min(limit, len(uids))} 封：")
            for msg in reversed(box.recent(limit)):
                hit = sorted(a for a in recipient_candidates(
                    msg, msg.raw_headers, p.rules)
                    if a.endswith("@privaterelay.appleid.com"))
                code = guess_code(msg, p.rules)
                flag = "码" if looks_like_code_mail(msg, p.rules) else "  "
                print(f"  [{flag}] uid={msg.uid:>5} "
                      f"{(msg.sender or '')[:38]:38s} | "
                      f"{(msg.subject or '')[:34]:34s} | "
                      f"relay={hit[:2]} code={code}")
    except Exception as exc:
        print(f"连接失败: {type(exc).__name__}: {exc}")
        print("检查：QQ 邮箱是否开了 IMAP、密码是否填的「授权码」、"
              "以及是否需要在 QQ 邮箱里开启「其他邮箱转发」")


def check_address(p: ICloudRelayQQProvider, addr: str) -> None:
    code = p.find_code(addr)
    print(f"{addr}: {'验证码 ' + code if code else '没找到验证码邮件'}")


def watch(p: ICloudRelayQQProvider, seconds: int, interval: int) -> None:
    addrs = p.identities()
    if not addrs:
        print("地址池为空，先填 data/icloud_relay.txt")
        return
    print(f"监听 {seconds}s，观察 {len(addrs)} 个地址…")
    seen: dict[str, str] = {}
    deadline = time.time() + seconds
    while time.time() < deadline:
        try:
            with p.inbox() as box:
                for msg in box.recent(p.rules.lookback):
                    if not looks_like_code_mail(msg, p.rules):
                        continue
                    cands = recipient_candidates(msg, msg.raw_headers, p.rules)
                    for a in addrs:
                        if a.lower() in cands and a not in seen:
                            code = guess_code(msg, p.rules)
                            seen[a] = code
                            print(f"  + {a} ← {code}  "
                                  f"({(msg.subject or '')[:40]})", flush=True)
        except Exception as exc:
            print(f"  轮询异常 {type(exc).__name__}: {exc}", flush=True)
        time.sleep(interval)
    print(f"\n本轮命中 {len(seen)}/{len(addrs)}：")
    for a in addrs:
        print(f"  {'OK ' if a in seen else '-- '} {a} {seen.get(a, '')}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default="")
    ap.add_argument("--pool", action="store_true", help="只列地址池")
    ap.add_argument("--address", default="", help="查某个地址的验证码")
    ap.add_argument("--watch", type=int, default=0, help="监听秒数")
    ap.add_argument("--limit", type=int, default=10, help="显示最近几封")
    ap.add_argument("--interval", type=int, default=8)
    args = ap.parse_args()

    cfg = load_config(args.config or None)
    p = build(cfg)

    if args.pool:
        show_pool(p)
        return 0
    show_pool(p)
    if args.address:
        check_address(p, args.address)
        return 0
    if args.watch:
        show_inbox(p, 3)
        watch(p, args.watch, args.interval)
        return 0
    show_inbox(p, args.limit)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
