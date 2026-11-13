"""注册器：改用指纹浏览器引擎 + 打码调度器。

与旧版 ``registrar.py`` 的区别：
- 浏览器来自 ``FingerprintEngine``（AdsPower / BitBrowser / Camoufox / Playwright），
  每个号一个独立环境，指纹与代理地区对齐；
- 验证码走 ``CaptchaSolver`` 多供应商降级，不再写死单家；
- 支持 turnstile / hcaptcha / reCAPTCHA v3 的任务类型识别（按页面 DOM 判断）；
- 提交后判定：成功 / 邮箱冲突（+tag 归一化）/ 需二次验证 / 硬失败。
"""

from __future__ import annotations

import dataclasses
import json
import logging
import random
import re
import string
import time
from pathlib import Path
from typing import Any

from app.net.captcha_solver import CaptchaSolver, CaptchaTask, CaptchaError
from app.register.fingerprint_engine import BrowserSession, FingerprintEngine


@dataclasses.dataclass(slots=True)
class Selectors:
    email_input: str = "input[type='email'], input[name='email'], input[autocomplete='email']"
    password_input: str = "input[type='password'], input[name='password']"
    username_input: str = "input[name='username'], input[name='name'], input[id*='username']"
    submit: str = "button[type='submit'], form button, [role='button'][type='submit']"
    error_region: str = "[class*='error'], [role='alert'], [data-error]"
    success_hint: str = "text=/verify|verification|check your email|确认|验证/i"
    recaptcha_frame: str = "iframe[src*='recaptcha/api2/anchor'], iframe[src*='recaptcha/enterprise']"
    hcaptcha_frame: str = "iframe[src*='hcaptcha.com']"
    turnstile_frame: str = "iframe[src*='challenges.cloudflare.com']"
    image_captcha: str = "img#captcha-img, img[src*='captcha']"
    image_captcha_input: str = "input[name='captcha'], input[placeholder*='code' i]"


@dataclasses.dataclass(slots=True)
class RegisterResult:
    ok: bool
    kind: str = ""              # success / email_conflict / captcha_failed / network / unknown
    error: str = ""
    screenshot: str = ""
    page_url: str = ""
    used_captcha: str = ""
    elapsed: float = 0.0


def random_username(prefix: str = "u", length: int = 8) -> str:
    body = "".join(random.choices(string.ascii_lowercase + string.digits, k=length))
    return f"{prefix}{body}"


def strong_password(length: int = 18) -> str:
    alphabet = string.ascii_letters + string.digits + "!@#$%^&*()-_=+"
    while True:
        pw = "".join(random.choices(alphabet, k=length))
        if (any(c.islower() for c in pw) and any(c.isupper() for c in pw)
                and sum(c.isdigit() for c in pw) >= 2
                and any(c in "!@#$%^&*()-_=+" for c in pw)):
            return pw


CONFLICT_PATTERNS = (
    r"already\s*(registered|exists|in\s*use|taken)",
    r"email\s*(is\s*)?(taken|used|registered)",
    r"已被注册", r"已注册", r"邮箱已", r"该邮箱", r"账号已存在",
)
BLOCK_PATTERNS = (
    r"too\s*many\s*attempts", r"rate\s*limit", r"try\s*again\s*later",
    r"请求过于频繁", r"操作太频繁", r"请稍后再试",
)


class BrowserRegistrar:
    def __init__(
        self,
        engine: FingerprintEngine,
        solver: CaptchaSolver | None,
        signup_url: str,
        selectors: Selectors | None = None,
        artifacts_dir: str | Path = "data/artifacts",
        recaptcha_site_key: str | None = None,
        hcaptcha_site_key: str | None = None,
        turnstile_site_key: str | None = None,
        logger: logging.Logger | None = None,
        humanize: bool = True,
        page_timeout: int = 60000,
    ) -> None:
        self.engine = engine
        self.solver = solver
        self.signup_url = signup_url
        self.sel = selectors or Selectors()
        self.artifacts_dir = Path(artifacts_dir)
        self.artifacts_dir.mkdir(parents=True, exist_ok=True)
        self.site_keys = {
            "recaptcha_v2": recaptcha_site_key,
            "hcaptcha": hcaptcha_site_key,
            "turnstile": turnstile_site_key,
        }
        self.log = logger or logging.getLogger("qoder.register")
        self.humanize = humanize
        self.page_timeout = page_timeout

    # ---------------------------------------------------------- 人味操作
    def _sleep(self, lo: int = 300, hi: int = 1200) -> None:
        time.sleep(random.randint(lo, hi) / 1000)

    async def _type_like_human(self, page: Any, selector: str, text: str) -> None:
        loc = page.locator(selector).first
        await loc.scroll_into_view_if_needed()
        await loc.click()
        if not self.humanize:
            await loc.fill(text)
            return
        for ch in text:
            await loc.type(ch, delay=random.randint(45, 165))
        await page.wait_for_timeout(random.randint(180, 700))

    async def _move_mouse_randomly(self, page: Any, times: int = 3) -> None:
        if not self.humanize:
            return
        try:
            vp = page.viewport_size or {"width": 1920, "height": 1080}
            for _ in range(times):
                await page.mouse.move(
                    random.randint(80, vp["width"] - 80),
                    random.randint(80, vp["height"] - 80),
                    steps=random.randint(8, 24),
                )
                await page.wait_for_timeout(random.randint(120, 420))
        except Exception:
            pass

    # ---------------------------------------------------------- 验证码
    async def _detect_captcha(self, page: Any) -> str | None:
        for kind, sel in (
            ("turnstile", self.sel.turnstile_frame),
            ("hcaptcha", self.sel.hcaptcha_frame),
            ("recaptcha_v2", self.sel.recaptcha_frame),
        ):
            if await page.locator(sel).count():
                return kind
        if await page.locator(self.sel.image_captcha).count():
            return "image"
        return None

    async def _solve_captcha(self, page: Any, kind: str, proxy_raw: str | None) -> str:
        if self.solver is None:
            raise CaptchaError("未配置打码平台")

        if kind == "image":
            el = page.locator(self.sel.image_captcha).first
            raw = await el.screenshot()
            import base64
            b64 = base64.b64encode(raw).decode()
            res = self.solver.solve(CaptchaTask(kind="image", image_b64=b64, proxy=proxy_raw))
            field = page.locator(self.sel.image_captcha_input).first
            await field.fill(res.token)
            return res.provider

        site_key = self.site_keys.get(kind)
        if not site_key:
            raise CaptchaError(f"缺少 {kind} 的 siteKey 配置")
        res = self.solver.solve(CaptchaTask(
            kind=kind, page_url=page.url, site_key=site_key, proxy=proxy_raw,
        ))

        if kind in ("recaptcha_v2", "hcaptcha", "turnstile"):
            await page.evaluate(
                """([kind, token]) => {
                    const ids = {
                        recaptcha_v2: ['g-recaptcha-response'],
                        hcaptcha: ['h-captcha-response', 'g-recaptcha-response'],
                        turnstile: ['cf-turnstile-response']
                    }[kind] || [];
                    for (const id of ids) {
                        const el = document.getElementById(id) ||
                                   document.querySelector(`[name="${id}"]`);
                        if (el) { el.style.display = 'block'; el.value = token; }
                    }
                    // 触发回调
                    for (const fn of ['onCaptchaSuccess','onSuccess','__turnstile_cb']) {
                        if (typeof window[fn] === 'function') {
                            try { window[fn](token); } catch (e) {}
                        }
                    }
                }""",
                [kind, res.token],
            )
            # turnstile / enterprise 会在 iframe 里放隐藏 input
            for name in ("g-recaptcha-response", "h-captcha-response", "cf-turnstile-response"):
                hidden = page.locator(f"textarea[name='{name}'], input[name='{name}']")
                if await hidden.count():
                    await hidden.first.evaluate(
                        "(el, t) => { el.value = t; el.dispatchEvent(new Event('change')); }",
                        res.token,
                    )
        return res.provider

    # ---------------------------------------------------------- 结果判定
    async def _classify(self, page: Any) -> RegisterResult:
        await page.wait_for_timeout(2500)
        body = ""
        try:
            body = (await page.inner_text("body"))[:8000]
        except Exception:
            pass
        low = body.lower()

        for pat in CONFLICT_PATTERNS:
            if re.search(pat, low):
                return RegisterResult(False, kind="email_conflict",
                                      error="邮箱已被占用", page_url=page.url)
        for pat in BLOCK_PATTERNS:
            if re.search(pat, low):
                return RegisterResult(False, kind="rate_limited",
                                      error="被限流", page_url=page.url)

        # 成功标志：出现"查收邮件 / 验证"提示，或跳转到验证页
        if re.search(r"verify|verification|check your email|确认|验证", low):
            return RegisterResult(True, kind="success", page_url=page.url)
        if re.search(r"welcome|dashboard|工作台|欢迎", low):
            return RegisterResult(True, kind="success", page_url=page.url)

        errs = page.locator(self.sel.error_region)
        if await errs.count():
            try:
                text = (await errs.first.inner_text())[:300]
            except Exception:
                text = ""
            if text:
                return RegisterResult(False, kind="form_error", error=text, page_url=page.url)
        return RegisterResult(False, kind="unknown", error="未能判定结果", page_url=page.url)

    async def _shot(self, page: Any, name: str) -> str:
        path = self.artifacts_dir / f"{name}.png"
        try:
            await page.screenshot(path=str(path), full_page=True)
        except Exception:
            return ""
        return str(path)

    # ---------------------------------------------------------- 主流程
    async def register_async(
        self,
        email_addr: str,
        password: str,
        username: str | None = None,
        proxy=None,
        region: str = "us",
        account_tag: str = "",
    ) -> RegisterResult:
        t0 = time.time()
        username = username or random_username()
        session: BrowserSession | None = None
        used_captcha = ""
        try:
            session = self.engine.open(proxy=proxy, region=region,
                                       account_tag=account_tag or email_addr.split("@")[0])
            page = session.page
            await page.goto(self.signup_url, wait_until="domcontentloaded",
                            timeout=self.page_timeout)
            await page.wait_for_timeout(random.randint(1200, 2600))
            await self._move_mouse_randomly(page)

            # 邮箱
            await self._type_like_human(page, self.sel.email_input, email_addr)
            await self._sleep()
            # 密码（两步式表单时可能还要确认框）
            await self._type_like_human(page, self.sel.password_input, password)
            confirm = page.locator("input[name*='confirm'], input[id*='confirm']")
            if await confirm.count():
                await self._type_like_human(
                    page, "input[name*='confirm'], input[id*='confirm']", password)
            # 用户名
            if await page.locator(self.sel.username_input).count():
                await self._type_like_human(page, self.sel.username_input, username)

            await self._move_mouse_randomly(page, times=2)

            # 验证码
            kind = await self._detect_captcha(page)
            if kind:
                used_captcha = await self._solve_captcha(
                    page, kind, proxy.raw if proxy else None)
                await page.wait_for_timeout(random.randint(800, 1800))

            # 提交
            btn = page.locator(self.sel.submit).first
            await btn.scroll_into_view_if_needed()
            await btn.click()
            await page.wait_for_timeout(random.randint(1500, 3000))

            result = await self._classify(page)

            # 提交后二次弹验证码
            if not result.ok and result.kind == "unknown":
                kind2 = await self._detect_captcha(page)
                if kind2:
                    used_captcha = await self._solve_captcha(
                        page, kind2, proxy.raw if proxy else None)
                    await page.locator(self.sel.submit).first.click()
                    await page.wait_for_timeout(2500)
                    result = await self._classify(page)

            result.screenshot = await self._shot(
                page, f"{result.kind}_{int(time.time())}")
            result.used_captcha = used_captcha
            result.elapsed = time.time() - t0
            return result

        except CaptchaError as exc:
            return RegisterResult(False, kind="captcha_failed", error=str(exc),
                                  elapsed=time.time() - t0)
        except Exception as exc:
            return RegisterResult(False, kind="exception", error=str(exc),
                                  elapsed=time.time() - t0)
        finally:
            if session:
                session.close()

    def register(self, *args, **kwargs) -> RegisterResult:
        import asyncio
        return asyncio.run(self.register_async(*args, **kwargs))
