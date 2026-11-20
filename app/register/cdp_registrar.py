"""CDP 注册器：比特浏览器开窗口（代理自动写入）→ 纯 CDP 跑完注册。

与 ``browser_registrar.BrowserRegistrar`` 的分工
------------------------------------------------
- ``BrowserRegistrar``：走 Playwright 的 page 对象，需要 node 驱动进程。
  本机沙箱里 ``asyncio`` 建命名管道被拒（WinError 5），这条路起不来。
- ``CDPRegistrar``（本模块）：连比特浏览器暴露的原生 CDP WebSocket，
  只用 socket + json + base64，不 spawn 任何子进程。

流程
----
① 取代理（``host:port:user:pass`` 已被 ``app.net.proxy`` 归一化）
② ``bb_cdp.open_for_register``：定位/新建比特环境 → 写代理 → 启动 → 校验 CDP
③ ``new_page`` 开出可控标签页
④ ``QoderSignupCDP.run``：姓名/邮箱 → 密码 → 阿里云滑块（2Captcha）→ 邮箱验证码
⑤ 收尾：截图存档、关环境（``keep_open`` 可留）
"""

from __future__ import annotations

import dataclasses
import logging
import os
import time
from pathlib import Path

from app.net.proxy import Proxy

log = logging.getLogger("cdp-registrar")


@dataclasses.dataclass(slots=True)
class CDPRegisterResult:
    ok: bool
    kind: str = ""
    error: str = ""
    stage: str = ""
    screenshot: str = ""
    page_url: str = ""
    used_captcha: str = ""
    elapsed: float = 0.0
    env_id: str = ""
    endpoint: str = ""
    proxy: str = ""
    stages: list = dataclasses.field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "ok": self.ok, "kind": self.kind, "error": self.error,
            "stage": self.stage, "env_id": self.env_id,
            "endpoint": self.endpoint, "proxy": self.proxy,
            "captcha": self.used_captcha, "elapsed": round(self.elapsed, 1),
            "url": self.page_url, "screenshot": self.screenshot,
        }


_STAGE_KIND = {
    "step1": "signup_step1",
    "step2": "signup_step2",
    "step3": "captcha_failed",
    "step4": "otp_failed",
    "exception": "exception",
    "init": "init",
}


class CDPRegistrar:
    """纯 CDP 注册。"""

    def __init__(
        self,
        cfg=None,
        *,
        client=None,
        api_base: str = "http://127.0.0.1:54345",
        group_id: str | None = None,
        env_id: str = "",
        reuse_env: bool = False,
        signup_url: str = "https://qoder.com/users/sign-up",
        artifacts_dir: str | Path = "data/artifacts",
        captcha_max_attempts: int = 6,
        otp_timeout: int = 240,
        imap_proxy: str | None = None,
        headless: bool = False,
        keep_open: bool = False,
        env_pool=None,
        logger: logging.Logger | None = None,
        manual_captcha: bool = False,
        manual_timeout: int = 600,
        manual_otp: bool = False,
        manual_form: bool = False,
    ) -> None:
        if client is None:
            from app.register.bitbrowser import BitBrowserClient

            token = None
            try:
                from app import secrets

                token = secrets.get("bitbrowser.token", None)
            except Exception:  # noqa: BLE001
                token = None
            client = BitBrowserClient(api_base, token=token)
        self.cfg = cfg
        self.client = client
        self.group_id = group_id
        self.env_id = env_id
        self.reuse_env = reuse_env
        self.signup_url = signup_url
        self.artifacts_dir = Path(artifacts_dir)
        self.artifacts_dir.mkdir(parents=True, exist_ok=True)
        self.captcha_max_attempts = captcha_max_attempts
        self.otp_timeout = otp_timeout
        self.imap_proxy = imap_proxy
        self.headless = headless
        self.keep_open = keep_open
        # 窗口池（bb_pool.EnvPool）：非 None 时不再每号新建环境，
        # 而是随机挑空闲池窗口重配指纹+代理+清残留后复用。
        self.env_pool = env_pool
        self.log = logger or log
        # 人工滑块：True 时③步暂停等用户手滑，通过后自动接管
        self.manual_captcha = manual_captcha
        self.manual_timeout = manual_timeout
        self.manual_otp = manual_otp
        self.manual_form = manual_form

    # ------------------------------------------------------------ 打码
    def _captcha_key(self) -> str:
        key = os.environ.get("TWOCAPTCHA_KEY", "")
        if key:
            return key
        try:
            from app import secrets

            return str(secrets.get("captcha.twocaptcha_key", "") or "")
        except Exception:  # noqa: BLE001
            return ""

    def _paid_client(self):
        key = self._captcha_key()
        paid = None
        if key:
            from app.net.twocaptcha_dragdrop import TwoCaptchaDragDrop

            paid = TwoCaptchaDragDrop(key, proxy=self.imap_proxy)
            try:
                self.log.info("2Captcha 余额 $%.4f", paid.balance())
            except Exception as exc:  # noqa: BLE001
                self.log.warning("2Captcha 余额查询失败（不影响识别）：%s", exc)
        else:
            self.log.warning("没配 2Captcha key，滑块只走本地识别")
        # 超级鹰试验通道：启用时优先，API 异常自动回退 2Captcha
        from app.net.chaojiying import build_slider_paid

        wrapped = build_slider_paid(paid)
        if wrapped is not paid:
            try:
                self.log.info("超级鹰优先（题分 %.0f），2Captcha 兜底",
                              wrapped.primary.balance())
            except Exception as exc:  # noqa: BLE001
                self.log.warning("超级鹰题分查询失败（不影响识别）：%s", exc)
        return wrapped

    # ------------------------------------------------------------ 健康
    def health(self) -> tuple[bool, str]:
        if not self.client.is_port_open():
            return False, f"比特浏览器本地服务未监听（{self.client.base}）"
        if not self.client.health():
            return False, "/health 不通"
        try:
            _, total = self.client.list_envs(page=0, page_size=1)
        except Exception as exc:  # noqa: BLE001
            return False, f"列表失败：{exc}"
        return True, f"服务正常，已有环境 {total} 个"

    # ------------------------------------------------------------ 主流程
    def register(
        self,
        email: str,
        password: str | None = None,
        *,
        proxy: Proxy | None = None,
        region: str = "us",
        account_tag: str = "",
        env_name: str = "",
        code_source=None,
        stop_before: str = "",
    ) -> CDPRegisterResult:
        """跑一次注册。``code_source`` 是 ``otp_source.CodeSource`` 或可调用对象。"""
        from app.register import bb_cdp
        from app.register.cdp_page import new_page
        from app.register.qoder_signup_cdp import QoderSignupCDP

        t0 = time.time()
        res = CDPRegisterResult(ok=False)
        launch = None
        page = None
        used_pool = False
        try:
            if self.env_pool is not None and not self.env_id:
                launch = self.env_pool.acquire(
                    account_tag or email, region=region,
                    proxy=proxy, headless=self.headless)
                used_pool = True
            else:
                launch = bb_cdp.open_for_register(
                    self.client,
                    proxy=proxy,
                    region=region,
                    account_tag=account_tag,
                    env_name=env_name,
                    env_id=self.env_id,
                    group_id=self.group_id,
                    reuse_env=self.reuse_env,
                    headless=self.headless,
                )
            res.env_id = launch.env_id
            res.endpoint = launch.endpoint
            res.proxy = launch.proxy_label
            self.log.info("环境就绪 env=%s cdp=%s 代理=%s（%s）",
                          launch.env_name, launch.endpoint, launch.proxy_label,
                          "新建" if launch.created else "复用")

            page = new_page(launch.endpoint, "about:blank", reuse_blank=True)
            # OS 级真实输入：CDP 注入的 pointermove 永远单 coalesced 样本，
            # 会被 Baxia 行为遥测识破（2026-11-19 取证实锤）；挂上后
            # 填表/点击走 SendInput，失败自动退回 CDP 不阻断流程
            try:
                from app.register.os_input import OSInput

                page.os_input = OSInput(page, launch.pid)
                if page.os_input.ready:
                    self.log.info("填表输入=OS 级 SendInput（hwnd=%s）",
                                  page.os_input.hwnd)
                else:
                    self.log.warning("OS 输入未就绪（首次动作时重试校准，"
                                     "失败则退 CDP）pid=%s", launch.pid)
            except Exception as exc:  # noqa: BLE001
                self.log.warning("OS 输入器初始化异常（用 CDP 注入）：%s", exc)
            paid = self._paid_client()
            flow = QoderSignupCDP(
                page,
                artifacts_dir=str(self.artifacts_dir),
                human=True,
                logger=self.log,
                captcha_max_attempts=self.captcha_max_attempts,
                proxy=(proxy.raw if proxy else None),
                paid_client=paid,
                prefer_paid=paid is not None,
                manual_captcha=self.manual_captcha,
                manual_timeout=self.manual_timeout,
                manual_otp=self.manual_otp,
                manual_form=self.manual_form,
            )
            provider = code_source if callable(code_source) else None
            rep = flow.run(email, password, code_provider=provider,
                           stop_before=stop_before)

            res.stages = [
                {"name": s.name, "ok": s.ok, "detail": s.detail,
                 "elapsed": s.elapsed}
                for s in rep.stages
            ]
            if rep.result is not None:
                res.stage = rep.result.stage
                res.page_url = rep.result.url or ""
                res.screenshot = rep.result.screenshot or ""
            res.ok = bool(rep.ok)
            if rep.ok:
                res.kind = "success"
            else:
                res.kind = _STAGE_KIND.get(res.stage, "failed")
                res.error = (rep.result.detail if rep.result else "") or "注册未完成"
            res.used_captcha = "2captcha" if paid else "local"
            return res
        except Exception as exc:  # noqa: BLE001
            res.kind = "exception"
            res.error = f"{type(exc).__name__}: {exc}"
            self.log.exception("CDP 注册异常")
            return res
        finally:
            res.elapsed = time.time() - t0
            try:
                if page is not None:
                    if not res.ok:
                        # 失败：导回空白页再断连，不给下次启动留旧标签
                        try:
                            page.goto("about:blank", wait_ms=200)
                        except Exception:  # noqa: BLE001
                            pass
                    page.close()
            except Exception:  # noqa: BLE001
                pass
            if launch and used_pool:
                if res.ok:
                    # 成功：窗口留开并锁定，领取段还要复用注册 Cookie
                    # 做免密授权；领取完成由 orchestrator 调 finish_claim。
                    self.env_pool.mark_claim_hold(launch.env_id)
                    if account_tag:
                        self.env_pool.remember_account(
                            account_tag, launch.env_id, launch.env_name)
                    self.log.info("窗口 %s 留开锁定，等待领取段复用注册 Cookie",
                                  launch.env_name)
                elif self.env_pool.is_temp(launch.env_id):
                    self.env_pool.delete_temp(launch.env_id)
                    self.log.info("溢出临时窗口 %s 已删除", launch.env_name)
                else:
                    bb_cdp.close_env(self.client, launch.env_id)
                    self.env_pool.release(launch.env_id)
                    self.log.info("窗口 %s 已关闭回池（下次用前重配+清残留）",
                                  launch.env_name)
            elif launch and not self.keep_open:
                bb_cdp.close_env(self.client, launch.env_id)
                self.log.info("已关闭环境 %s", launch.env_name)

    # ------------------------------------------------------------ 兼容旧接口
    def open_session(self, proxy: Proxy | None = None, region: str = "us",
                     account_tag: str = "") -> object:
        """只要一个开好代理的 CDP 页面（给探索性脚本用）。"""
        from app.register import bb_cdp
        from app.register.cdp_page import new_page

        launch = bb_cdp.open_for_register(
            self.client, proxy=proxy, region=region, account_tag=account_tag,
            env_id=self.env_id, group_id=self.group_id,
            reuse_env=self.reuse_env, headless=self.headless,
        )
        return launch, new_page(launch.endpoint, "about:blank")
