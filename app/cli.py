"""CLI 入口：python -m app.cli <command>"""

from __future__ import annotations

import argparse
import csv
import json
import sys
import time
from datetime import datetime
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
    # 常驻收信进程：自检时拉起，outlook/hotmail 长连接保持在线
    from app.mail import watcher
    print(f"常驻收信进程 pid={watcher.ensure_watcher()}")
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

    # 身份模式：默认不传 = 原随机方案；browser = 匹配注册段浏览器画像
    profile = None
    mode = (args.identity_mode
            or str(get(cfg, "device.identity_mode", "") or "")).strip().lower()
    if mode in ("browser", "match", "fingerprint_browser"):
        from app.device.identity_profile import profile_from_browser
        from app.register.fingerprint_engine import make_profile

        seed = args.seed or "manual"
        profile = profile_from_browser(
            make_profile(region=(args.region or "us").lower(), seed=seed))
        log.info("设备身份=浏览器匹配画像 %s", profile.brief())

    report = reset.run(ResetOptions(
        steps=steps,
        dry_run=not args.execute,
        preserve_chat=args.keep_chat,
        fake_hardware=args.fake_hardware,
        timezone=args.timezone,
        backup=not args.no_backup,
        profile=profile,
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

    pipe = Pipeline(cfg)
    try:
        rep = pipe.run_one(region=args.region, dry_run_device=args.dry_run_device)
    finally:
        pipe.shutdown_browsers()
    print(json.dumps({
        "ok": rep.ok, "account_id": rep.account_id, "email": rep.email,
        "stage": rep.stage, "message": rep.message, "credits": rep.credits,
        "captcha": rep.captcha_provider,
    }, ensure_ascii=False, indent=2))
    return 0 if rep.ok else 2


# ---------------------------------------------------------------- 人工接管
def cmd_manual_go(args) -> int:
    """人工模式接管通知：写入 data/.manual_go，等待中的注册流程即继续。"""
    p = Path("data/.manual_go")
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(str(time.time()), encoding="utf-8")
    print("已写入接管通知 data/.manual_go；等待中的【人工模式】流程将继续")
    return 0


def cmd_reg_one(args) -> int:
    """只跑注册+邮箱验证（单窗口测试用）：不碰设备、不碰客户端。

    与 run:one 的区别：到 verified 就停，不跑设备重置和领取，
    适合链路调试时单窗口验证注册段改动。
    """
    cfg = load_config(args.config)
    init_engine(get(cfg, "storage.url", "sqlite:///data/qoder.db"))
    create_all()
    from app.orchestrator import Pipeline

    pipe = Pipeline(cfg)
    try:
        rep = pipe.register_only(region=args.region)
    finally:
        pipe.shutdown_browsers()
    print(json.dumps({
        "ok": rep.ok, "account_id": rep.account_id, "email": rep.email,
        "stage": rep.stage, "message": rep.message,
        "captcha": rep.captcha_provider,
    }, ensure_ascii=False, indent=2))
    return 0 if rep.ok else 2


def cmd_run_loop(args) -> int:
    cfg = load_config(args.config)
    init_engine(get(cfg, "storage.url", "sqlite:///data/qoder.db"))
    create_all()
    from app.orchestrator import Pipeline

    pipe = Pipeline(cfg)
    try:
        reps = pipe.run_loop(args.n, args.sleep)
    finally:
        pipe.shutdown_browsers()
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


def cmd_pool_status(args) -> int:
    """窗口池状态：探测/补齐/占用一览（--ensure 不足时补齐）。"""
    cfg = load_config(args.config)
    from app.register.bb_pool import build_env_pool
    from app.register.bitbrowser import BitBrowserClient, resolve_base

    client = BitBrowserClient(resolve_base(cfg))
    pool = build_env_pool(cfg, client)
    if pool is None:
        print("窗口池未启用（browser.env_pool.enabled=false，走每号新建旧逻辑）")
        return 0
    if args.ensure:
        pool.ensure()
    if args.close:
        print(f"已关闭 {pool.close_all()} 个池窗口")
    print(json.dumps(pool.status(), ensure_ascii=False, indent=2))
    return 0


def cmd_web(args) -> int:
    """启动 Web 控制台（配置/状态/启停机器人）。"""
    from app.webui import serve

    serve(port=args.port)
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


# ---------------------------------------------------------------- 邮箱清单体检
def cmd_mail_watch(args) -> int:
    """常驻收信进程前台运行；--stop 杀掉已有进程。"""
    from app.mail import watcher
    if args.stop:
        print(f"已停止常驻收信进程 pid={watcher.stop_watcher()}")
        return 0
    watcher.run_watcher()
    return 0


def cmd_mail_file(args) -> int:
    """体检/入库 data/emails.txt（email----pass----clientId----refreshToken）。"""
    cfg = load_config(args.config)
    init_engine(get(cfg, "storage.url", "sqlite:///data/qoder.db"))
    create_all()

    from app.mail.outlook_file import audit

    path = args.file or get(cfg, "mail.outlook_file", "data/emails.txt")
    rep = audit(path)
    print("=== 清单体检 ===")
    for line in rep.lines():
        print(f"- {line}")

    if rep.bad_lines:
        print("\n格式错误行（应为 4 段或 6 段，用 ---- 分隔）：")
        lines = Path(path).read_text(encoding="utf-8", errors="ignore").splitlines()
        for n in rep.bad_lines[:10]:
            print(f"  第 {n} 行: {lines[n - 1][:80]}")

    if rep.addresses:
        print(f"\n前 5 条：")
        for a in rep.addresses[:5]:
            print(f"  {a}")
        if len(rep.addresses) > 5:
            print(f"  ... 还有 {len(rep.addresses) - 5} 条")

    if args.verify:
        from app.mail.outlook_oauth import OutlookMailbox, load_accounts

        print("\n=== 令牌实测（刷 OAuth + 连 IMAP）===")
        accs = load_accounts(path)
        ok_n = 0
        for i, acc in enumerate(accs, 1):
            box = OutlookMailbox(acc)
            try:
                box.refresh_access_token()
                box.connect()
                box.select("INBOX")
                n = len(box.list_uids())
                print(f"{i:2d}. {acc.email:38s} OK   INBOX {n} 封")
                ok_n += 1
            except Exception as exc:  # noqa: BLE001
                print(f"{i:2d}. {acc.email:38s} FAIL {type(exc).__name__}: "
                      f"{str(exc)[:70]}")
            finally:
                try:
                    box.close()
                except Exception:  # noqa: BLE001
                    pass
            time.sleep(0.3)
        print(f"\n令牌可用 {ok_n}/{len(accs)}")
        if ok_n < len(accs):
            return 2

    if not args.import_to_db:
        print("\n（加 --import 把地址写进数据库邮箱池）")
        return 0 if rep.total else 2
    with session_scope() as s:
        added = skipped = 0
        for addr in rep.addresses:
            low = addr.lower()
            if s.query(MailRow).filter(MailRow.address == low).first():
                skipped += 1
                continue
            s.add(MailRow(
                provider=MailProviderEnum.OUTLOOK_FILE,
                address=addr,
                parent_account=None,
                status=MailStatus.POOL,
                note=f"source={path}",
            ))
            added += 1
    print(f"\n入库 {added} 条，跳过 {skipped} 条（已存在）")
    return 0


# ---------------------------------------------------------------- 代理续期
def cmd_proxy_gen(args) -> int:
    """按模板批量生成新会话代理（ipdeep 这类按 session 计费的入口）。"""
    cfg = load_config(args.config)
    from app.net.proxy import ProxyPool, append_to_file, extend_from_template

    tpl = args.template or get(cfg, "proxy.extend.template", "")
    if not tpl:
        print("没有模板：配置 proxy.extend.template 或传 --template")
        return 2
    region = (args.region or get(cfg, "region", "us")).lower()
    user = (args.user or get(cfg, "proxy.extend.user", "")).replace("{region}", region)
    password = args.password or str(get(cfg, "proxy.extend.password", "") or "")
    ttl = int(args.ttl or get(cfg, "proxy.extend.ttl", 5))
    count = int(args.count or get(cfg, "proxy.extend.count", 20))

    print(f"模板: {tpl}")
    print(f"账号: {user}  地区={region} 粘性={ttl}分钟 目标={count}条 验证={'否' if args.no_verify else '是'}")
    raws = extend_from_template(
        template=tpl, user=user, password=password, ttl=ttl, count=count,
        region=region, verify=not args.no_verify,
        concurrency=args.workers, timeout=args.timeout,
    )
    print(f"生成并存活: {len(raws)} 条")
    path = args.file or get(cfg, "proxy.file", "data/proxies.txt")
    if args.replace:
        target = Path(path)
        if target.exists():
            target.rename(target.with_suffix(target.suffix + ".bak"))
        target.write_text(chr(10).join(raws) + chr(10), encoding="utf-8")
        print(f"已覆盖写入 {path}（旧文件存为 .bak）")
    else:
        added = append_to_file(path, raws)
        print(f"追加写入 {path}：新增 {added} 条")
    pool = ProxyPool.from_file(path)
    print(f"代理池: {pool.summary()}")
    return 0 if raws else 2


def cmd_proxy_state(args) -> int:
    """看一条一用账本：文件里多少条、用了多少、还剩多少。"""
    cfg = load_config(args.config)
    from app.net.proxy import ProxyLedger, ProxyPool

    ledger = ProxyLedger(get(cfg, "proxy.state_file", "data/.proxy_state.json"))
    pool = ProxyPool.from_file(get(cfg, "proxy.file", "data/proxies.txt"))
    if args.prune:
        n = ledger.prune(keep_hours=args.keep_hours)
        print(f"清理 {n} 条过期账目（保留 {args.keep_hours} 小时）")
    st = ledger.stats()
    raws = [p.raw for p in pool.proxies]
    remain = ledger.remaining(raws)
    print("=== 代理账本 ===")
    print(f"- 文件      : {get(cfg, 'proxy.file', '')}")
    print(f"- 文件条数  : {len(raws)}")
    print(f"- 已消耗    : {st['total']}")
    print(f"- 本文件剩余: {remain}")
    if st["by_note"]:
        detail = " ".join(f"{k}:{v}" for k, v in sorted(st["by_note"].items()))
        print(f"- 消耗明细  : {detail}")
    entries = ledger._read()["entries"]
    recent = sorted(entries.items(), key=lambda kv: float(kv[1].get("used_at") or 0),
                    reverse=True)[:5]
    if recent:
        print("\n最近 5 条:")
        for raw, meta in recent:
            print(f"  {meta.get('label', raw)[:44]:46s} env={meta.get('env','-'):8s} {meta.get('note','')}")
    return 0 if remain else 3


# ---------------------------------------------------------------- 单号领取
def cmd_claim_one(args) -> int:
    """单账号领取：登录（可选自动）→ 抓 token → 核对额度 → 入库 → 落 txt。"""
    cfg = load_config(args.config)
    init_engine(get(cfg, "storage.url", "sqlite:///data/qoder.db"))
    create_all()
    from app.claim.claimer import Claimer
    from app.stock.txt_export import export_all

    email = args.email
    password = args.password
    if not email:
        with session_scope() as s:
            row = (s.query(Account)
                   .filter(Account.state.in_([AccountState.VERIFIED, AccountState.REGISTERED]))
                   .order_by(Account.id.asc()).first())
            if row:
                email, password = row.email, row.password or ""
    if not email:
        print("没有可领取的账号：用 --email/--password 指定，或先跑注册")
        return 2
    print(f"=== 领取 {email} ===")

    claimer = Claimer(
        client_path=get(cfg, "claim.client_path", ""),
        login_mode="cdp" if args.login else "inherit",
        wait_credit_timeout=int(get(cfg, "claim.wait_credit_timeout", 120)),
        capture_token=True,
        credit_probe_url=get(cfg, "claim.credit_probe_url", None) or None,
    )
    print(f"客户端: {claimer.client_path}")

    ui = None
    login_state: dict = {}
    if args.login:
        from app.claim.ui_login import make_ui_login

        # 与 orchestrator 领取段（claim.login_mode=cdp）同一套半自动登录。
        # 一一对应：优先用注册环境名 acct{id}（查 db 拿账号 id）
        with session_scope() as s:
            _row = (s.query(Account).filter(Account.email == email)
                    .order_by(Account.id.asc()).first())
            _env = f"acct{_row.id}" if _row else f"login_{args.env_name or 'one'}"
        ui, login_state = make_ui_login(
            cfg, email, password,
            env_name=_env,
            region=get(cfg, "region", "us"),
            link_timeout=args.link_timeout,
            login_timeout=args.login_timeout,
            artifacts_dir=get(cfg, "storage.artifacts_dir", "data/artifacts"),
        )

    res = claimer.perform(email, password, ui_login=ui)

    # 一一对应：领取/额度兜底优先复用注册环境 acct{id}（同指纹代理 IP，
    # cookie 在）；查不到账号（外部导入）才退回 login_* 命名环境
    with session_scope() as s:
        _row = s.query(Account).filter(Account.email == email).first()
        _acct_id = _row.id if _row else None
    env_base = (f"acct{_acct_id}" if _acct_id
                else f"login_{args.env_name or 'one'}")

    # 登录会话里已经读到额度就直接用；否则再开窗口读一次
    from app.claim.claimer import apply_credits_fallback

    src = apply_credits_fallback(
        res, login_state, cfg,
        env_name=env_base,
        region=get(cfg, "region", "us"),
    )
    if src == "login-session":
        print(f"登录会话额度: {login_state.get('detail')} 计划={res.plan}")
    elif src == "account-page":
        print(f"账号页额度: {login_state.get('_page')} 计划={res.plan}")
        if res.credits <= 0:
            print("（还没在客户端领取？权益可能尚未到账）")

    print(json.dumps({
        "ok": res.ok, "credits": res.credits, "plan": res.plan,
        "token_source": res.token_source, "error": res.error,
        "expire_at": res.expire_at.isoformat(sep=" ", timespec="seconds") if res.expire_at else "",
    }, ensure_ascii=False, indent=1))

    with session_scope() as s:
        acc = s.query(Account).filter(Account.email == email).first()
        if acc is None:
            acc = Account(email=email, password=password, username="", state=AccountState.NEW)
            s.add(acc)
            s.flush()
        if res.token:
            acc.token_json = json.dumps(res.token, ensure_ascii=False)
        if res.ok:
            # token 已入库 = 客户端登录已生效；credits 读不到不再卡 CLAIMED——
            # 记 credits_unknown 旗标，库存按 0 额度进 D 级，可人工复核
            acc.state = AccountState.STOCK
            acc.credits = res.credits
            acc.plan = res.plan or acc.plan
            acc.trial_expire_at = res.expire_at
            acc.claimed_at = datetime.now()
            if res.credits <= 0:
                acc.add_flag("credits_unknown")
        rep = export_all(s, get(cfg, "storage.output_dir", "data/output"))

    print(f"账号已更新（状态 {acc.state.value}），输出: {rep.files.get('accounts')}")
    return 0 if res.ok else 2


# ---------------------------------------------------------------- 文本导出
def cmd_export_txt(args) -> int:
    """把账号 / token / 统计各写一个 txt（全量重写）。"""
    cfg = load_config(args.config)
    init_engine(get(cfg, "storage.url", "sqlite:///data/qoder.db"))
    create_all()
    from app.stock.txt_export import export_all

    out_dir = args.dir or get(cfg, "storage.output_dir", "data/output")
    with session_scope() as s:
        rep = export_all(s, out_dir)
    print("=== 文本导出 ===")
    for name, path in rep.files.items():
        size = Path(path).stat().st_size if Path(path).exists() else 0
        print(f"- {name:9s} {path}  ({size} bytes)")
    print(f"- 账号 {rep.accounts} 条，含 token {rep.tokens} 条")
    return 0


# ---------------------------------------------------------------- 客户端路径探测
def cmd_client_find(args) -> int:
    """列出所有候选并给出选中项（不写死任何机器路径）。"""
    cfg = load_config(args.config)
    from app.claim.client_path import describe, find_client

    override = args.path or (get(cfg, "claim.client_path", "") or "")
    print(describe(override=override))
    path, chosen, _cands = find_client(override=override)
    if path is None:
        print("\n没找到客户端：确认 Qoder 客户端已安装，或用 --path / claim.client_path 指定")
        return 2
    print(f"\n结论：用 {path}")
    if chosen and "launcher" in chosen.path.name.lower():
        print("      这是启动器入口，会自己拉起 .qoder-versions 里的本体")
    return 0


# ---------------------------------------------------------------- 比特代理联调
def cmd_browser_probe(args) -> int:
    """把本地代理文件里的代理写进比特浏览器环境，可选直接启动校验 CDP。"""
    cfg = load_config(args.config)
    from app.register import bb_cdp
    from app.register.bitbrowser import BitBrowserClient
    from app.net.proxy import ProxyPool

    pool = ProxyPool.from_file(get(cfg, "proxy.file", "proxies-cn-sticky.txt"))
    if args.proxy:
        from app.net.proxy import parse_line

        one = parse_line(args.proxy)
        if one is None:
            print(f"--proxy 解析失败：{args.proxy}")
            return 2
        pool = ProxyPool([one])
    print(f"=== 代理池 ===\n- {pool.summary()}")
    if not pool.proxies:
        print("代理池为空，检查 proxy.file")
        return 2

    if args.check_all:
        import concurrent.futures as cf

        def _probe(px):
            return px, pool.check(px, timeout=args.timeout)

        with cf.ThreadPoolExecutor(max_workers=max(1, args.workers)) as ex:
            rows = list(ex.map(_probe, pool.proxies))
        alive = sum(1 for _, ok in rows if ok)
        for px, ok in rows:
            print(f"  {'OK  ' if ok else 'DEAD'} {px.label}")
        print(f"\n- 存活 {alive}/{len(rows)}")
        return 0 if alive else 2
    from app.net.proxy import ProxyLedger, acquire_once

    ledger = ProxyLedger(get(cfg, "proxy.state_file", "data/.proxy_state.json"))
    if args.reuse:
        idx = args.proxy_index % len(pool.proxies)
        px = pool.proxies[idx]
        print(f"- 复用 #{idx} {px.label}（不消耗账本）")
    else:
        px = acquire_once(pool, ledger, region=(args.region or get(cfg, "region", "us")),
                          check=False)
        if px is None:
            print("- 代理池里没有未使用过的了，跑 proxy:gen 续期")
            return 2
        print(f"- 取用一条新代理 {px.label}")
        print(f"- 剩余未用: {ledger.remaining([p.raw for p in pool.proxies])}")
    print(f"- 归一化: {px.raw}")
    if px.session:
        print(f"- 粘性会话: {px.session}"
              f"{f'（{px.sticky_minutes} 分钟）' if px.sticky_minutes else ''}")

    from app.register.bitbrowser import resolve_base
    client = BitBrowserClient(resolve_base(cfg))
    ok, detail = (client.health(), "OK") if client.is_port_open() else (False, "端口未监听")
    print(f"\n=== 比特浏览器 ===\n- {client.base} {'OK' if ok else detail}")
    if not ok:
        return 2

    region = (args.region or get(cfg, "region", "us")).lower()
    env_name = args.env_name or f"qoder_probe_{idx}"
    group_id = get(cfg, "browser.bitbrowser.group_id", "") or None
    fields = ("proxyMethod", "proxyType", "host", "port",
              "proxyUserName", "proxyPassword")

    if not args.launch:
        env_id, created = bb_cdp.ensure_env(
            client, name=env_name, region=region, proxy=px,
            group_id=group_id, reuse_env=True, force_new=args.force_new,
        )
        env = client.get_env(env_id)
        print(f"\n- 环境 {env.name} {env.id}（{'新建' if created else '复用'}）")
        print("- 写入后的代理字段：")
        print(json.dumps({k: env.raw.get(k) for k in fields},
                         ensure_ascii=False, indent=2))
        return 0

    launch = bb_cdp.open_for_register(
        client, proxy=px, region=region, env_name=env_name,
        group_id=group_id, reuse_env=not args.force_new,
        force_new=args.force_new,
    )
    print("\n- 启动结果：")
    print(json.dumps(launch.as_dict(), ensure_ascii=False, indent=2))
    env = client.get_env(launch.env_id)
    print("- 环境里的代理字段：")
    print(json.dumps({k: env.raw.get(k) for k in fields},
                     ensure_ascii=False, indent=2))
    if args.keep:
        print(f"\n窗口保持打开：{launch.endpoint}")
        return 0
    bb_cdp.close_env(client, launch.env_id)
    print("\n- 已关闭窗口")
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

    s = sub.add_parser("mail:watch", help="常驻收信进程（outlook/hotmail 长连接）")
    s.add_argument("--stop", action="store_true", help="停止常驻收信进程")
    s.set_defaults(func=cmd_mail_watch)

    s = sub.add_parser("mail:file",
                       help="体检/入库 data/emails.txt（email----pass----clientId----refreshToken）")
    s.add_argument("--file", default="", help="清单路径，默认取 mail.outlook_file")
    s.add_argument("--import", dest="import_to_db", action="store_true",
                   help="把清单地址写进数据库邮箱池")
    s.add_argument("--verify", action="store_true",
                   help="逐条刷 OAuth 令牌并连 IMAP，验证账号真能用")
    s.set_defaults(func=cmd_mail_file)

    s = sub.add_parser("env:check", help="指纹浏览器/打码/代理/设备自检")
    s.set_defaults(func=cmd_env_check)

    s = sub.add_parser("browser:probe",
                       help="把本地代理写进比特环境并校验（可选启动）")
    s.add_argument("--proxy-index", type=int, default=0, help="用第几条代理")
    s.add_argument("--env-name", default="", help="环境名，留空自动")
    s.add_argument("--region", default=None)
    s.add_argument("--launch", action="store_true", help="真的启动并校验 CDP")
    s.add_argument("--keep", action="store_true", help="启动后不关窗口")
    s.add_argument("--force-new", action="store_true", help="强制新建环境")
    s.add_argument("--check-all", action="store_true",
                   help="先把整池代理并发探活，只报结果")
    s.add_argument("--proxy", default="",
                   help="临时指定一条代理（覆盖代理文件），例：h:port:user:pass")
    s.add_argument("--timeout", type=int, default=15, help="探活超时秒")
    s.add_argument("--workers", type=int, default=10, help="探活并发")
    s.add_argument("--reuse", action="store_true",
                   help="复用指定下标的代理，不从账本里消耗")
    s.set_defaults(func=cmd_browser_probe)

    s = sub.add_parser("claim:one", help="单账号领取（可自动登录）")
    s.add_argument("--email", default="")
    s.add_argument("--password", default="")
    s.add_argument("--login", action="store_true", default=True,
                   help="自动登录（需要客户端点一次「登录」拿到链接）")
    s.add_argument("--no-login", dest="login", action="store_false",
                   help="跳过登录，直接抓现有 token")
    s.add_argument("--link-timeout", type=int, default=180)
    s.add_argument("--login-timeout", type=int, default=300)
    s.add_argument("--env-name", default="")
    s.set_defaults(func=cmd_claim_one)

    s = sub.add_parser("export:txt", help="导出账号/token/统计 txt")
    s.add_argument("--dir", default="", help="输出目录，默认 storage.output_dir")
    s.set_defaults(func=cmd_export_txt)

    s = sub.add_parser("proxy:gen", help="按模板批量生成新会话代理")
    s.add_argument("--template", default="")
    s.add_argument("--user", default="")
    s.add_argument("--password", default="")
    s.add_argument("--ttl", type=int, default=0)
    s.add_argument("--count", type=int, default=0)
    s.add_argument("--region", default=None)
    s.add_argument("--file", default="")
    s.add_argument("--replace", action="store_true", help="覆盖写而不是追加")
    s.add_argument("--no-verify", action="store_true")
    s.add_argument("--timeout", type=int, default=15)
    s.add_argument("--workers", type=int, default=6)
    s.set_defaults(func=cmd_proxy_gen)

    s = sub.add_parser("proxy:state", help="查看一条一用账本")
    s.add_argument("--prune", action="store_true")
    s.add_argument("--keep-hours", type=float, default=168.0)
    s.set_defaults(func=cmd_proxy_state)

    s = sub.add_parser("client:find", help="自动探测 Qoder 客户端可执行文件")
    s.add_argument("--path", default="", help="手动指定 exe，覆盖自动探测")
    s.set_defaults(func=cmd_client_find)

    s = sub.add_parser("run:one", help="跑一轮完整循环")
    s.add_argument("--region", default=None)
    s.add_argument("--dry-run-device", action="store_true",
                   help="设备重置只预演不执行")
    s.set_defaults(func=cmd_run_one)

    s = sub.add_parser("reg:one", help="只跑注册+邮箱验证（单窗口调试，不碰设备/客户端）")
    s.add_argument("--region", default=None)
    s.set_defaults(func=cmd_reg_one)

    s = sub.add_parser("manual:go", help="人工接管通知：通知等待中的注册流程继续")
    s.set_defaults(func=cmd_manual_go)

    s = sub.add_parser("run:loop", help="批量跑（全串行）")
    s.add_argument("--n", type=int, default=10)
    s.add_argument("--sleep", type=int, default=90)
    s.set_defaults(func=cmd_run_loop)

    s = sub.add_parser("run:concurrent", help="注册并行 + 领取串行")
    s.add_argument("--total", type=int, default=50, help="注册成功目标数（达标自动截止）")
    s.add_argument("--workers", type=int, default=3, help="注册并发数（建议 3~5）")
    s.add_argument("--region", default=None)
    s.add_argument("--slot", default="local", help="机器槽位标识，多机部署时每台不同")
    s.add_argument("--lock", default="data/.device.lock")
    s.set_defaults(func=cmd_run_concurrent)

    s = sub.add_parser("pool:status", help="比特窗口池状态（探测/补齐/占用）")
    s.add_argument("--ensure", action="store_true", help="不足 size 时补齐池窗口")
    s.add_argument("--close", action="store_true", help="关掉全部池窗口并清占用账本")
    s.set_defaults(func=cmd_pool_status)

    s = sub.add_parser("web", help="启动 Web 控制台（前端配置 + 启停机器人）")
    s.add_argument("--port", type=int, default=8877)
    s.set_defaults(func=cmd_web)

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
    s.add_argument("--identity-mode", default=None, choices=["browser"],
                   help="browser=设备身份匹配注册段浏览器画像（缺省=原随机方案）")
    s.add_argument("--seed", default="",
                   help="identity-mode=browser 时的画像种子（与账号 tag 一致）")
    s.add_argument("--region", default="us", help="identity-mode=browser 时的地区")
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
    # 密钥从 config/secrets.yaml 灌进环境变量（不覆盖已有值），
    # 必须在 load_config 之前跑，否则 env:XXX 插值拿到空串
    try:
        from app import secrets

        touched = secrets.export_env()
        if touched:
            log.debug("secrets 已导入：%s", ", ".join(touched))
    except Exception:
        pass
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
