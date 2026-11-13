"""CLI 入口：python -m app.cli <command>"""

from __future__ import annotations

import argparse
import csv
import json
import sys
import time
from pathlib import Path

from app.config import get, load_config
from app.db import create_all, get_session, init_engine, session_scope
from app.logging_setup import setup
from app.mail.factory import build_mail_layer
from app.models import (Account, AccountState, MailIdentity as MailRow,
                        MailProviderEnum, MailStatus)
from app.stock.exporter import StockManager

log = setup()


# ---------------------------------------------------------------- 建库
def cmd_init_db(args) -> int:
    cfg = load_config(args.config)
    url = get(cfg, "storage.url", "sqlite:///data/qoder.db")
    init_engine(url)
    create_all(drop=args.force)
    print(f"数据库就绪：{url}{'（已重建）' if args.force else ''}")
    return 0


# ---------------------------------------------------------------- 邮箱导入
def cmd_mail_import(args) -> int:
    cfg = load_config(args.config)
    init_engine(get(cfg, "storage.url", "sqlite:///data/qoder.db"))
    create_all()
    path = Path(args.file)
    if not path.exists():
        print(f"文件不存在：{path}")
        return 1

    prov_map = {
        "hotmail": MailProviderEnum.HOTMAIL,
        "outlook_tag": MailProviderEnum.OUTLOOK_TAG,
        "icloud_relay": MailProviderEnum.ICLOUD_RELAY,
    }
    prov = prov_map[args.provider]

    records: list[tuple[str, str, str]] = []   # (address, secret, imap_host)
    if args.provider == "hotmail":
        with path.open("r", encoding="utf-8-sig", newline="") as fh:
            for row in csv.DictReader(fh):
                addr = (row.get("email") or "").strip()
                if not addr:
                    continue
                records.append((
                    addr,
                    (row.get("password") or "").strip(),
                    (row.get("imap_host") or "outlook.office365.com").strip(),
                ))
    else:
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.split("#", 1)[0].strip()
            if line:
                records.append((line, "", ""))

    added = skipped = 0
    with session_scope() as s:
        for addr, secret, imap in records:
            if s.query(MailRow).filter(MailRow.address == addr.lower()).first():
                skipped += 1
                continue
            parent = None
            if args.provider == "outlook_tag" and "+" in addr and "@" in addr:
                local, _, dom = addr.partition("@")
                parent = f"{local.split('+', 1)[0]}@{dom}"
            note_bits = []
            if secret:
                note_bits.append(f"secret={secret}")
            if imap:
                note_bits.append(f"imap={imap}")
            s.add(MailRow(provider=prov, address=addr, parent_account=parent,
                          status=MailStatus.POOL,
                          note=";".join(note_bits) or None))
            added += 1
    print(f"导入 {added} 条，跳过 {skipped} 条（已存在）")
    return 0


# ---------------------------------------------------------------- 邮箱池状态
def cmd_mail_pool(args) -> int:
    cfg = load_config(args.config)
    init_engine(get(cfg, "storage.url", "sqlite:///data/qoder.db"))
    create_all()

    class _DB:
        get_session = staticmethod(get_session)

    layer = build_mail_layer(cfg, _DB)
    print("=== 邮件源健康 ===")
    for h in layer.health():
        pct = 100 * h.ratio()
        flag = "" if h.ok else "  <== 已耗尽"
        print(f"- {h.name:<14} {h.remaining:>4}/{h.total:<4} ({pct:5.1f}%)  {h.detail}{flag}")

    with session_scope() as s:
        rows = s.query(MailRow).all()
        stat: dict[str, dict[str, int]] = {}
        for r in rows:
            k = r.provider.value if hasattr(r.provider, "value") else str(r.provider)
            st = r.status.value if hasattr(r.status, "value") else str(r.status)
            stat.setdefault(k, {})
            stat[k][st] = stat[k].get(st, 0) + 1
    print("\n=== 地址库存 ===")
    if not stat:
        print("- 空，先跑 mail:import")
    for prov, counts in stat.items():
        detail = " ".join(f"{k}:{v}" for k, v in sorted(counts.items()))
        print(f"- {prov:<14} {detail}")
    return 0


# ---------------------------------------------------------------- 浏览器/打码自检
def cmd_env_check(args) -> int:
    cfg = load_config(args.config)
    from app.net.captcha_solver import build_solver
    from app.register.fingerprint_engine import build_fingerprint_engine

    print("=== 指纹浏览器后端 ===")
    engine = build_fingerprint_engine(cfg)
    ok, detail = engine.health()
    print(f"- {engine.backend:<14} {'OK ' if ok else 'FAIL'} {detail}")

    print("\n=== 打码平台 ===")
    solver = build_solver(cfg)
    if not solver.providers:
        print("- 未配置任何打码供应商（captcha.providers.*.api_key）")
    for name, p in solver.providers.items():
        kinds = [k for k in ("recaptcha_v2", "recaptcha_v3", "hcaptcha",
                             "turnstile", "image") if p.supports(k)]
        print(f"- {name:<12} 支持 {'/'.join(kinds)}  单价 {p.price}")

    print("\n=== 代理池 ===")
    from app.net.proxy import ProxyPool
    pool = ProxyPool.from_file(get(cfg, "proxy.file", "data/proxies.txt"))
    print(f"- {pool.summary()}")

    print("\n=== 设备重置（只读自检）===")
    from app.device.qoder_reset import QoderIdentityReset
    r = QoderIdentityReset(app_dir=get(cfg, "device.app_dir", None))
    st = r.status()
    for k, v in st.items():
        if k == "id_files":
            present = [n for n, e in v.items() if e]
            print(f"- {k}: {len(present)}/{len(v)} 个存在 {present[:5]}")
        else:
            print(f"- {k}: {v}")
    return 0


# ---------------------------------------------------------------- 设备重置
def cmd_device_reset(args) -> int:
    cfg = load_config(args.config)
    from app.device.qoder_reset import ALL_STEPS, QoderIdentityReset, ResetOptions

    reset = QoderIdentityReset(
        logger=log,
        app_dir=get(cfg, "device.app_dir", None),
        backup_dir=get(cfg, "device.backup_dir", "data/device_backup"),
    )
    steps = tuple(args.steps.split(",")) if args.steps else tuple(ALL_STEPS)
    report = reset.run(ResetOptions(
        steps=steps,
        dry_run=not args.execute,
        preserve_chat=args.keep_chat,
        fake_hardware=args.fake_hardware,
        timezone=args.timezone,
        backup=not args.no_backup,
    ))
    print(json.dumps(report.as_dict(), ensure_ascii=False, indent=2))
    return 0 if report.ok else 2


def cmd_device_restore(args) -> int:
    cfg = load_config(args.config)
    from app.device.qoder_reset import QoderIdentityReset
    reset = QoderIdentityReset(app_dir=get(cfg, "device.app_dir", None))
    for line in reset.restore(args.backup):
        print(line)
    return 0


# ---------------------------------------------------------------- 跑流水线
def cmd_run_one(args) -> int:
    cfg = load_config(args.config)
    init_engine(get(cfg, "storage.url", "sqlite:///data/qoder.db"))
    create_all()
    from app.orchestrator import Pipeline

    rep = Pipeline(cfg).run_one(region=args.region, dry_run_device=args.dry_run_device)
    print(json.dumps({
        "ok": rep.ok, "account_id": rep.account_id, "email": rep.email,
        "stage": rep.stage, "message": rep.message, "credits": rep.credits,
        "captcha": rep.captcha_provider,
    }, ensure_ascii=False, indent=2))
    return 0 if rep.ok else 2


def cmd_run_loop(args) -> int:
    cfg = load_config(args.config)
    init_engine(get(cfg, "storage.url", "sqlite:///data/qoder.db"))
    create_all()
    from app.orchestrator import Pipeline

    reps = Pipeline(cfg).run_loop(args.n, args.sleep)
    ok = sum(1 for r in reps if r.ok)
    print(f"共 {len(reps)} 轮，成功 {ok}，失败 {len(reps) - ok}")
    return 0


def cmd_run_concurrent(args) -> int:
    """注册并行 + 领取串行。"""
    cfg = load_config(args.config)
    init_engine(get(cfg, "storage.url", "sqlite:///data/qoder.db"))
    create_all()
    from app.concurrent import ConcurrentPipeline

    pipe = ConcurrentPipeline(
        cfg,
        register_workers=args.workers,
        slot_id=args.slot,
        lock_path=args.lock,
    )
    # 预检：设备锁是否被占
    if not pipe.device_lock.acquire(timeout=0):
        print("设备锁被占用（另一个领取进程在跑），并发领取互斥，请等它结束或加 --slot 区分机器")
        return 3
    pipe.device_lock.release()

    stats = pipe.run(total=args.total, region=args.region)
    print("\n=== 结果 ===")
    print(f"注册成功 {stats.registered}，失败 {stats.register_failed}")
    print(f"领取成功 {stats.claimed}，失败 {stats.claim_failed}")
    print(f"总积分 {stats.credits}")
    print(f"队列待领 {pipe.queue.pending()}")
    return 0


def cmd_queue(args) -> int:
    """查看/维护待领队列。"""
    cfg = load_config(args.config)
    init_engine(get(cfg, "storage.url", "sqlite:///data/qoder.db"))
    create_all()
    from app.concurrent import AccountQueue

    q = AccountQueue(args.slot)
    if args.reclaim:
        n = q.reclaim_stale(args.stale_minutes)
        print(f"回收卡住的 claiming 账号 {n} 个 → verified")

    with session_scope() as s:
        rows = (s.query(Account)
                .filter(Account.state == AccountState.VERIFIED)
                .order_by(Account.id.asc()).all())
        print(f"待领队列：{len(rows)} 个")
        for a in rows[:20]:
            print(f"  #{a.id:<6} {a.email}")
        if len(rows) > 20:
            print(f"  ... 还有 {len(rows) - 20} 个")

        stuck = (s.query(Account)
                 .filter(Account.state == AccountState.CLAIMING).count())
        print(f"卡在 claiming：{stuck} 个（进程崩溃残留，用 --reclaim 回收）")
    return 0


def cmd_device_lock(args) -> int:
    """查看/释放设备锁。"""
    from app.concurrent import DeviceLock

    lock = DeviceLock(args.lock)
    if args.release:
        lock.release()
        print(f"已释放 {args.lock}")
        return 0
    info = lock._read()
    if not info:
        print(f"{args.lock} 未被占用")
        return 0
    pid = info.get("pid")
    age = int(time.time() - info.get("ts", 0))
    stale = lock._is_stale(info)
    print(f"持有者 pid={pid} 已持锁 {age} 秒 {'（僵尸锁）' if stale else '（活跃）'}")
    if args.steal and stale:
        lock.release()
        print("已清除僵尸锁")
    return 0


# ---------------------------------------------------------------- 导出
def cmd_export_stock(args) -> int:
    cfg = load_config(args.config)
    init_engine(get(cfg, "storage.url", "sqlite:///data/qoder.db"))
    create_all()
    with session_scope() as s:
        mgr = StockManager(s)
        n = mgr.export_csv(args.out) if args.format == "csv" else \
            mgr.export_json(args.out, include_token=args.with_token)
        print(f"导出 {n} 条 → {args.out}")
        print(mgr.summary())
        soon = mgr.expiring_soon(3)
        if soon:
            print(f"3 天内到期 {len(soon)} 个号：")
            for r in soon[:10]:
                print(f"  {r.email}  {r.days_left} 天  {r.credits} Credits  {r.grade}")
    return 0


def cmd_stats(args) -> int:
    cfg = load_config(args.config)
    init_engine(get(cfg, "storage.url", "sqlite:///data/qoder.db"))
    create_all()
    with session_scope() as s:
        accounts = s.query(Account).all()
        total = len(accounts)
        by_state: dict[str, int] = {}
        for a in accounts:
            k = a.state.value if hasattr(a.state, "value") else str(a.state)
            by_state[k] = by_state.get(k, 0) + 1
        claimed = [a for a in accounts if a.credits > 0]
        total_credits = sum(a.credits for a in claimed)
        tag_hits = sum(1 for a in accounts if "tag_normalized" in a.flag_list())
        dropped = sum(1 for a in accounts if "mail_dropped" in a.flag_list())
        dirty = sum(1 for a in accounts if "proxy_dirty" in a.flag_list())

        print("=== 账号状态 ===")
        for k, v in sorted(by_state.items()):
            print(f"- {k:<12} {v}")
        print(f"\n总账号 {total}，成功领取 {len(claimed)}，总积分 {total_credits}")
        if total:
            print(f"成功率 {len(claimed) / total * 100:.1f}%")
        print(f"tag 归一化 {tag_hits}  丢信 {dropped}  代理脏 {dirty}")
        print(StockManager(s).summary())
    return 0


# ---------------------------------------------------------------- 解析
def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="qoder_register",
                                description="Qoder 邮箱注册/领取流水线")
    p.add_argument("--config", default=None, help="配置文件路径")
    sub = p.add_subparsers(dest="command", required=True)

    s = sub.add_parser("init-db", help="建库建表")
    s.add_argument("--force", action="store_true")
    s.set_defaults(func=cmd_init_db)

    s = sub.add_parser("mail:import", help="导入邮箱身份清单")
    s.add_argument("--provider", required=True,
                   choices=["hotmail", "outlook_tag", "icloud_relay"])
    s.add_argument("--file", required=True)
    s.set_defaults(func=cmd_mail_import)

    s = sub.add_parser("mail:pool", help="邮箱池健康与库存")
    s.set_defaults(func=cmd_mail_pool)

    s = sub.add_parser("env:check", help="指纹浏览器/打码/代理/设备自检")
    s.set_defaults(func=cmd_env_check)

    s = sub.add_parser("run:one", help="跑一轮完整循环")
    s.add_argument("--region", default=None)
    s.add_argument("--dry-run-device", action="store_true",
                   help="设备重置只预演不执行")
    s.set_defaults(func=cmd_run_one)

    s = sub.add_parser("run:loop", help="批量跑（全串行）")
    s.add_argument("--n", type=int, default=10)
    s.add_argument("--sleep", type=int, default=90)
    s.set_defaults(func=cmd_run_loop)

    s = sub.add_parser("run:concurrent", help="注册并行 + 领取串行")
    s.add_argument("--total", type=int, default=50, help="目标账号数")
    s.add_argument("--workers", type=int, default=3, help="注册并发数（建议 3~5）")
    s.add_argument("--region", default=None)
    s.add_argument("--slot", default="local", help="机器槽位标识，多机部署时每台不同")
    s.add_argument("--lock", default="data/.device.lock")
    s.set_defaults(func=cmd_run_concurrent)

    s = sub.add_parser("queue", help="查看/维护待领队列")
    s.add_argument("--slot", default="local")
    s.add_argument("--reclaim", action="store_true", help="回收卡在 claiming 的账号")
    s.add_argument("--stale-minutes", type=int, default=30)
    s.set_defaults(func=cmd_queue)

    s = sub.add_parser("device:lock", help="查看/释放设备独占锁")
    s.add_argument("--lock", default="data/.device.lock")
    s.add_argument("--release", action="store_true")
    s.add_argument("--steal", action="store_true", help="清除僵尸锁")
    s.set_defaults(func=cmd_device_lock)

    s = sub.add_parser("device:reset", help="设备标识重置（默认 dry-run）")
    s.add_argument("--execute", action="store_true", help="真执行")
    s.add_argument("--steps", default=None,
                   help="逗号分隔：kill,machine_id,telemetry,cache,identity,advanced,login,hardware,chat")
    s.add_argument("--keep-chat", action="store_true")
    s.add_argument("--fake-hardware", action="store_true")
    s.add_argument("--timezone", default="America/New_York")
    s.add_argument("--no-backup", action="store_true")
    s.set_defaults(func=cmd_device_reset)

    s = sub.add_parser("device:restore", help="从备份回滚")
    s.add_argument("--backup", required=True)
    s.set_defaults(func=cmd_device_restore)

    s = sub.add_parser("export:stock", help="导出库存")
    s.add_argument("--format", choices=["csv", "json"], default="csv")
    s.add_argument("--out", default="data/stock.csv")
    s.add_argument("--with-token", action="store_true")
    s.set_defaults(func=cmd_export_stock)

    s = sub.add_parser("stats", help="统计")
    s.set_defaults(func=cmd_stats)

    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
