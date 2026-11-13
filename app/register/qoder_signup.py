"""Qoder 注册流程（2026-11-12 实测版）。

注册页：https://qoder.com/users/sign-up

实测四步
--------
① 姓名 + 邮箱 + 勾选条款  → Continue
     input#basic_firstName   placeholder="Enter your first name"
     input#basic_lastName    placeholder="Enter your Last Name"
     input#basic_email       placeholder="Enter your email address"
     input[type=checkbox]    Agree to the Terms
     button[type=submit]     "Continue"

② 设密码  → Continue
     input#basic_password    autocomplete="new-password"
     （此步 email 输入框仍在，值保留）

③ 阿里云滑块「Click to verify」→ 求解
     #aliyunCaptcha-img / #aliyunCaptcha-puzzle
     #aliyunCaptcha-sliding-body / #aliyunCaptcha-sliding-slider
     用 AliyunSliderSolver 处理

④ 6 位邮箱验证码  → Continue
     input#basic_code / input[autocomplete=one-time-code]
     邮件从 Outlook OAuth2 取，正则 \\d{6}

与旧版（sign-in 流程）的区别
---------------------------
旧代码走 ``/users/sign-in``（只要邮箱+验证码），实际注册页是 ``/users/sign-up``，
且必须提供 First/Last Name 和密码。
"""

from __future__ import annotations

import asyncio
import dataclasses
import random
import re
from typing import Callable

SIGNUP_URL = "https://qoder.com/users/sign-up"
CODE_RE = re.compile(r"\b(\d{6})\b")

FIRST_NAMES = ["James", "Emily", "Michael", "Sarah", "David", "Jessica",
               "Daniel", "Ashley", "Matthew", "Amanda", "Ryan", "Sophia",
               "William", "Olivia", "Henry", "Emma"]
LAST_NAMES = ["Smith", "Johnson", "Williams", "Brown", "Jones", "Garcia",
              "Miller", "Davis", "Rodriguez", "Martinez", "Wilson", "Anderson",
              "Taylor", "Thomas", "Moore", "Jackson"]


def pick_name(seed: str) -> tuple[str, str]:
    """由邮箱确定姓名，保证同一邮箱每次一致。"""
    import hashlib
    h = hashlib.sha256(seed.encode()).hexdigest()
    return (FIRST_NAMES[int(h[0:4], 16) % len(FIRST_NAMES)],
            LAST_NAMES[int(h[4:8], 16) % len(LAST_NAMES)])


def gen_password(length: int = 14) -> str:
    """生成符合常见强度要求的密码。"""
    alphabet = "abcdefghijkmnpqrstuvwxyzABCDEFGHJKLMNPQRSTUVWXYZ23456789"
    rnd = random.SystemRandom()
    core = "".join(rnd.choice(alphabet) for _ in range(length - 2))
    return core + rnd.choice("!@#%") + rnd.choice("23456789")


@dataclasses.dataclass(slots=True)
class SignupResult:
    ok: bool
    stage: str = ""
    detail: str = ""
    email: str = ""
    password: str = ""
    first_name: str = ""
    last_name: str = ""
    url: str = ""
    captcha_attempts: int = 0
    code: str = ""
    screenshot: str = ""


class QoderSignup:
    """驱动 Qoder 注册页。

    ``code_provider``：无参可调用对象，返回 6 位验证码（同步或异步）。
    ``artifacts_dir``：截图保存目录。
    """

    def __init__(
        self,
        page,
        code_provider: Callable[[], object],
        artifacts_dir: str = "data/artifacts",
        humanize: bool = True,
        logger=None,
        captcha_max_attempts: int = 8,
        paid_client=None,
        prefer_paid: bool = True,
        proxy: str | None = "http://127.0.0.1:7897",
    ) -> None:
        self.page = page
        self.code_provider = code_provider
        self.artifacts_dir = artifacts_dir
        self.humanize = humanize
        self.log = logger
        self.captcha_max_attempts = captcha_max_attempts
        self.paid_client = paid_client
        self.prefer_paid = prefer_paid
        self.proxy = proxy

    def _d(self, msg: str) -> None:
        if self.log:
            self.log.info("[signup] %s", msg)

    async def _sleep(self, lo: float, hi: float) -> None:
        await asyncio.sleep(random.uniform(lo, hi))

    async def _type(self, selector: str, text: str,
                    delay: tuple[int, int] = (45, 140)) -> bool:
        """逐字符输入，模拟真人节奏。"""
        loc = self.page.locator(selector).first
        if await loc.count() == 0:
            return False
        await loc.scroll_into_view_if_needed()
        await loc.click()
        if not self.humanize:
            await loc.fill(text)
            return True
        for ch in text:
            await loc.type(ch, delay=random.randint(*delay))
        await self._sleep(0.2, 0.7)
        return True

    async def _shot(self, name: str) -> str:
        import time
        from pathlib import Path
        d = Path(self.artifacts_dir)
        d.mkdir(parents=True, exist_ok=True)
        p = d / f"signup_{name}_{int(time.time())}.png"
        try:
            await self.page.screenshot(path=str(p), full_page=True)
            return str(p)
        except Exception:
            return ""

    async def _fill_otp(self, code: str) -> bool:
        """填 6 位 OTP 验证码。

        实测 Qoder 用的是 **Ant Design 的 OTP Input**：
            <input type="text" maxlength="6" inputmode="numeric"
                   autocomplete="one-time-code"
                   class="ant-input ant-input-outlined realInput--oCoXlfv">

        坑：Playwright 的 ``type()`` 能让 DOM 的 ``value`` 变成正确的 6 位，
        但 **React 的受控 state 不会同步**（Ant Design 用 onChange 驱动），
        所以页面上显示对了、却没有触发提交。

        解法：用 **CDP 发送真实键盘事件**（keyDown/keyUp + text），
        React 只认原生事件流。
        """
        loc = self.page.locator(
            "input[autocomplete='one-time-code'], "
            "input[maxlength='6'][inputmode='numeric'], "
            "input[maxlength='6']").first
        if await loc.count() == 0:
            self._d("找不到 OTP 输入框")
            return False

        digits = [c for c in code if c.isdigit()][:6]
        if not digits:
            return False

        # 清空
        try:
            await loc.click(timeout=5000)
            await loc.fill("")
            await asyncio.sleep(0.2)
        except Exception:
            pass

        # CDP 真实键盘输入
        try:
            cdp = await self.page.context.new_cdp_session(self.page)
            for ch in digits:
                # keyDown 带 text，浏览器会插入字符并触发完整事件链
                await cdp.send("Input.dispatchKeyEvent", {
                    "type": "keyDown",
                    "text": ch,
                    "key": ch,
                    "code": f"Digit{ch}",
                    "windowsVirtualKeyCode": ord(ch),
                    "nativeVirtualKeyCode": ord(ch),
                })
                await cdp.send("Input.dispatchKeyEvent", {
                    "type": "keyUp",
                    "key": ch,
                    "code": f"Digit{ch}",
                    "windowsVirtualKeyCode": ord(ch),
                    "nativeVirtualKeyCode": ord(ch),
                })
                await asyncio.sleep(random.uniform(0.06, 0.16))
            self._d(f"CDP 键盘已输入 {len(digits)} 位")
        except Exception as exc:
            self._d(f"CDP 键盘失败: {type(exc).__name__}: {exc}，退回 type()")
            try:
                await loc.click()
                await loc.type("".join(digits), delay=80)
            except Exception:
                return False

        # 校验 DOM 值 + React 状态
        await asyncio.sleep(0.5)
        try:
            val = await loc.input_value()
        except Exception:
            val = ""
        self._d(f"OTP 填后 value={val}")

        # 若 DOM 值不对，退回 Playwright fill（至少保证 DOM 正确）
        if val != "".join(digits):
            try:
                await loc.fill("".join(digits))
                await loc.dispatch_event("input")
                await loc.dispatch_event("change")
                self._d(f"已用 fill 兜底，value={await loc.input_value()}")
            except Exception:
                pass

        # 触发 blur，让 Ant Design 提交
        try:
            await loc.press("Tab")
        except Exception:
            pass
        return True

    async def _click_continue(self, timeout: int = 8000) -> bool:
        for sel in ('button:has-text("Continue")', 'button[type="submit"]'):
            try:
                loc = self.page.locator(sel).first
                if await loc.count() > 0 and await loc.is_visible():
                    await loc.click(timeout=timeout)
                    return True
            except Exception:
                continue
        return False

    # ---------------------------------------------------------- 主流程
    async def run(self, email_addr: str,
                  password: str | None = None) -> SignupResult:
        pwd = password or gen_password()
        first, last = pick_name(email_addr)
        self.page.set_default_timeout(30000)

        try:
            # ---- 预热：先逛首页建立正常会话 ----
            self._d("预热首页")
            try:
                await self.page.goto("https://qoder.com/",
                                     wait_until="domcontentloaded", timeout=60000)
                await self._sleep(2, 4)
                await self.page.mouse.move(random.randint(200, 800),
                                           random.randint(150, 600),
                                           steps=random.randint(8, 20))
            except Exception:
                pass

            # ---- ① 姓名 + 邮箱 + 勾选 ----
            self._d(f"打开注册页 {SIGNUP_URL}")
            await self.page.goto(SIGNUP_URL, wait_until="domcontentloaded",
                                 timeout=60000)
            await self._sleep(2, 3.5)

            if not await self._type("#basic_firstName", first):
                return SignupResult(False, "step1", "找不到 firstName 输入框",
                                    email_addr, pwd, first, last)
            if not await self._type("#basic_lastName", last):
                return SignupResult(False, "step1", "找不到 lastName 输入框",
                                    email_addr, pwd, first, last)
            if not await self._type("#basic_email", email_addr, (45, 110)):
                return SignupResult(False, "step1", "找不到 email 输入框",
                                    email_addr, pwd, first, last)

            cb = self.page.locator("input[type=checkbox]").first
            if await cb.count() > 0:
                if not await cb.is_checked():
                    await cb.check(force=True)
                self._d(f"条款已勾选: {await cb.is_checked()}")

            await self._sleep(0.6, 1.4)
            if not await self._click_continue():
                return SignupResult(False, "step1", "点不到 Continue",
                                    email_addr, pwd, first, last)
            self._d("① 姓名邮箱已提交")
            await self._sleep(4, 6)

            # ---- ② 密码 ----
            pwd_sel = "input#basic_password, input[autocomplete=new-password]"
            pwd_loc = self.page.locator(pwd_sel).first
            if await pwd_loc.count() > 0 and await pwd_loc.is_visible():
                if not await self._type(pwd_sel, pwd, (40, 120)):
                    return SignupResult(False, "step2", "填密码失败",
                                        email_addr, pwd, first, last)
                await self._sleep(0.6, 1.2)
                if not await self._click_continue():
                    return SignupResult(False, "step2", "密码步点不到 Continue",
                                        email_addr, pwd, first, last)
                self._d("② 密码已提交")
                await self._sleep(5, 7)
            else:
                self._d("未出现密码步骤（可能流程变了）")

            # ---- ③ 滑块 ----
            from app.register.aliyun_slider import AliyunSliderSolver

            solver = AliyunSliderSolver(self.page, human=self.humanize,
                                        logger=self.log,
                                        paid_client=self.paid_client,
                                        prefer_paid=self.prefer_paid,
                                        proxy=self.proxy)

            st = await self.page.evaluate(
                """() => ({
                  hasImg: !!document.querySelector('#aliyunCaptcha-img'),
                  bodyText: (document.body.innerText || '').slice(0, 300),
                })""")

            cap_attempts = 0
            if st.get("hasImg"):
                self._d("③ 检测到阿里云滑块，开始求解")
                res = await solver.solve(max_attempts=self.captcha_max_attempts)
                cap_attempts = res.attempts
                if not res.ok:
                    shot = await self._shot("captcha_fail")
                    return SignupResult(False, "step3",
                                        f"滑块未通过: {res.detail}",
                                        email_addr, pwd, first, last,
                                        captcha_attempts=cap_attempts,
                                        screenshot=shot)
                self._d(f"③ 滑块通过（{cap_attempts} 次）")
                await self._sleep(2, 4)
            else:
                self._d(f"③ 未检测到滑块。页面文本: {st.get('bodyText','')[:120]}")

            # ---- ④ 验证码 ----
            code_sel = ("input#basic_code, input[autocomplete=one-time-code], "
                        "input[id*=code], input[placeholder*=code i]")
            code_loc = self.page.locator(code_sel).first
            if await code_loc.count() == 0:
                # 等一会儿，验证码输入框可能延迟出现
                for _ in range(10):
                    await self._sleep(1, 1.5)
                    if await code_loc.count() > 0:
                        break

            if await code_loc.count() == 0:
                body = await self.page.evaluate(
                    "() => (document.body.innerText || '').slice(0, 400)")
                shot = await self._shot("no_code_input")
                return SignupResult(False, "step4",
                                    f"找不到验证码输入框。页面: {body[:150]}",
                                    email_addr, pwd, first, last,
                                    captcha_attempts=cap_attempts,
                                    screenshot=shot)

            self._d("④ 等待邮箱验证码")
            code_obj = self.code_provider()
            if asyncio.iscoroutine(code_obj):
                code_obj = await code_obj
            code = str(code_obj or "").strip()
            if not code:
                shot = await self._shot("no_code_mail")
                return SignupResult(False, "step4", "未取到验证码",
                                    email_addr, pwd, first, last,
                                    captcha_attempts=cap_attempts,
                                    screenshot=shot)

            self._d(f"④ 收到验证码 {code}")

            # ---- 填验证码 ----
            # 实测：Qoder 用 **6 个独立的单字符输入框**（maxlength=1），
            # 页面显示为 "3 7 6 7 4 6"。
            # 必须逐格填，且每格要触发 input 事件，最后一格填完才会自动提交。
            filled = await self._fill_otp(code)
            if not filled:
                # 兜底：当普通输入框处理
                if not await self._type(code_sel, code, (50, 130)):
                    return SignupResult(False, "step4", "填验证码失败",
                                        email_addr, pwd, first, last, code,
                                        captcha_attempts=cap_attempts)

            await self._sleep(1.0, 1.8)
            clicked = False
            for name in ("Continue", "Verify", "Sign up", "Submit"):
                try:
                    btn = self.page.get_by_role("button", name=name).first
                    if await btn.count() > 0 and await btn.is_visible():
                        await btn.click()
                        clicked = True
                        break
                except Exception:
                    continue
            if not clicked:
                await self.page.keyboard.press("Enter")

            self._d("④ 验证码已提交")
            await self._sleep(8, 12)

            # ---- 确认结果 ----
            url = self.page.url
            body = await self.page.evaluate(
                "() => (document.body.innerText || '').slice(0, 500)")
            shot = await self._shot("final")

            ok_markers = ("welcome", "dashboard", "get started", "/qoder",
                          "sign out", "logout", "workspace")
            low = body.lower()
            logged_in = ("/users/sign-up" not in url) or any(
                m in low for m in ok_markers)

            if logged_in:
                return SignupResult(True, "done", f"注册完成 url={url}",
                                    email_addr, pwd, first, last, url,
                                    captcha_attempts=cap_attempts, code=code,
                                    screenshot=shot)

            # 失败时 dump 页面结构，便于定位提交方式
            try:
                import json as _json
                from pathlib import Path as _Path
                dump = await self.page.evaluate(
                    """() => {
                      const vis = (e) => !!(e.offsetWidth || e.offsetHeight ||
                                            e.getClientRects().length);
                      return {
                        url: location.href,
                        inputs: Array.from(document.querySelectorAll('input'))
                          .filter(vis).map(e => ({
                            type: e.type, id: e.id, name: e.name,
                            maxlength: e.maxLength, inputmode: e.inputMode,
                            ac: e.autocomplete, value: (e.value||''),
                            cls: (typeof e.className === 'string'
                                  ? e.className : '').slice(0, 60),
                          })),
                        buttons: Array.from(document.querySelectorAll('button'))
                          .map(e => ({
                            text: (e.innerText||'').trim().slice(0, 40),
                            type: e.type, id: e.id, disabled: e.disabled,
                            visible: vis(e),
                            cls: (typeof e.className === 'string'
                                  ? e.className : '').slice(0, 60),
                          })),
                        forms: Array.from(document.querySelectorAll('form')).map(f => ({
                          id: f.id, action: (f.action||'').slice(0,100), method: f.method,
                        })),
                        clickables: Array.from(document.querySelectorAll(
                            '[role=button], .btn, [class*=submit], [class*=verify]'))
                          .map(e => ({
                            tag: e.tagName,
                            text: (e.innerText||'').trim().slice(0, 30),
                            cls: (typeof e.className === 'string'
                                  ? e.className : '').slice(0, 60),
                            visible: vis(e),
                          })).slice(0, 20),
                      };
                    }""")
                d = _Path(self.artifacts_dir)
                d.mkdir(parents=True, exist_ok=True)
                (d / f"otp_page_{code or 'nocode'}.json").write_text(
                    _json.dumps(dump, ensure_ascii=False, indent=2),
                    encoding="utf-8")
                self._d(f"验证失败，页面结构已 dump: inputs={len(dump['inputs'])} "
                        f"buttons={len(dump['buttons'])} "
                        f"forms={len(dump['forms'])}")
                for b in dump["buttons"]:
                    self._d(f"  button: {b}")
                for i in dump["inputs"]:
                    self._d(f"  input: {i}")
            except Exception as exc:
                self._d(f"dump 失败: {exc}")

            return SignupResult(False, "verify",
                                f"未确认登录态 url={url} body={body[:150]}",
                                email_addr, pwd, first, last, url,
                                captcha_attempts=cap_attempts, code=code,
                                screenshot=shot)

        except Exception as exc:
            shot = await self._shot("exception")
            return SignupResult(False, "exception",
                                f"{type(exc).__name__}: {exc}",
                                email_addr, pwd, first, last,
                                screenshot=shot)


# ---------------------------------------------------------------- 收码
def extract_code(text: str) -> str:
    """从邮件正文提取 6 位数字验证码。"""
    if not text:
        return ""
    m = CODE_RE.search(text)
    return m.group(1) if m else ""


async def wait_code_from_mailbox(mailbox, timeout: int = 180,
                                 poll: int = 4,
                                 baseline_uids: set[str] | None = None) -> str:
    """用 OutlookMailbox 等 Qoder 验证码邮件。

    ``baseline_uids``：**必须**在提交注册表单之前拍快照传入。
    否则会漏掉「提交后立刻到达」的邮件 —— 实测有邮件在开始等待前 4 秒就到了，
    内部重新拍基线会把它当旧邮件跳过，直接超时。

    其它实测要点：
    - Qoder 验证邮件的**纯文本正文为空**，验证码只在 HTML 里
    - 必须用 ``extract_code`` 解析（它能排除 CSS 颜色值）
    - 发件人 ``<no-reply@qoder.com>``，主题 ``Verify your Email with Qoder``
    """
    import time

    from app.mail.code_extract import extract_code, is_qoder_mail

    deadline = time.time() + timeout
    if baseline_uids is None:
        baseline_uids = set()
        try:
            mailbox.select("INBOX")
            baseline_uids = {u.decode() if isinstance(u, bytes) else str(u)
                             for u in mailbox.list_uids()}
        except Exception:
            pass

    baseline = set(baseline_uids)

    while time.time() < deadline:
        try:
            mailbox.select("INBOX")
            for u in mailbox.list_uids():
                key = u.decode() if isinstance(u, bytes) else str(u)
                if key in baseline:
                    continue
                msg = mailbox.fetch(u)
                if msg is None:
                    continue
                if not is_qoder_mail(msg.sender, msg.subject, msg.body_text):
                    baseline.add(key)
                    continue
                # 正文可能为空，优先解析 HTML
                code = extract_code(msg.body_html or msg.body_text)
                if not code:
                    code = extract_code(f"{msg.subject}\n{msg.body_html}")
                if code:
                    return code
                baseline.add(key)
        except Exception:
            pass
        await asyncio.sleep(poll)
    return ""
