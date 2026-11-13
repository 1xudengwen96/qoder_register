"""Qoder 注册流程 —— 纯 CDP 实现（不依赖 Playwright）。

四个步骤（2026-11-12 实测标定，2026-11-13 用 CDP 复核）
-----------------------------------------------------
① 姓名 + 邮箱 + 勾选条款 → Continue
     #basic_firstName / #basic_lastName / #basic_email / input[type=checkbox]
② 密码 → Continue
     #basic_password（autocomplete=new-password）
③ 阿里云 FeiLin 滑块（#aliyunCaptcha-img 出现时）
④ 6 位邮箱验证码 → 自动提交

第 ④ 步的历史卡点（复现证据 data/artifacts/otp_page_*.json）
-----------------------------------------------------------
填入 6 位后 DOM ``value`` 正确，但页面不前进、``buttons: []``。
原因：Ant Design 的 OTP 输入是**受控组件**，只改 DOM 不触发 React 的
onChange → 内部 state 始终不满 6 位 → 完成回调（含自动提交）不触发。

本实现的对策（按顺序尝试，全部记录到 artifacts）
1. 逐字符发真实键盘事件（keyDown + text / keyUp）
2. 原生 setter 写 value + 派发 input/change 事件（点 React）
3. 补 keydown/keypress/keyup 合成事件
4. 找可见提交按钮（Continue / Verify / Submit / Next / Sign up）点掉
5. 发 Enter
每一步后都重新读 URL / 输入框 value / 按钮集合，谁成谁留。
"""

from __future__ import annotations

import json
import logging
import random
import re
import time
from dataclasses import dataclass, field
from pathlib import Path

from app.register.cdp_page import CDPPage, Rect
from app.register.qoder_signup import (  # 复用已标定的常量与工具
    CODE_RE,
    gen_password,
    pick_name,
    SignupResult,
)

SIGNUP_URL = "https://qoder.com/users/sign-up"
HOME_URL = "https://qoder.com/"

OTP_SELECTORS = (
    "input[autocomplete='one-time-code']",
    "input[maxlength='6'][inputmode='numeric']",
    "input[maxlength='6']",
)

JS_VISIBLE_STATE = """(() => {
  const vis = (e) => !!(e.offsetWidth || e.offsetHeight || e.getClientRects().length);
  const otp = document.querySelector(
    "input[autocomplete='one-time-code'], input[maxlength='6'][inputmode='numeric'], input[maxlength='6']");
  return {
    url: location.href,
    body: (document.body ? document.body.innerText : '').slice(0, 500),
    otpValue: otp ? String(otp.value || '') : '',
    otpFocused: otp ? document.activeElement === otp : false,
    buttons: Array.from(document.querySelectorAll('button, [role=button]'))
      .filter(vis).map(e => ({
        text: (e.innerText || '').trim().slice(0, 24),
        disabled: !!e.disabled, cls: String(e.className || '').slice(0, 50),
      })),
  };
})()"""

JS_DEEP_DUMP = """(() => {
  const vis = (e) => !!(e.offsetWidth || e.offsetHeight || e.getClientRects().length);
  const desc = (e) => ({
    tag: e.tagName, id: e.id,
    cls: String(e.className || '').slice(0, 80),
    text: (e.innerText || '').trim().slice(0, 30),
    visible: vis(e), disabled: !!e.disabled,
  });
  return {
    url: location.href,
    inputs: Array.from(document.querySelectorAll('input')).map(e => ({
      id: e.id, type: e.type, maxlength: e.maxLength,
      autocomplete: e.autocomplete, value: String(e.value || ''),
      visible: vis(e), cls: String(e.className || '').slice(0, 60),
    })),
    allButtons: Array.from(document.querySelectorAll('button')).map(desc),
    clickables: Array.from(document.querySelectorAll(
        '[role=button], [class*=submit], [class*=continue], [class*=next]'))
      .map(desc).slice(0, 30),
    forms: Array.from(document.querySelectorAll('form')).map(f => ({
      id: f.id, action: f.action || '', method: f.method,
      inputs: f.querySelectorAll('input').length,
    })),
    bodyText: (document.body ? document.body.innerText : '').slice(0, 600),
  };
})()"""


# ============================================================ 收码（同步版）
def wait_code_sync(mailbox, timeout: int = 180, poll: int = 4,
                   baseline_uids: set[str] | None = None,
                   folders: tuple[str, ...] = ("INBOX", "Junk")) -> str:
    """同步等 Qoder 验证码邮件。

    **必须扫 Junk**：实测 Qoder 的验证邮件会被 Outlook 判成垃圾，
    INBOX 里干干净净，UI 却已经显示 "Enter the code sent to ..."。
    只扫 INBOX 会一直等到超时（踩过）。
    """
    from app.mail.code_extract import extract_code, is_qoder_mail

    deadline = time.time() + timeout
    baseline = set(baseline_uids or set())
    junk_rescan = 0
    while time.time() < deadline:
        for folder in folders:
            try:
                mailbox.select(folder)
            except Exception:
                continue
            is_inbox = folder.upper().startswith("INBOX")
            try:
                uids = mailbox.list_uids()
            except Exception:
                continue
            # Junk 里没有基线，只看最近 30 封，避免老邮件干扰
            scan = uids if is_inbox else uids[-30:]
            for u in scan:
                key = u.decode() if isinstance(u, bytes) else str(u)
                if is_inbox and key in baseline:
                    continue
                try:
                    msg = mailbox.fetch(u)
                except Exception:
                    continue
                if msg is None:
                    continue
                if not is_qoder_mail(msg.sender, msg.subject, msg.body_text):
                    if is_inbox:
                        baseline.add(key)
                    continue
                code = extract_code(msg.body_html or msg.body_text)
                if not code:
                    code = extract_code(f"{msg.subject}\n{msg.body_html}")
                if code:
                    return code
                if is_inbox:
                    baseline.add(key)
            if not is_inbox:
                junk_rescan += 1
        time.sleep(poll)
    return ""


@dataclass(slots=True)
class Stage:
    name: str
    ok: bool
    detail: str = ""
    elapsed: float = 0.0


@dataclass(slots=True)
class FlowReport:
    ok: bool
    result: SignupResult | None = None
    stages: list[Stage] = field(default_factory=list)
    artifacts: list[str] = field(default_factory=list)
    otp_debug: dict = field(default_factory=dict)


class QoderSignupCDP:
    """用 CDPPage 驱动 Qoder 注册页。"""

    def __init__(self, page: CDPPage, artifacts_dir: str = "data/artifacts",
                 human: bool = True, logger: logging.Logger | None = None,
                 paid_client=None, prefer_paid: bool = False,
                 proxy: str | None = None, captcha_max_attempts: int = 6) -> None:
        self.page = page
        self.artifacts = Path(artifacts_dir)
        self.artifacts.mkdir(parents=True, exist_ok=True)
        self.human = human
        self.log = logger or logging.getLogger("signup-cdp")
        self.paid = paid_client
        self.prefer_paid = prefer_paid
        self.proxy = proxy
        self.captcha_max_attempts = captcha_max_attempts
        self.report = FlowReport(ok=False)
        self._t0 = time.time()

    # ---------------------------------------------------------- 基础
    def _d(self, msg: str) -> None:
        self.log.info("[signup-cdp] %s", msg)

    def _sleep(self, lo: float, hi: float) -> None:
        time.sleep(random.uniform(lo, hi))

    def _stage(self, name: str, ok: bool, detail: str = "") -> None:
        self.report.stages.append(
            Stage(name, ok, detail, round(time.time() - self._t0, 1)))
        self._d(f"{'PASS' if ok else 'FAIL'} {name}: {detail}")

    def _shot(self, tag: str) -> str:
        p = self.artifacts / f"cdp_signup_{tag}_{int(time.time())}.png"
        try:
            self.page.shot(str(p))
            self.report.artifacts.append(str(p))
            return str(p)
        except Exception:
            return ""

    def _dump(self, tag: str, js: str) -> dict:
        try:
            d = self.page.eval(js) or {}
        except Exception as exc:
            d = {"error": f"{type(exc).__name__}: {exc}"}
        p = self.artifacts / f"cdp_{tag}_{int(time.time())}.json"
        try:
            p.write_text(json.dumps(d, ensure_ascii=False, indent=2),
                         encoding="utf-8")
            self.report.artifacts.append(str(p))
        except Exception:
            pass
        return d

    # ---------------------------------------------------------- 按钮
    def _click_submit(self, names=("Continue", "Verify", "Submit", "Next",
                                   "Sign up", "Confirm")) -> str:
        """点可见的提交按钮，返回点到了哪个文案。"""
        for want in names:
            js = """(() => {
              const want = %s;
              const vis = (e) => !!(e.offsetWidth || e.offsetHeight ||
                                    e.getClientRects().length);
              const nodes = Array.from(document.querySelectorAll(
                  'button, [role=button]')).filter(vis).filter(e => !e.disabled);
              const hit = nodes.find(e =>
                  (e.innerText || '').trim().toLowerCase() === want);
              if (!hit) return null;
              hit.scrollIntoView({block: 'center'});
              const r = hit.getBoundingClientRect();
              return {x: r.x + r.width / 2, y: r.y + r.height / 2};
            })()""" % json.dumps(want.lower())
            got = self.page.eval(js)
            if got:
                self.page.click_xy(float(got["x"]), float(got["y"]),
                                   human=self.human)
                return want
        # 退一步：全局第一个可见 submit 按钮
        js_any = """(() => {
          const vis = (e) => !!(e.offsetWidth || e.offsetHeight ||
                                e.getClientRects().length);
          const hit = Array.from(document.querySelectorAll('button[type=submit]'))
              .filter(vis).filter(e => !e.disabled)[0];
          if (!hit) return null;
          const r = hit.getBoundingClientRect();
          return {x: r.x + r.width / 2, y: r.y + r.height / 2};
        })()"""
        got = self.page.eval(js_any)
        if got:
            self.page.click_xy(float(got["x"]), float(got["y"]), human=self.human)
            return "button[type=submit]"
        return ""

    # ---------------------------------------------------------- ①
    def step_identity(self, email: str, first: str, last: str) -> bool:
        self.page.goto(HOME_URL, wait_ms=random.randint(1800, 3200))
        self.page.move(random.randint(200, 700), random.randint(150, 500))
        self.page.goto(SIGNUP_URL, wait_ms=random.randint(1500, 2600))

        if not self.page.sel_ready("#basic_firstName", timeout=30):
            self._stage("① 注册页", False, f"没有姓名输入框 url={self.page.url()}")
            self._shot("no_form")
            return False

        ok = True
        ok &= self.page.fill("#basic_firstName", first, human=self.human)
        self._sleep(0.2, 0.5)
        ok &= self.page.fill("#basic_lastName", last, human=self.human)
        self._sleep(0.2, 0.5)
        ok &= self.page.fill("#basic_email", email, human=self.human)

        checked = self.page.check("input[type=checkbox]")
        self._sleep(0.4, 0.9)
        if not checked:
            self._stage("① 勾选条款", False, "checkbox 没勾上")
            ok = False

        clicked = self._click_submit()
        self._sleep(0.5, 1.0)
        if not clicked:
            self._stage("① 提交", False, "点不到 Continue")
            self._shot("no_continue")
            return False

        self._stage("① 姓名邮箱", ok, f"已提交（{clicked}）")
        return ok

    # ---------------------------------------------------------- ②
    def step_password(self, password: str) -> bool:
        if not self.page.sel_ready("#basic_password", timeout=20, visible=True):
            self._stage("② 密码", True, "未出现密码步骤（流程可能变了）")
            return True
        if not self.page.fill("#basic_password", password, human=self.human):
            self._stage("② 密码", False, "填密码失败")
            return False
        self._sleep(0.5, 1.0)
        clicked = self._click_submit()
        if not clicked:
            self._stage("② 密码", False, "点不到 Continue")
            return False
        self._sleep(2.0, 3.0)
        self._stage("② 密码", True, f"已提交（{clicked}）")
        return True

    # ---------------------------------------------------------- ③
    def step_captcha(self) -> bool:
        from app.register.aliyun_slider_cdp import AliyunSliderCDP

        appears = self.page.wait_js(
            "(() => !!document.querySelector('#aliyunCaptcha-img') || "
            "!!document.querySelector('#captcha-element'))()", timeout=12)
        if not appears:
            self._stage("③ 滑块", True, "本步未出现滑块")
            return True

        solver = AliyunSliderCDP(self.page, human=self.human, logger=self.log,
                                 paid_client=self.paid,
                                 prefer_paid=self.prefer_paid,
                                 proxy=self.proxy,
                                 local_fallback=not self.prefer_paid)
        res = solver.solve(max_attempts=self.captcha_max_attempts)
        if not res.ok:
            self._shot("captcha_fail")
            self._stage("③ 滑块", False,
                        f"{res.detail} target={res.target_x:.1f} "
                        f"final={res.final_x:.1f} 次数={res.attempts}")
            return False
        self._sleep(1.5, 2.5)
        self._stage("③ 滑块", True, f"{res.detail} 次数={res.attempts} "
                                   f"method={res.method}")
        return True

    # ---------------------------------------------------------- ④
    def _otp_selector(self) -> str:
        for sel in OTP_SELECTORS:
            if self.page.exists(sel):
                return sel
        return ""

    def fill_otp(self, code: str) -> bool:
        """把 6 位验证码打进 OTP 框，并确保 React 感知到。"""
        digits = [c for c in code if c.isdigit()][:6]
        if len(digits) < 6:
            self._d(f"验证码不完整: {code}")
            return False
        sel = self._otp_selector()
        if not sel:
            self._d("找不到 OTP 输入框")
            return False
        s = json.dumps(sel)

        # 1) 聚焦 + 清空
        self.page.focus(sel) if False else self.page.click(sel, human=self.human)
        self._sleep(0.2, 0.4)
        self.page.clear(sel)
        self._sleep(0.15, 0.3)

        # 2) 逐字符真实键盘事件
        for ch in digits:
            self.page.type_text(ch, delay=(0.05, 0.12))
            time.sleep(random.uniform(0.05, 0.14))
        self._sleep(0.5, 0.9)
        val = self.page.value(sel)
        self._d(f"键盘输入后 value={val}")

        # 3) 原生 setter + input/change（点 React 受控 state）
        if val != "".join(digits):
            self.page.eval("""(() => {
              const e = document.querySelector(%s);
              if (!e) return false;
              const setter = Object.getOwnPropertyDescriptor(
                  window.HTMLInputElement.prototype, 'value').set;
              setter.call(e, %s);
              e.dispatchEvent(new Event('input', {bubbles: true}));
              e.dispatchEvent(new Event('change', {bubbles: true}));
              return true;
            })()""" % (s, json.dumps("".join(digits))))
            self._sleep(0.3, 0.6)
            self._d(f"setter 后 value={self.page.value(sel)}")

        # 4) 补一轮键盘合成事件（部分实现监听 keydown）
        self.page.eval("""(() => {
          const e = document.querySelector(%s);
          if (!e) return false;
          e.focus();
          for (const t of ['keydown', 'keypress', 'keyup']) {
            e.dispatchEvent(new KeyboardEvent(t, {
              key: 'Enter', code: 'Enter', keyCode: 13, which: 13,
              bubbles: true, cancelable: true,
            }));
          }
          return true;
        })()""" % s)
        return True

    def step_otp(self, code_provider) -> bool:
        otp_wait = self.page.wait_js(
            "(() => !!document.querySelector("
            "\"input[autocomplete='one-time-code'], input[maxlength='6']\"))()",
            timeout=40)
        if not otp_wait:
            body = self.page.text()[:200].replace("\n", " | ")
            self._shot("no_otp_input")
            self._stage("④ OTP 输入框", False, f"没出现。页面: {body}")
            return False

        self._stage("④ OTP 输入框", True, "已出现")
        self._d("等邮箱验证码……")
        code = code_provider()
        if not code:
            self._shot("no_code_mail")
            self._stage("④ 收码", False, "未取到验证码")
            return False
        self._stage("④ 收码", True, f"code={code}")

        self.fill_otp(code)
        state = self.page.eval(JS_VISIBLE_STATE) or {}
        self._d(f"填码后 url={state.get('url')} buttons={state.get('buttons')}")

        # 尝试提交：先找按钮，再回车
        clicked = self._click_submit()
        if not clicked:
            self.page.key(("Enter"))
            clicked = "Enter"
        self._d(f"提交方式: {clicked}")
        self._sleep(3.0, 4.5)

        state = self.page.eval(JS_VISIBLE_STATE) or {}
        url = str(state.get("url") or self.page.url())
        body = str(state.get("body") or "")
        self._d(f"提交后 url={url}")

        if "/sign-up" in url:
            dbg = self._dump("otp_stuck", JS_DEEP_DUMP)
            self.report.otp_debug = dbg
            self._shot("otp_stuck")
            self._stage("④ 提交", False,
                        f"停在本页，otpValue={state.get('otpValue')} "
                        f"buttons={[b.get('text') for b in state.get('buttons') or []]}")
            return False

        self._stage("④ 提交", True, f"已离开注册页 -> {url}")
        low = body.lower()
        ok_markers = ("welcome", "dashboard", "get started", "workspace",
                      "sign out", "logout", "qoder ide")
        if any(m in low for m in ok_markers):
            self._stage("⑤ 登录态", True, "页面出现登录后标志")
        else:
            self._stage("⑤ 登录态", True,
                        f"url 已跳转（正文 {len(body)} 字）")
        return True

    # ---------------------------------------------------------- 主流程
    def run(self, email: str, password: str | None = None,
            code_provider=None, stop_before: str = "") -> FlowReport:
        pwd = password or gen_password()
        first, last = pick_name(email)
        self.page.cdp.call("Page.enable")
        self._d(f"目标邮箱 {email} / {first} {last}")

        res = SignupResult(False, "init", "", email, pwd, first, last)
        try:
            if stop_before == "captcha" and not self.step_identity(email, first, last):
                res.stage = "step1"
                res.detail = "第①步失败"
                self.report.result = res
                return self.report
            if stop_before == "captcha":
                self.step_password(pwd)
                self.step_captcha()
                self._shot("stop_at_captcha")
                self.report.ok = True
                res.ok, res.stage, res.detail = True, "captcha-stop", "已在滑块前停下"
                self.report.result = res
                return self.report

            if not self.step_identity(email, first, last):
                res.stage, res.detail = "step1", "第①步失败"
                return self._finish(res)
            self._sleep(2.0, 3.0)
            if not self.step_password(pwd):
                res.stage, res.detail = "step2", "第②步失败"
                return self._finish(res)
            self._sleep(2.0, 3.5)
            if not self.step_captcha():
                res.stage, res.detail = "step3", "第③步滑块未通过"
                return self._finish(res)

            if code_provider is None:
                res.stage, res.detail = "step4", "没有收码回调"
                return self._finish(res)
            if not self.step_otp(code_provider):
                res.stage, res.detail = "step4", "第④步 OTP 未通过"
                return self._finish(res)

            res.ok, res.stage, res.detail = True, "done", "注册完成"
            res.url = self.page.url()
            self._shot("final")
            return self._finish(res)
        except Exception as exc:
            self._shot("exception")
            res.stage = "exception"
            res.detail = f"{type(exc).__name__}: {exc}"
            return self._finish(res)

    def _finish(self, res: SignupResult) -> FlowReport:
        self.report.result = res
        self.report.ok = res.ok
        self._shot("final" if res.ok else "failed")
        return self.report
