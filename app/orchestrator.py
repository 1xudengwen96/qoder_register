"""流程编排 v2：指纹浏览器 + 多平台打码 + qoder-free 改造重置器。

链路：
  ① 取邮箱（Outlook +tag / Hotmail 多主号 / iCloud 转发聚合）
  ② 指纹浏览器开环境（与代理地区对齐）
  ③ 网页注册（打码平台降级调度）
  ④ 收信点确认（同源 IP）
  ⑤ 设备重置（qoder_reset.QoderIdentityReset，全量 9 步）
  ⑥ 客户端登录领取
  ⑦ 入库分级，到期预警

事务边界：每步独立提交，中断后按 state 续跑。
串行约束：领取阶段同一时刻只允许一个客户端实例。
"""

from __future__ import annotations

import dataclasses
import json
import time
from datetime import datetime, timedelta

from app.claim.claimer import Claimer
from app.config import get
from app.db import get_session, session_scope
from app.device.qoder_reset import ALL_STEPS, QoderIdentityReset, ResetOptions
from app.logging_setup import setup
from app.mail.base import MailIdentity as MailIdent
from app.mail.factory import build_mail_layer
from app.models import (
    Account,
    AccountState,
    DeviceProfile,
    EventLog,
    MailIdentity as MailRow,
    MailStatus,
)
from app.net.captcha_solver import build_solver
from app.net.proxy import ProxyPool
from app.register.browser_registrar import BrowserRegistrar, random_username, strong_password
from app.register.fingerprint_engine import build_fingerprint_engine
from app.register.verifier import Verifier
from app.stock.exporter import StockManager

log = setup()


@dataclasses.dataclass(slots=True)
class RunReport:
    ok: bool
    account_id: int | None = None
    email: str = ""
    stage: str = ""
    message: str = ""
    credits: int = 0
    captcha_provider: str = ""


class _DBHolder:
    """给 build_mail_layer 用的轻量 db 句柄。"""

    @staticmethod
    def get_session():
        return get_session()


class Pipeline:
    def __init__(self, cfg) -> None:
        self.cfg = cfg
        try:
            from app import secrets

            secrets.export_env()
        except Exception:  # noqa: BLE001
            pass
        self.mail_layer = build_mail_layer(cfg, _DBHolder)
        self.proxies = ProxyPool.from_file(get(cfg, "proxy.file", "data/proxies.txt"))
        from app.net.proxy import ProxyLedger

        self.ledger = ProxyLedger(get(cfg, "proxy.state_file",
                                      "data/.proxy_state.json"))
        self.solver = build_solver(cfg)
        self.engine = build_fingerprint_engine(cfg)
        self.registrar = BrowserRegistrar(
            engine=self.engine,
            solver=self.solver,
            signup_url=get(cfg, "register.signup_url", "https://qoder.com/signup"),
            artifacts_dir=get(cfg, "storage.artifacts_dir", "data/artifacts"),
            recaptcha_site_key=get(cfg, "register.recaptcha_site_key", None),
            hcaptcha_site_key=get(cfg, "register.hcaptcha_site_key", None),
            turnstile_site_key=get(cfg, "register.turnstile_site_key", None),
        )
        # 注册通道：CDP（比特浏览器自动填代理）优先，Playwright 引擎兜底
        backend = str(get(cfg, "browser.backend", "") or "").lower()
        self.cdp_registrar = None
        self.use_cdp = bool(get(cfg, "browser.cdp_mode", backend == "bitbrowser"))
        if self.use_cdp:
            from app.register.bitbrowser import resolve_base
            from app.register.cdp_registrar import CDPRegistrar

            self.cdp_registrar = CDPRegistrar(
                cfg,
                api_base=resolve_base(cfg),
                group_id=(get(cfg, "browser.bitbrowser.group_id", "") or None),
                env_id=get(cfg, "browser.bitbrowser.env_id", "") or "",
                reuse_env=bool(get(cfg, "browser.bitbrowser.reuse_env", False)),
                signup_url=get(cfg, "register.cdp_signup_url",
                               "https://qoder.com/users/sign-up"),
                artifacts_dir=get(cfg, "storage.artifacts_dir", "data/artifacts"),
                captcha_max_attempts=int(get(cfg, "captcha.max_attempts", 6)),
                otp_timeout=int(get(cfg, "verify.timeout", 240)),
                imap_proxy=(get(cfg, "mail.imap_proxy", "") or None),
                headless=bool(get(cfg, "browser.bitbrowser.headless", False)),
                keep_open=bool(get(cfg, "browser.bitbrowser.keep_open", False)),
                logger=log,
                manual_captcha=bool(get(cfg, "captcha.manual", False)),
                manual_timeout=int(get(cfg, "captcha.manual_timeout", 600)),
                manual_otp=bool(get(cfg, "captcha.manual_otp", False)),
                manual_form=bool(get(cfg, "captcha.manual_form", False)),
            )

        # 窗口池（2026-11-18）：固定 N 个带标识的比特窗口循环复用，
        # 每用一次重配指纹+新代理+清残留；开关 browser.env_pool.enabled。
        # 旧逻辑（每号新建 acct{id}）备份在 data/logic_backup_20261118。
        self.env_pool = None
        if self.cdp_registrar is not None:
            from app.register.bb_pool import build_env_pool

            self.env_pool = build_env_pool(cfg, self.cdp_registrar.client)
            self.cdp_registrar.env_pool = self.env_pool
            if self.env_pool is not None:
                log.info("窗口池已启用：固定 %d 个 %s* 窗口循环复用",
                         self.env_pool.size, self.env_pool.prefix)

        self.verifier = Verifier(
            mail_layer=self.mail_layer,
            timeout=int(get(cfg, "verify.timeout", 600)),
            extra_confirm_hosts=tuple(get(cfg, "verify.confirm_hosts", []) or []),
        )
        self.resetter = QoderIdentityReset(
            logger=log,
            app_dir=get(cfg, "device.app_dir", None),
            backup_dir=get(cfg, "device.backup_dir", "data/device_backup"),
            client_path=get(cfg, "claim.client_path", "") or None,
        )
        # 领取登录模式：inherit（默认，继承已有登录态，无人值守）/ cdp
        # （半自动：人点一次客户端「登录」，拿链接之后 wait_link →
        # drive_login → 回跳全自动，见 ``_claim_ui_login``）
        self.claim_login_mode = str(
            get(cfg, "claim.login_mode", "inherit") or "inherit").strip().lower()
        self.claimer = Claimer(
            client_path=get(cfg, "claim.client_path", ""),
            account_data_dir=get(cfg, "claim.data_dir", ""),
            wait_credit_timeout=int(get(cfg, "claim.wait_credit_timeout", 120)),
            capture_token=bool(get(cfg, "claim.capture_token", True)),
            credit_probe_url=get(cfg, "claim.credit_probe_url", None),
            login_mode="cdp" if self.claim_login_mode == "cdp" else "inherit",
        )

    # ------------------------------------------------------------ 事件
    @staticmethod
    def _event(account_id: int | None, stage: str, level: str, message: str,
               payload: dict | None = None, mail_id: int | None = None) -> None:
        with session_scope() as s:
            s.add(EventLog(
                account_id=account_id, mail_identity_id=mail_id, stage=stage,
                level=level, message=message,
                payload=json.dumps(payload, ensure_ascii=False) if payload else None,
            ))
        fn = {"info": log.info, "warning": log.warning, "error": log.error}.get(level, log.info)
        fn(f"[{stage}] {message}")

    def _fail(self, account_id: int, stage: str, msg: str) -> None:
        with session_scope() as s:
            a = s.get(Account, account_id)
            if a:
                a.state = AccountState.DEAD
        self._event(account_id, stage, "error", msg)

    def _flag(self, account_id: int, flag: str) -> None:
        with session_scope() as s:
            a = s.get(Account, account_id)
            if a:
                a.add_flag(flag)

    def _mail_status(self, ident, status: MailStatus) -> None:
        with session_scope() as s:
            row = (s.query(MailRow)
                   .filter(MailRow.address == ident.address)
                   .order_by(MailRow.id.desc()).first())
            if row:
                row.status = status
                if status == MailStatus.USED:
                    row.used_at = datetime.now()

    # ------------------------------------------------------------ 代理分配
    @property
    def _proxy_enabled(self) -> bool:
        """代理总开关：false = 全链路直连（比特环境 proxyMethod=3，
        QQ IMAP 收信本就直连，不受此开关影响）。"""
        return bool(get(self.cfg, "proxy.enabled", True))

    def _acquire_proxy(self, region: str | None = None, env_tag: str = "") -> object | None:
        """给这次开窗口取**一条没用过**的代理；池子空了就按模板续期。

        新逻辑（优先）：``data/ip.txt``（ip:port:user:password）→ ipdeep extract_url
        兜底；region 只允许 sg/us。详见 ``proxy.acquire_for_browser``。
        拿不到再走原来的 ProxyPool + acquire_once 链路。

        ``proxy.enabled=false`` 时直接返回 ``None``（直连模式，
        注册窗口不带代理、用本机出口 IP）。
        """
        if not self._proxy_enabled:
            return None

        from app.net.proxy import acquire_for_browser, acquire_once

        # ① 新逻辑：ip.txt（消耗式）→ ipdeep 兜底（只 us/sg）
        px = acquire_for_browser(self.cfg, region=region or "sg")
        if px is not None:
            if env_tag:
                self.ledger.mark(px, env=env_tag, note="in-use")
            return px

        # ② 旧逻辑兜底
        if not bool(get(self.cfg, "proxy.one_shot", True)):
            return self.proxies.acquire(region=region)

        check = bool(get(self.cfg, "proxy.check_before_use", True))
        timeout = int(get(self.cfg, "proxy.check_timeout", 12))
        px = acquire_once(self.proxies, self.ledger, region=region,
                          check=check, timeout=timeout)
        if px is None and bool(get(self.cfg, "proxy.auto_extend", True)):
            added = self._extend_proxies(region=region)
            if added:
                self.proxies = ProxyPool.from_file(
                    get(self.cfg, "proxy.file", "data/proxies.txt"))
                px = acquire_once(self.proxies, self.ledger, region=region,
                                  check=check, timeout=timeout)
        if px is not None and env_tag:
            self.ledger.mark(px, env=env_tag, note="in-use")
        return px

    def _extend_proxies(self, region: str | None = None) -> int:
        """按 proxy.extend.template 生成新会话并写回代理文件。"""
        from app.net.proxy import append_to_file, extend_from_template

        tpl = str(get(self.cfg, "proxy.extend.template", "") or "")
        if not tpl:
            return 0
        region = (region or get(self.cfg, "region", "us")).lower()
        user_tpl = str(get(self.cfg, "proxy.extend.user", "") or "")
        raws = extend_from_template(
            template=tpl,
            user=user_tpl.replace("{region}", region),
            password=str(get(self.cfg, "proxy.extend.password", "") or ""),
            ttl=int(get(self.cfg, "proxy.extend.ttl", 5)),
            count=int(get(self.cfg, "proxy.extend.count", 20)),
            region=region,
            verify=bool(get(self.cfg, "proxy.extend.verify", True)),
        )
        added = append_to_file(get(self.cfg, "proxy.file", "data/proxies.txt"), raws)
        log.info("代理续期：模板生成 %d 条，存活并写入 %d 条", len(raws), added)
        return added

    # ------------------------------------------------------------ 注册通道
    def _do_register(self, email_addr: str, password: str, username: str,
                     proxy, region: str, account_tag: str, ident):
        """按配置选注册通道：CDP（比特浏览器 + 自动填代理）或 Playwright 引擎。"""
        if self.cdp_registrar is None:
            return self.registrar.register(
                email_addr, password, username,
                proxy=proxy, region=region, account_tag=account_tag,
            )

        from app.register.otp_source import make_code_source

        src = make_code_source(
            self.cfg, email=email_addr, ident=ident,
            mail_layer=self.mail_layer,
            timeout=int(get(self.cfg, "verify.timeout", 240)),
            imap_proxy=(get(self.cfg, "mail.imap_proxy", "") or None),
        )
        try:
            return self.cdp_registrar.register(
                email_addr, password,
                proxy=proxy, region=region, account_tag=account_tag,
                code_source=src,
            )
        finally:
            if src is not None:
                src.close()

    # ------------------------------------------------------------ 设备画像
    def _device_profile_for(self, account_id: int, region: str):
        """设备身份画像（browser 匹配模式）。

        注册段的比特浏览器画像是 ``make_profile(region, seed=acct{id})``（确定性），
        领取段用同一 seed 重建后派生设备身份 → 平台/时区/语言/CPU/内存/GPU/分辨率
        与注册环境完全一致。``device.identity_mode`` 不设或非 browser 时返回 None，
        走原随机方案。
        """
        mode = str(get(self.cfg, "device.identity_mode", "") or "").strip().lower()
        if mode not in ("browser", "match", "fingerprint_browser"):
            return None
        try:
            from app.device.identity_profile import profile_from_browser
            from app.register.fingerprint_engine import make_profile

            bfp = make_profile(region=region, seed=f"acct{account_id}")
            return profile_from_browser(bfp)
        except Exception as exc:  # noqa: BLE001
            log.warning("浏览器匹配画像构建失败，退回原随机方案：%s", exc)
            return None

    # ------------------------------------------------------------ 领取登录
    def _claim_env_name(self, account_id: int) -> str:
        """领取段用哪个窗口：池化后查 账号→窗口 映射（qpool_XX），
        没启用池/没记录时退回旧逻辑 acct{id}。"""
        pool = getattr(self, "env_pool", None)
        if pool is not None:
            name = pool.env_name_for(f"acct{account_id}")
            if name:
                return name
        return f"acct{account_id}"

    def shutdown_browsers(self) -> None:
        """进程收尾：关掉全部池窗口（用户设计：任务进程结束就关浏览器，
        新进程来了再随机开启）。profile 存盘，Cookie 不丢。"""
        pool = getattr(self, "env_pool", None)
        if pool is not None:
            pool.close_all()

    def _claim_ui_login(self, email_addr: str, password: str, account_id: int):
        """领取段的登录动作，返回 (回调, state)。

        默认（``claim.login_mode`` 不设 / inherit）返回 ``(None, {})``：走
        继承模式，perform 里只等 token 变化——但设备重置已清空登录态、
        客户端停在登录界面，这条路在全自动链路上等不到新 token，等于把
        领取留给人工补跑。配置 ``claim.login_mode: cdp`` 改走半自动：
        人点一次客户端「登录」，之后 wait_link → drive_login → 回跳全自动。
        """
        if self.claim_login_mode not in ("cdp", "ui", "manual"):
            return None, {}
        try:
            from app.claim.ui_login import make_ui_login

            ui, state = make_ui_login(
                self.cfg, email_addr, password,
                # 一一对应：复用注册窗口（池化后是 qpool_XX，同指纹/代理/IP，
                # 注册 cookie 还在，selectAccounts 免密授权），不再另建 login 环境
                env_name=self._claim_env_name(account_id),
                region=get(self.cfg, "region", "us"),
                link_timeout=int(get(self.cfg, "claim.link_timeout", 180)),
                login_timeout=int(get(self.cfg, "claim.login_timeout", 300)),
                artifacts_dir=get(self.cfg, "storage.artifacts_dir", "data/artifacts"),
            )
            return ui, state
        except Exception as exc:  # noqa: BLE001
            log.warning("半自动登录回调构建失败，退回继承模式：%s", exc)
            return None, {}

    def _claim_with_fallback(self, email_addr: str, password: str, account_id: int):
        """perform + 额度兜底（登录会话额度 → 账号页重读），claim_only/_run_one 共用。"""
        from app.claim.claimer import apply_credits_fallback

        ui, login_state = self._claim_ui_login(email_addr, password, account_id)
        cres = self.claimer.perform(email_addr, password, ui_login=ui)
        if cres.ok and cres.credits <= 0:
            src = apply_credits_fallback(
                cres, login_state, self.cfg,
                env_name=self._claim_env_name(account_id),
                region=get(self.cfg, "region", "us"),
            )
            if src and cres.credits > 0:
                log.info("额度兜底命中（%s）：credits=%d plan=%s",
                         src, cres.credits, cres.plan)
        return cres

    # ------------------------------------------------------------ 分段执行
    def register_only(self, region: str | None = None, worker: int = 0) -> RunReport:
        """只做注册 + 验证，不碰设备、不碰客户端。可并发跑多个 worker。

        与 ``run_one`` 的区别：到 ``verified`` 就返回，把账号留给领取段。
        并发安全：每个 worker 独占一个指纹浏览器环境，互不共享 Cookie/缓存。
        """
        region = (region or get(self.cfg, "region", "us")).lower()

        idents = self.mail_layer.acquire(1)
        if not idents:
            return RunReport(False, stage="mail", message="邮箱池已空")
        ident = idents[0]

        with session_scope() as s:
            # mail:import 可能已把地址以 POOL 状态导入，先查再 update/insert
            mail_row = s.query(MailRow).filter(
                MailRow.address == ident.address).one_or_none()
            if mail_row is None:
                mail_row = MailRow(provider=ident.provider, address=ident.address,
                                   parent_account=ident.parent_account,
                                   status=MailStatus.PENDING)
                s.add(mail_row)
            else:
                mail_row.status = MailStatus.PENDING
                if ident.parent_account and not mail_row.parent_account:
                    mail_row.parent_account = ident.parent_account
            s.flush()
            acct = Account(email=ident.address, password=strong_password(),
                           username=random_username(), state=AccountState.NEW)
            s.add(acct)
            s.flush()
            mail_row.qoder_account_id = acct.id
            account_id, mail_id = acct.id, mail_row.id
            email_addr, password, username = acct.email, acct.password, acct.username

        self._event(account_id, "mail", "info", f"取到邮箱 {email_addr}",
                    {"provider": ident.provider, "worker": worker}, mail_id)

        proxy = self._acquire_proxy(region=region, env_tag=f"acct{account_id}")
        if proxy is None and self._proxy_enabled:
            self._fail(account_id, "proxy", "代理池无可用出口")
            return RunReport(False, account_id, email_addr, "proxy", "代理池无可用出口")
        if proxy is None:
            self._event(account_id, "proxy", "info",
                        "代理开关关闭，注册窗口直连（本机出口 IP）")

        mident = MailIdent(
            address=ident.address, provider=ident.provider,
            parent_account=ident.parent_account,
            secret=getattr(ident, "secret", None),
            imap_host=getattr(ident, "imap_host", None),
            meta=getattr(ident, "meta", {}) or {},
        )
        reg = self._do_register(
            email_addr, password, username, proxy, region, f"acct{account_id}", ident,
        )
        if not reg.ok:
            if reg.kind == "email_conflict" and ident.provider == "outlook_tag":
                self._flag(account_id, "tag_normalized")
            elif reg.kind == "rate_limited":
                if proxy is not None:
                    self.proxies.mark_banned(proxy)
                self._flag(account_id, "proxy_dirty")
            self._fail(account_id, "register", f"{reg.kind}: {reg.error}")
            return RunReport(False, account_id, email_addr, "register",
                             f"{reg.kind}: {reg.error}", captcha_provider=reg.used_captcha)

        with session_scope() as s:
            a = s.get(Account, account_id)
            a.state = AccountState.REGISTERED
            a.register_ip = proxy.exit_ip_hint if proxy is not None else "direct"

        # CDP 通道在注册内部就完成了邮箱验证（6 位码），不再等确认链接
        if self.cdp_registrar is None:
            vres = self.verifier.run(mident, proxy=proxy)
            if not vres.ok:
                self._mail_status(ident, MailStatus.DROPPED)
                self._flag(account_id, "mail_dropped")
                self._fail(account_id, "verify", vres.error)
                return RunReport(False, account_id, email_addr, "verify", vres.error,
                                 captcha_provider=reg.used_captcha)

        with session_scope() as s:
            s.get(Account, account_id).state = AccountState.VERIFIED
        self._mail_status(ident, MailStatus.USED)
        self._event(account_id, "verify", "info", "注册+验证完成，入待领队列",
                    {"worker": worker})
        return RunReport(True, account_id, email_addr, "verified", "ready",
                         captcha_provider=reg.used_captcha)

    def claim_only(self, account_id: int, device_lock=None) -> RunReport:
        """只做设备重置 + 客户端领取。必须串行，同一时刻只能跑一个。

        ``device_lock`` 是 ``concurrent.DeviceLock``；传入则长任务期间续租。
        """
        from app.device.qoder_reset import ALL_STEPS, QoderIdentityReset, ResetOptions

        with session_scope() as s:
            a = s.get(Account, account_id)
            if a is None:
                return RunReport(False, account_id, "", "claim", "账号不存在")
            email_addr, password = a.email, a.password or ""

        region = get(self.cfg, "region", "us").lower()
        tz = get(self.cfg, f"region_profiles.{region}.timezone", "America/New_York")

        # 设备重置（全局状态，不能并发）
        identity = self._device_profile_for(account_id, region)
        if identity is not None:
            log.info("设备身份=浏览器匹配画像 %s", identity.brief())
        rres = self.resetter.run(ResetOptions(
            steps=tuple(ALL_STEPS),
            dry_run=False,
            preserve_chat=False,
            fake_hardware=bool(get(self.cfg, "device.fake_hardware", False)),
            timezone=tz,
            locale=get(self.cfg, f"region_profiles.{region}.locale", "en-US"),
            profile=identity,
        ))
        if device_lock:
            device_lock.refresh()

        with session_scope() as s:
            prof = DeviceProfile(
                machine_id=rres.machine_id or "unknown",
                telemetry_id=rres.telemetry.get("telemetry.devDeviceId", ""),
                account_id=account_id, claim_count=0,
            )
            s.add(prof)
            s.flush()
            a = s.get(Account, account_id)
            a.device_profile_id = prof.id
            a.state = AccountState.CLAIMING
            prof_id = prof.id
        self._event(account_id, "reset",
                    "info" if rres.ok else "warning",
                    f"设备重置 {'成功' if rres.ok else '有异常'}，{len(rres.actions)} 项动作",
                    {"errors": rres.errors[:10]})

        if device_lock:
            device_lock.refresh()

        cres = self._claim_with_fallback(email_addr, password, account_id)
        try:
            if not cres.ok:
                self._release(prof_id)
                self._fail(account_id, "claim", cres.error)
                return RunReport(False, account_id, email_addr, "claim", cres.error)

            with session_scope() as s:
                a = s.get(Account, account_id)
                a.state = AccountState.STOCK
                a.credits = cres.credits
                a.plan = cres.plan
                a.trial_expire_at = cres.expire_at or (datetime.now() + timedelta(days=14))
                a.claimed_at = datetime.now()
                if cres.token:
                    a.token_json = json.dumps(cres.token, ensure_ascii=False)
                prof = s.get(DeviceProfile, prof_id)
                if prof:
                    prof.claim_count += 1
                summary = StockManager(s).summary()

            self._event(account_id, "claim", "info",
                        f"领取 {cres.credits} Credits / {cres.plan}；{summary}")
            return RunReport(True, account_id, email_addr, "claim", "claimed", cres.credits)
        finally:
            # 领取完成（无论成败）：注册窗口关窗回池，解除占用锁定
            pool = getattr(self, "env_pool", None)
            if pool is not None:
                pool.finish_claim(f"acct{account_id}")

    # ------------------------------------------------------------ 单轮（串行全流程）
    def run_one(self, region: str | None = None, dry_run_device: bool = False) -> RunReport:
        """跑一轮完整循环；无论成败，收尾都把账号/token 落成 txt。"""
        try:
            return self._run_one(region=region, dry_run_device=dry_run_device)
        finally:
            self._export_outputs()

    def _export_outputs(self) -> None:
        try:
            from app.stock.txt_export import export_all

            with session_scope() as s:
                rep = export_all(s, get(self.cfg, "storage.output_dir", "data/output"))
            log.info("输出已刷新：账号 %d 条 / token %d 条 -> %s",
                     rep.accounts, rep.tokens, rep.files.get("accounts", ""))
        except Exception as exc:  # noqa: BLE001
            log.warning("文本导出失败（不影响流程）：%s: %s", type(exc).__name__, exc)

    def _run_one(self, region: str | None = None, dry_run_device: bool = False) -> RunReport:
        region = (region or get(self.cfg, "region", "us")).lower()
        tz = get(self.cfg, f"region_profiles.{region}.timezone", "America/New_York")

        # ① 邮箱（跳过已建过账号的地址：mail_identity 可能回 POOL，
        #    但同邮箱在 account 表已存在（如昨天 CLAIMED 的），直接 insert 会撞唯一约束）
        ident = None
        with session_scope() as s:
            for _ in range(3):
                idents = self.mail_layer.acquire(1)
                if not idents:
                    break
                cand = idents[0]
                if s.query(Account).filter(
                        Account.email == cand.address).one_or_none() is None:
                    ident = cand
                    break
                row = s.query(MailRow).filter(
                    MailRow.address == cand.address).one_or_none()
                if row is not None and row.status == MailStatus.POOL:
                    row.status = MailStatus.USED
                s.flush()
            if ident is None:
                return RunReport(False, stage="mail", message="邮箱池已空/无新地址")

            mail_row = s.query(MailRow).filter(
                MailRow.address == ident.address).one_or_none()
            if mail_row is None:
                mail_row = MailRow(provider=ident.provider, address=ident.address,
                                   parent_account=ident.parent_account,
                                   status=MailStatus.PENDING)
                s.add(mail_row)
            else:
                mail_row.status = MailStatus.PENDING
                if ident.parent_account and not mail_row.parent_account:
                    mail_row.parent_account = ident.parent_account
            s.flush()
            acct = Account(email=ident.address, password=strong_password(),
                           username=random_username(), state=AccountState.NEW)
            s.add(acct)
            s.flush()
            mail_row.qoder_account_id = acct.id
            account_id, mail_id = acct.id, mail_row.id
            email_addr, password, username = acct.email, acct.password, acct.username

        self._event(account_id, "mail", "info", f"取到邮箱 {email_addr}",
                    {"provider": ident.provider}, mail_id)

        # 代理
        proxy = self._acquire_proxy(region=region, env_tag=f"acct{account_id}")
        if proxy is None and self._proxy_enabled:
            self._fail(account_id, "proxy", "代理池无可用出口")
            return RunReport(False, account_id, email_addr, "proxy", "代理池无可用出口")
        if proxy is None:
            self._event(account_id, "proxy", "info",
                        "代理开关关闭，注册窗口直连（本机出口 IP）")

        # ② + ③ 指纹浏览器注册
        mident = MailIdent(
            address=ident.address, provider=ident.provider,
            parent_account=ident.parent_account,
            secret=getattr(ident, "secret", None),
            imap_host=getattr(ident, "imap_host", None),
            meta=getattr(ident, "meta", {}) or {},
        )
        reg = self._do_register(
            email_addr, password, username, proxy, region, f"acct{account_id}", ident,
        )
        if not reg.ok:
            if reg.kind == "email_conflict" and ident.provider == "outlook_tag":
                self._flag(account_id, "tag_normalized")
                self._event(account_id, "register", "warning",
                            "疑似 +tag 被归一化，建议降权 outlook_tag",
                            {"screenshot": reg.screenshot})
            elif reg.kind == "rate_limited":
                if proxy is not None:
                    self.proxies.mark_banned(proxy)
                self._flag(account_id, "proxy_dirty")
            self._fail(account_id, "register", f"{reg.kind}: {reg.error}")
            return RunReport(False, account_id, email_addr, "register",
                             f"{reg.kind}: {reg.error}", captcha_provider=reg.used_captcha)

        with session_scope() as s:
            a = s.get(Account, account_id)
            a.state = AccountState.REGISTERED
            a.register_ip = proxy.exit_ip_hint if proxy is not None else "direct"
        self._event(account_id, "register", "info",
                    f"注册成功（打码 {reg.used_captcha or '无'}，耗时 {reg.elapsed:.1f}s）",
                    {"screenshot": reg.screenshot, "url": reg.page_url})

        # ④ 验证
        if self.cdp_registrar is None:
            vres = self.verifier.run(mident, proxy=proxy)
            if not vres.ok:
                self._mail_status(ident, MailStatus.DROPPED)
                self._flag(account_id, "mail_dropped")
                self._fail(account_id, "verify", vres.error)
                return RunReport(False, account_id, email_addr, "verify", vres.error,
                                 captcha_provider=reg.used_captcha)
            verify_detail = {"link": vres.link}
        else:
            verify_detail = {"mode": "otp-in-signup"}

        with session_scope() as s:
            s.get(Account, account_id).state = AccountState.VERIFIED
        self._event(account_id, "verify", "info", "邮箱验证通过", verify_detail)

        # ⑤ 设备重置（qoder-free 改造版，全量 9 步）
        identity = self._device_profile_for(account_id, region)
        if identity is not None:
            log.info("设备身份=浏览器匹配画像 %s", identity.brief())
        rres = self.resetter.run(ResetOptions(
            steps=tuple(ALL_STEPS),
            dry_run=dry_run_device,
            preserve_chat=False,
            fake_hardware=bool(get(self.cfg, "device.fake_hardware", False)),
            timezone=tz,
            locale=get(self.cfg, f"region_profiles.{region}.locale", "en-US"),
            profile=identity,
        ))
        with session_scope() as s:
            prof = DeviceProfile(
                machine_id=rres.machine_id or "dry-run",
                telemetry_id=rres.telemetry.get("telemetry.devDeviceId", ""),
                mac_address=None, volume_serial=None,
                account_id=account_id, claim_count=0,
            )
            s.add(prof)
            s.flush()
            a = s.get(Account, account_id)
            a.state = AccountState.CLAIMING
            a.device_profile_id = prof.id
            prof_id = prof.id
        self._event(account_id, "reset",
                    "info" if rres.ok else "warning",
                    f"设备重置 {'成功' if rres.ok else '有异常'}，动作 {len(rres.actions)} 项",
                    {"errors": rres.errors[:10], "backup": rres.backup_dir})

        # ⑥ 领取（claim.login_mode=cdp 时半自动：人点一次客户端「登录」）
        cres = self._claim_with_fallback(email_addr, password, account_id)
        pool = getattr(self, "env_pool", None)
        if pool is not None:
            pool.finish_claim(f"acct{account_id}")
        if not cres.ok:
            self._release(prof_id)
            self._fail(account_id, "claim", cres.error)
            return RunReport(False, account_id, email_addr, "claim", cres.error,
                             captcha_provider=reg.used_captcha)

        with session_scope() as s:
            a = s.get(Account, account_id)
            a.state = AccountState.CLAIMED
            a.credits = cres.credits
            a.plan = cres.plan
            a.trial_expire_at = cres.expire_at or (datetime.now() + timedelta(days=14))
            a.claimed_at = datetime.now()
            if cres.token:
                a.token_json = json.dumps(cres.token, ensure_ascii=False)
            prof = s.get(DeviceProfile, prof_id)
            if prof:
                prof.claim_count += 1
        self._mail_status(ident, MailStatus.USED)

        # ⑦ 入库
        with session_scope() as s:
            s.get(Account, account_id).state = AccountState.STOCK
            summary = StockManager(s).summary()
        self._event(account_id, "claim", "info",
                    f"领取 {cres.credits} Credits / {cres.plan}；{summary}")

        return RunReport(True, account_id, email_addr, "claim", "claimed",
                         cres.credits, reg.used_captcha)

    # ------------------------------------------------------------ 批量
    def run_loop(self, n: int, sleep_sec: int = 90) -> list[RunReport]:
        reports: list[RunReport] = []
        for i in range(n):
            log.info("===== 第 %d/%d 轮 =====", i + 1, n)
            try:
                reports.append(self.run_one())
            except Exception as exc:
                log.exception("轮次异常：%s", exc)
                reports.append(RunReport(False, stage="exception", message=str(exc)))
            if i < n - 1:
                time.sleep(sleep_sec)
        ok = sum(1 for r in reports if r.ok)
        log.info("完成 %d 轮，成功 %d，失败 %d", len(reports), ok, len(reports) - ok)
        log.info(self.solver.cost_summary())
        return reports

    def _release(self, prof_id: int) -> None:
        with session_scope() as s:
            prof = s.get(DeviceProfile, prof_id)
            if prof:
                prof.account_id = None
