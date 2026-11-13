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
        self.mail_layer = build_mail_layer(cfg, _DBHolder)
        self.proxies = ProxyPool.from_file(get(cfg, "proxy.file", "data/proxies.txt"))
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
        self.verifier = Verifier(
            mail_layer=self.mail_layer,
            timeout=int(get(cfg, "verify.timeout", 600)),
            extra_confirm_hosts=tuple(get(cfg, "verify.confirm_hosts", []) or []),
        )
        self.resetter = QoderIdentityReset(
            logger=log,
            app_dir=get(cfg, "device.app_dir", None),
            backup_dir=get(cfg, "device.backup_dir", "data/device_backup"),
        )
        self.claimer = Claimer(
            client_path=get(cfg, "claim.client_path", ""),
            account_data_dir=get(cfg, "claim.data_dir", ""),
            wait_credit_timeout=int(get(cfg, "claim.wait_credit_timeout", 120)),
            capture_token=bool(get(cfg, "claim.capture_token", True)),
            credit_probe_url=get(cfg, "claim.credit_probe_url", None),
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
            mail_row = MailRow(provider=ident.provider, address=ident.address,
                               parent_account=ident.parent_account,
                               status=MailStatus.PENDING)
            s.add(mail_row)
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

        proxy = self.proxies.acquire(region=region)
        if proxy is None:
            self._fail(account_id, "proxy", "代理池无可用出口")
            return RunReport(False, account_id, email_addr, "proxy", "代理池无可用出口")

        mident = MailIdent(
            address=ident.address, provider=ident.provider,
            parent_account=ident.parent_account,
            secret=getattr(ident, "secret", None),
            imap_host=getattr(ident, "imap_host", None),
            meta=getattr(ident, "meta", {}) or {},
        )
        reg = self.registrar.register(
            email_addr, password, username,
            proxy=proxy, region=region,
            account_tag=f"acct{account_id}",
        )
        if not reg.ok:
            if reg.kind == "email_conflict" and ident.provider == "outlook_tag":
                self._flag(account_id, "tag_normalized")
            elif reg.kind == "rate_limited":
                self.proxies.mark_banned(proxy)
                self._flag(account_id, "proxy_dirty")
            self._fail(account_id, "register", f"{reg.kind}: {reg.error}")
            return RunReport(False, account_id, email_addr, "register",
                             f"{reg.kind}: {reg.error}", captcha_provider=reg.used_captcha)

        with session_scope() as s:
            a = s.get(Account, account_id)
            a.state = AccountState.REGISTERED
            a.register_ip = proxy.exit_ip_hint

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
        rres = self.resetter.run(ResetOptions(
            steps=tuple(ALL_STEPS),
            dry_run=False,
            preserve_chat=False,
            fake_hardware=bool(get(self.cfg, "device.fake_hardware", False)),
            timezone=tz,
            locale=get(self.cfg, f"region_profiles.{region}.locale", "en-US"),
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

        cres = self.claimer.perform(email_addr, password)
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

    # ------------------------------------------------------------ 单轮（串行全流程）
    def run_one(self, region: str | None = None, dry_run_device: bool = False) -> RunReport:
        region = (region or get(self.cfg, "region", "us")).lower()
        tz = get(self.cfg, f"region_profiles.{region}.timezone", "America/New_York")

        # ① 邮箱
        idents = self.mail_layer.acquire(1)
        if not idents:
            return RunReport(False, stage="mail", message="邮箱池已空")
        ident = idents[0]

        with session_scope() as s:
            mail_row = MailRow(provider=ident.provider, address=ident.address,
                               parent_account=ident.parent_account,
                               status=MailStatus.PENDING)
            s.add(mail_row)
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
        proxy = self.proxies.acquire(region=region)
        if proxy is None:
            self._fail(account_id, "proxy", "代理池无可用出口")
            return RunReport(False, account_id, email_addr, "proxy", "代理池无可用出口")

        # ② + ③ 指纹浏览器注册
        mident = MailIdent(
            address=ident.address, provider=ident.provider,
            parent_account=ident.parent_account,
            secret=getattr(ident, "secret", None),
            imap_host=getattr(ident, "imap_host", None),
            meta=getattr(ident, "meta", {}) or {},
        )
        reg = self.registrar.register(
            email_addr, password, username,
            proxy=proxy, region=region,
            account_tag=f"acct{account_id}",
        )
        if not reg.ok:
            if reg.kind == "email_conflict" and ident.provider == "outlook_tag":
                self._flag(account_id, "tag_normalized")
                self._event(account_id, "register", "warning",
                            "疑似 +tag 被归一化，建议降权 outlook_tag",
                            {"screenshot": reg.screenshot})
            elif reg.kind == "rate_limited":
                self.proxies.mark_banned(proxy)
                self._flag(account_id, "proxy_dirty")
            self._fail(account_id, "register", f"{reg.kind}: {reg.error}")
            return RunReport(False, account_id, email_addr, "register",
                             f"{reg.kind}: {reg.error}", captcha_provider=reg.used_captcha)

        with session_scope() as s:
            a = s.get(Account, account_id)
            a.state = AccountState.REGISTERED
            a.register_ip = proxy.exit_ip_hint
        self._event(account_id, "register", "info",
                    f"注册成功（打码 {reg.used_captcha or '无'}，耗时 {reg.elapsed:.1f}s）",
                    {"screenshot": reg.screenshot, "url": reg.page_url})

        # ④ 验证
        vres = self.verifier.run(mident, proxy=proxy)
        if not vres.ok:
            self._mail_status(ident, MailStatus.DROPPED)
            self._flag(account_id, "mail_dropped")
            self._fail(account_id, "verify", vres.error)
            return RunReport(False, account_id, email_addr, "verify", vres.error,
                             captcha_provider=reg.used_captcha)

        with session_scope() as s:
            s.get(Account, account_id).state = AccountState.VERIFIED
        self._event(account_id, "verify", "info", "邮箱验证通过", {"link": vres.link})

        # ⑤ 设备重置（qoder-free 改造版，全量 9 步）
        rres = self.resetter.run(ResetOptions(
            steps=tuple(ALL_STEPS),
            dry_run=dry_run_device,
            preserve_chat=False,
            fake_hardware=bool(get(self.cfg, "device.fake_hardware", False)),
            timezone=tz,
            locale=get(self.cfg, f"region_profiles.{region}.locale", "en-US"),
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

        # ⑥ 领取
        cres = self.claimer.perform(email_addr, password)
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
