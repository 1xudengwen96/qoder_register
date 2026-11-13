"""网页注册：Playwright 驱动 + 反自动化硬化。

设计要点：
- 选择器集中放在 ``Selectors``，站点改版只改这一处；
- 验证码处理抽成 ``_handle_captcha``，接打码平台；
- 注册结果三元组：ok / normalized_hit（+tag 被当重复）/ error。
"""

from __future__ import annotations

import base64
import dataclasses
import random
import re
import string
import time
from pathlib import Path
from typing import Any

from app.net.captcha import CaptchaClient, CaptchaError
from app.net.proxy import Proxy, random_identity_for_region


@dataclasses.dataclass(slots=True)
class RegistSelectors:
    email_input: str = "input[type='email'], input[name='email']"
    password_input: str = "input[type='password'], input[name='password']"
    username_input: str = "input[name='username'], input[name='name']"
    submit_button: str = "button[type='submit']"
    captcha_frame: str = "iframe[src*='recaptcha'], iframe[src*='hcaptcha']"
    error_text: str = "[class*='error'], [role='alert']"
    success_marker: str = "text=/verify|verification|check your email|验证/i"


@dataclasses.dataclass(slots=True)
class RegisterOutcome:
    ok: bool
    normalized_hit: bool = False
    error: str = ""
    screenshot: str | None = None
    page_url: str = ""


def random_username(prefix: str = "u") -> str:
    body = "".join(random.choices(string.ascii_lowercase + string.digits, k=8))
    return f"{prefix}{body}"


def strong_password(length: int = 16) -> str:
    alphabet = string.ascii_letters + string.digits + "!@#$%^&*"
    while True:
        pw = "".join(random.choices(alphabet, k=length))
        if (any(c.islower() for c in pw) and any(c.isupper() for c in pw)
                and any(c.isdigit() for c in pw) and any(c in "!@#$%^&*" for c in pw)):
            return pw


NORMALIZED_PATTERNS = (
    r"already\s*(registered|exists|in\s*use)",
    r"email.*(taken|used)",
    r"已被注册", r"已注册", r"邮箱.*存在", r"该邮箱",
)


class Registrar:
    def __init__(
        self,
        signup_url: str,
        selectors: RegistSelectors | None = None,
        captcha: CaptchaClient | None = None,
        headless: bool = False,
        artifacts_dir: str | Path = "data/artifacts",
        extra_confirm_hosts: tuple[str, ...] = (),
        site_key_recaptcha: str | None = None,
        humanize: bool = True,
    ) -> None:
        self.signup_url = signup_url
        self.sel = selectors or RegistSelectors()
        self.captcha = captcha
        self.headless = headless
        self.artifacts_dir = Path(artifacts_dir)
        self.artifacts_dir.mkdir(parents=True, exist_ok=True)
        self.extra_confirm_hosts = extra_confirm_hosts
        self.site_key = site_key_recaptcha
        self.humanize = humanize

    # ---- 反自动化硬化 -----------------------------------------------------
    @staticmethod
    async def _harden(page: Any, region: str | None) -> None:
        ident = random_identity_for_region(region)
        await page.add_init_script("""
            Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
            if (!window.chrome) { window.chrome = { runtime: {} }; }
            Object.defineProperty(navigator, 'languages', {
                get: () => ['%(locale)s', 'en']
            });
            Object.defineProperty(navigator, 'plugins', { get: () => [1,2,3,4,5] });
        """ % {"locale": ident["locale"]})

    async def _human_type(self, locator, text: str) -> None:
        if not self.humanize:
            await locator.fill(text)
            return
        await locator.click()
        for ch in text:
            await locator.type(ch, delay=random.randint(40, 140))
        await locator.page.wait_for_timeout(random.randint(200, 600))

    # ---- 验证码 -----------------------------------------------------------
    async def _handle_captcha(self, page: Any, proxy: Proxy | None) -> bool:
        if self.captcha is None or not self.site_key:
            return True
        try:
            token = self.captcha.solve(
                "recaptcha_v2",
                site_key=self.site_key,
                page_url=page.url,
                proxy=proxy.raw if proxy else None,
            )
        except CaptchaError:
            return False
        if not token:
            return False
        await page.evaluate(
            """(t) => {
                const el = document.getElementById('g-recaptcha-response');
                if (el) { el.style.display='block'; el.value = t; }
            }""",
            token,
        )
        return True

    # ---- 主流程 -----------------------------------------------------------
    async def register(
        self,
        email_addr: str,
        password: str,
        username: str | None = None,
        proxy: Proxy | None = None,
        region: str | None = None,
    ) -> RegisterOutcome:
        from playwright.async_api import async_playwright  # 延迟导入

        username = username or random_username()
        shot: str | None = None
        try:
            async with async_playwright() as pw:
                browser = await pw.chromium.launch(
                    headless=self.headless,
                    proxy=proxy.playwright_proxy() if proxy else None,
                    args=[
                        "--disable-blink-features=AutomationControlled",
                        "--no-first-run",
                    ],
                )
                ctx_kwargs: dict[str, Any] = {
                    "viewport": {"width": random.choice([1366, 1440, 1536, 1920]),
                                 "height": random.choice([768, 900, 864, 1080])},
                    "locale": random_identity_for_region(region)["locale"],
                    "timezone_id": random_identity_for_region(region)["timezone"],
                }
                ctx = await browser.new_context(**ctx_kwargs)
                page = await ctx.new_page()
                await self._harden(page, region)

                await page.goto(self.signup_url, wait_until="domcontentloaded", timeout=60000)
                await page.wait_for_timeout(random.randint(800, 2000))

                await self._human_type(page.locator(self.sel.email_input).first, email_addr)
                await self._human_type(page.locator(self.sel.password_input).first, password)
                if await page.locator(self.sel.username_input).count():
                    await self._human_type(page.locator(self.sel.username_input).first, username)

                if await page.locator(self.sel.captcha_frame).count():
                    if not await self._handle_captcha(page, proxy):
                        shot = await self._shot(page, f"captcha_fail_{int(time.time())}")
                        return RegisterOutcome(False, error="captcha_failed", screenshot=shot,
                                               page_url=page.url)

                await page.locator(self.sel.submit_button).first.click()
                await page.wait_for_timeout(3000)

                body = await page.inner_text("body")
                normalized = any(re.search(p, body, re.I) for p in NORMALIZED_PATTERNS)
                shot = await self._shot(page, f"register_{int(time.time())}")
                ok = not (normalized and ("already" in body.lower() or "已注册" in body
                                          or "存在" in body))
                await browser.close()
                return RegisterOutcome(
                    ok=ok, normalized_hit=normalized, screenshot=shot, page_url=page.url,
                    error="" if ok else "email_conflict",
                )
        except Exception as exc:
            return RegisterOutcome(False, error=f"exception: {exc}")

    async def _shot(self, page: Any, name: str) -> str:
        path = self.artifacts_dir / f"{name}.png"
        try:
            await page.screenshot(path=str(path), full_page=True)
        except Exception:
            return ""
        return str(path)
