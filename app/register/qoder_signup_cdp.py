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
def wait_code_sync(mailbox, timeout: int = 180, poll: int = 3,
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
                if key in baseline:
                    continue
                try:
                    msg = mailbox.fetch(u)
                except Exception:
                    continue
                if msg is None:
                    continue
                if not is_qoder_mail(msg.sender, msg.subject, msg.body_text):
                    # Junk 也记基线：旧版只记 INBOX，Junk 老信每轮重 fetch
                    # （最多 30 次 FETCH 往返），邮箱越大收码越慢。
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
                 proxy: str | None = None, captcha_max_attempts: int = 6,
                 manual_captcha: bool = False,
                 manual_timeout: int = 600,
                 manual_otp: bool = False,
                 manual_form: bool = False,
                form_nav: bool = False) -> None:
        self.page = page
        self.artifacts = Path(artifacts_dir)
        self.artifacts.mkdir(parents=True, exist_ok=True)
        self.human = human
        self.log = logger or logging.getLogger("signup-cdp")
        self.paid = paid_client
        self.prefer_paid = prefer_paid
        self.proxy = proxy
        self.captcha_max_attempts = captcha_max_attempts
        # 人工滑块：滑块出现后暂停等用户手滑，通过后自动接管。
        # 与自动化识别互斥（manual_captcha=True 时③步完全不碰鼠标）。
        self.manual_captcha = manual_captcha
        self.manual_timeout = manual_timeout
        # 人工填码实验模式：收码后输出验证码并停手，由用户手动
        # 在页面输入，用于二分定位二次验证触发点是否在填码环节
        self.manual_otp = manual_otp
        # 人工填表实验模式：①②+滑块全由用户手动，程序只收码填码，
        # 用于二分定位触发点在前序填表链路还是 OTP 环节
        self.manual_form = manual_form
        # 仅导航实验：manual_form 基础上程序只负责导航到注册页，
        # 其余全手动——区分触发点是 Page.navigate 还是 OS 输入
        self.form_nav = form_nav
        # 接管通知文件：用户完成后写入，程序看到后恢复（消费即删）。
        # artifacts 默认 data/artifacts，通知文件落在 data/ 下。
        self._go_file = self.artifacts.parent / ".manual_go"
        self.report = FlowReport(ok=False)
        self._baxia_seen = False   # 本会话是否被 Baxia 惩罚（决定是否同会话重试）
        self._t0 = time.time()
        # 并发 worker 同时跑时，秒级时间戳会撞名互盖截图/存档，
        # 每个流程实例带一段随机后缀保证产物互不干扰。
        self._uid = f"{int(self._t0)}_{random.randrange(1 << 24):06x}"

    # ---------------------------------------------------------- 基础
    def _d(self, msg: str) -> None:
        self.log.info("[signup-cdp] %s", msg)

    def _sleep(self, lo: float, hi: float) -> None:
        time.sleep(random.uniform(lo, hi))

    def _idle_moves(self, duration: float) -> None:
        """等待期间模拟真人手部微动：小幅鼠标漂移 + 偶尔滚轮。

        人工接管模式下不动作——用户可能正在窗口里手动操作，
        程序动鼠标就是「跟用户抢」，改为纯等待。
        """
        if self.manual_captcha:
            time.sleep(duration)
            return
        end = time.time() + duration
        x, y = random.uniform(300, 900), random.uniform(200, 600)
        while time.time() < end:
            nx = min(max(x + random.uniform(-60, 60), 20), 1200)
            ny = min(max(y + random.uniform(-40, 40), 20), 800)
            # 带轨迹的漂移：旧版直接 move 到新坐标是瞬移，
            # 行为遥测里「鼠标从不经过中间点」是机器特征
            steps = random.randint(8, 16)
            for i in range(1, steps + 1):
                t = i / steps
                try:
                    self.move(x + (nx - x) * t + random.uniform(-0.8, 0.8),
                              y + (ny - y) * t + random.uniform(-0.8, 0.8))
                except Exception:  # noqa: BLE001
                    pass
                time.sleep(random.uniform(0.012, 0.03))
            x, y = nx, ny
            if random.random() < 0.1:
                try:
                    self.page.cdp.call("Input.dispatchMouseEvent", {
                        "type": "mouseWheel", "x": x, "y": y,
                        "deltaX": 0, "deltaY": random.choice([-120, 120])})
                except Exception:  # noqa: BLE001
                    pass
            time.sleep(random.uniform(0.6, 2.2))

    def _otp_wait_idle(self, duration: float) -> None:
        """OTP 等码期微动：与 _idle_moves 不同，人工模式下也动——
        此时用户不在操作窗口，静止几十秒反而是破绽。"""
        end = time.time() + duration
        x, y = random.uniform(300, 900), random.uniform(200, 600)
        while time.time() < end:
            nx = min(max(x + random.uniform(-60, 60), 20), 1200)
            ny = min(max(y + random.uniform(-40, 40), 20), 800)
            steps = random.randint(8, 16)
            for i in range(1, steps + 1):
                t = i / steps
                try:
                    self.page.move(x + (nx - x) * t + random.uniform(-0.8, 0.8),
                                   y + (ny - y) * t + random.uniform(-0.8, 0.8))
                except Exception:  # noqa: BLE001
                    pass
                time.sleep(random.uniform(0.012, 0.03))
            x, y = nx, ny
            time.sleep(random.uniform(0.6, 2.2))

    def _stage(self, name: str, ok: bool, detail: str = "") -> None:
        self.report.stages.append(
            Stage(name, ok, detail, round(time.time() - self._t0, 1)))
        self._d(f"{'PASS' if ok else 'FAIL'} {name}: {detail}")

    def _shot(self, tag: str) -> str:
        p = self.artifacts / f"cdp_signup_{tag}_{self._uid}.png"
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
        p = self.artifacts / f"cdp_{tag}_{self._uid}.json"
        try:
            p.write_text(json.dumps(d, ensure_ascii=False, indent=2),
                         encoding="utf-8")
            self.report.artifacts.append(str(p))
        except Exception:
            pass
        return d

    # ---------------------------------------------------------- 按钮
    def _click_submit(self, names=("Continue", "Verify", "Submit", "Next",
                                   "Sign up", "Confirm",
                                   "继续", "下一步", "提交", "注册",
                                   "确认")) -> str:
        """点可见的提交按钮，返回点到了哪个文案。

        中英文都认（acct22 实测：中文页面按钮是「继续」，只匹配英文会漏点）。
        """
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
        # 2026-11-19 用户要求提速：首页停留浏览/滚轮等戏剧化等待移除，
        # 直接进注册页（根因已定案为邮箱风险，与导航节奏无关）
        self.page.goto(SIGNUP_URL, wait_ms=random.randint(1500, 2600))

        # 已登录态兑底：复用 env 跨账号时服务端会直接跳到 /account/profile
        # 找不到注册表单。先点 Log out 再回 sign-up。
        # 注意：2 秒没等到表单 ≠ 已登录——SPA 渲染慢时 URL 仍停在
        # /users/sign-up，误判会凭空发一次 sign_out、清 cookie、多两次
        # 导航。只有 URL 真跳离注册页才判定已登录。
        if not self.page.sel_ready("#basic_firstName", timeout=2):
            cur = self.page.url()
            if "/sign-up" not in cur:
                self._stage("① 检测已登录", True, f"先登出 url={cur[:80]}")
                # 调客户端的退出（POST 带 CSRF token；Rails 会校验）
                self.page.eval(
                    "(async()=>{try{const m=document.querySelector("
                    "'meta[name=csrf-token]');"
                    "await fetch('/users/sign_out',{method:'POST',"
                    "credentials:'include',headers:{'X-CSRF-Token':"
                    "(m?m.content:''),'X-Requested-With':'XMLHttpRequest'}})}"
                    "catch(e){}})()",
                    await_promise=True)
                self.page.goto(SIGNUP_URL, wait_ms=random.randint(1500, 2600))
                # 登出没生效（会话 cookie 还在，服务端仍重定向回 /account）
                # → CDP 清掉全部 cookie 强制回到未登录态
                if not self.page.sel_ready("#basic_firstName", timeout=3):
                    self._d("登出未生效，CDP 清 cookie 强制登出")
                    try:
                        self.page.cdp.call("Network.enable")
                    except Exception:  # noqa: BLE001
                        pass
                    try:
                        self.page.cdp.call("Network.clearBrowserCookies")
                    except Exception:  # noqa: BLE001
                        pass
                    self.page.goto(SIGNUP_URL,
                                   wait_ms=random.randint(1800, 3000))

        if not self.page.sel_ready("#basic_firstName", timeout=30):
            self._stage("① 注册页", False, f"没有姓名输入框 url={self.page.url()}")
            self._shot("no_form")
            return False

        ok = True
        r1 = self.page.fill("#basic_firstName", first, human=self.human)
        self._d(f"firstName 填写={'OK' if r1 else 'FAIL'}")
        ok &= r1
        self._sleep(0.2, 0.5)
        r2 = self.page.fill("#basic_lastName", last, human=self.human)
        self._d(f"lastName 填写={'OK' if r2 else 'FAIL'}")
        ok &= r2
        self._sleep(0.2, 0.5)
        r3 = self.page.fill("#basic_email", email, human=self.human)
        self._d(f"email 填写={'OK' if r3 else 'FAIL'}")
        ok &= r3
        self._sleep(0.2, 0.5)

        checked = self.page.check("input[type=checkbox]")
        self._d(f"条款勾选={'OK' if checked else 'FAIL'}")
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
    def _wait_manual_gate(self) -> bool:
        """接管闸门：阻塞等用户通知（data/.manual_go 出现，消费即删）。

        唯一恢复条件是通知文件——不以页面状态自行恢复：
        用户没说继续，滑块过了、九宫格弹了程序也绝不动作。
        等待期间只观察记录页面状态，不碰鼠标键盘。
        """
        from app.register.aliyun_slider_cdp import JS_SOLVED

        t0 = time.time()
        last_note = t0
        while time.time() - t0 < self.manual_timeout:
            if self._go_file.exists():
                try:
                    self._go_file.unlink()
                except OSError:
                    pass
                self._d(f"【人工模式】收到接管通知（等了 {time.time() - t0:.0f}s），继续")
                return True
            if time.time() - last_note >= 20:
                last_note = time.time()
                if self.manual_form:
                    # 人工填表实验：等待期零求值，不污染对照条件
                    self._d(f"【人工模式】等待通知中… 已等 {time.time() - t0:.0f}s "
                            f"（人工填表实验：零求值等待）。"
                            f"通知方式：python -m app.cli manual:go")
                    time.sleep(1.0)
                    continue
                try:
                    st = self.page.eval(JS_SOLVED) or {}
                except Exception:  # noqa: BLE001
                    st = {}
                self._d(f"【人工模式】等待通知中… 已等 {time.time() - t0:.0f}s "
                        f"（页面状态 {st.get('why')}，仅供观察不动作）。"
                        f"通知方式：python -m app.cli manual:go")
            time.sleep(1.0)
        self._d(f"【人工模式】等待通知超时（{self.manual_timeout}s）")
        return False

    def _wait_manual_captcha(self) -> bool:
        """人工滑块：滑块出现后程序彻底停手，等用户通知再继续。

        与旧版「检测到滑块通过即自动接管」不同：用户要求
        「没通知之前不要跟我抢」——滑块及随后可能弹的九宫格
        都由用户手动处理，程序收到 go 通知后才接管后续步骤。
        """
        # 清掉残留的 go 信号，防止上一轮误当作本轮通知
        try:
            if self._go_file.exists():
                self._go_file.unlink()
        except OSError:
            pass
        self._d("【人工模式】请手动完成滑块（若随后弹九宫格也请一并手动完成），"
                "全部通过后通知程序继续：python -m app.cli manual:go")
        ok = self._wait_manual_gate()
        if ok:
            self._stage("③ 滑块", True, "人工接管完成")
        else:
            self._shot("manual_timeout")
            self._stage("③ 滑块", False,
                        f"人工接管等待超时（{self.manual_timeout}s）")
        return ok

    def step_captcha(self) -> bool:
        from app.register.aliyun_slider_cdp import AliyunSliderCDP

        appears = self.page.wait_js(
            "(() => !!document.querySelector('#aliyunCaptcha-img') || "
            "!!document.querySelector('#captcha-element'))()", timeout=12)
        if not appears:
            self._stage("③ 滑块", True, "本步未出现滑块")
            return True

        if self.manual_captcha:
            return self._wait_manual_captcha()

        solver = AliyunSliderCDP(self.page, human=self.human, logger=self.log,
                                 paid_client=self.paid,
                                 prefer_paid=self.prefer_paid,
                                 proxy=self.proxy)
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
    JS_OTP_FILL = """(() => {
  const e = document.querySelector(__SEL__);
  if (!e) return 'no-input';
  const code = __CODE__;
  e.focus();
  const setter = Object.getOwnPropertyDescriptor(
      window.HTMLInputElement.prototype, 'value').set;

  // 1) 原生 setter + 带 inputType/data 的 InputEvent
  setter.call(e, '');
  e.dispatchEvent(new InputEvent('input', {bubbles: true,
      inputType: 'deleteContentBackward'}));
  setter.call(e, code);
  e.dispatchEvent(new InputEvent('input', {bubbles: true, cancelable: true,
      inputType: 'insertText', data: code}));
  e.dispatchEvent(new Event('change', {bubbles: true}));

  // 2) 直接调 React 的 onChange —— 受控组件只有这条必定生效
  const rkey = Object.keys(e).find(k => k.startsWith('__reactProps$'));
  let fired = false, err = '';
  if (rkey) {
    const props = e[rkey];
    if (props && typeof props.onChange === 'function') {
      try {
        props.onChange({target: e, currentTarget: e, bubbles: true,
                        nativeEvent: new Event('change')});
        fired = true;
      } catch (ex) { err = String(ex).slice(0, 80); }
    }
  }
  return JSON.stringify({value: String(e.value || ''), fired: fired,
                         err: err, reactKey: !!rkey});
})()"""

    def _net_log(self, limit: int = 40) -> list[dict]:
        """把 CDP 收到的网络事件整理成简短列表（诊断提交有没有真的发出去）。"""
        out: list[dict] = []
        for ev in list(self.page.cdp.events):
            method = ev.get("method")
            params = ev.get("params") or {}
            if method == "Network.requestWillBeSent":
                req = params.get("request") or {}
                url = str(req.get("url") or "")
                if any(k in url for k in ("/api", "sign", "verify", "otp",
                                          "captcha", "user")):
                    out.append({"t": "req", "m": req.get("method"),
                                "url": url[:140]})
            elif method == "Network.responseReceived":
                resp = params.get("response") or {}
                url = str(resp.get("url") or "")
                if any(k in url for k in ("/api", "sign", "verify", "otp",
                                          "captcha", "user")):
                    out.append({"t": "resp", "s": resp.get("status"),
                                "url": url[:140]})
        return out[-limit:]

    # 强制触发 AntD OTP 的完成回调：直接以「字符串」调 onChange
    JS_OTP_FORCE = r"""(() => {
  const e = document.querySelector(
    "input[autocomplete='one-time-code'], input[maxlength='6']");
  if (!e) return 'no-input';
  const code = e.value || '';
  const out = {code, fired: [], err: ''};
  const call = (obj, name, arg) => {
    try {
      if (obj && typeof obj[name] === 'function') {
        obj[name](arg);
        out.fired.push(name);
      }
    } catch (ex) { out.err += name + ':' + String(ex).slice(0, 40) + ';'; }
  };
  const keyOf = (el) => el && Object.keys(el).find(k => k.startsWith('__reactProps$'));
  const props = e[keyOf(e)];
  // AntD Input.OTP 的 onChange 形参是字符串
  call(props, 'onChange', code);
  call(props, 'onInput', {target: e, currentTarget: e});
  const pk = keyOf(e.parentElement);
  if (pk) {
    const pp = e.parentElement[pk];
    call(pp, 'onChange', code);
  }
  // 再补一次原生事件链
  try {
    const setter = Object.getOwnPropertyDescriptor(
      window.HTMLInputElement.prototype, 'value').set;
    setter.call(e, code);
    e.dispatchEvent(new InputEvent('input', {bubbles: true,
        inputType: 'insertText', data: code}));
    e.dispatchEvent(new Event('change', {bubbles: true}));
    out.fired.push('native');
  } catch (ex) { out.err += 'native:' + String(ex).slice(0, 40); }
  return JSON.stringify(out);
})()"""

    def _otp_selector(self) -> str:
        for sel in OTP_SELECTORS:
            if self.page.exists(sel):
                return sel
        return ""


    def _js_otp_fill(self, selector_json: str, code_json: str) -> str:
        """安全渲染 JS_OTP_FILL（不要用 % 格式化：模板里有字面 % 会炸）。"""
        return (self.JS_OTP_FILL
                .replace("__SEL__", selector_json)
                .replace("__CODE__", code_json))

    def fill_otp(self, code: str) -> str:
        """把 6 位码灌进 OTP 框：真键盘 → 原生 setter → 直接调 React onChange。

        返回最终使用的选择器；空串表示没找到框。
        """
        digits = [c for c in code if c.isdigit()][:6]
        if len(digits) < 6:
            self._d(f"验证码不完整: {code}")
            return ""
        sel = self._otp_selector()
        if not sel:
            self._d("找不到 OTP 输入框")
            return ""
        target = "".join(digits)
        s = json.dumps(sel)

        # 1) 真键盘逐字符（先让组件进入可输入状态；提速版节奏）
        self.page.click(sel, human=self.human)
        self._sleep(0.15, 0.3)
        self.page.clear(sel)
        self._sleep(0.1, 0.2)
        for ch in digits:
            self.page.type_text(ch, delay=(0.06, 0.15))
            time.sleep(random.uniform(0.04, 0.1))
        self._sleep(0.3, 0.5)
        try:
            typed = self.page.value(sel) or ""
        except Exception:  # noqa: BLE001
            typed = ""
        self._d(f"键盘输入后 value={typed}")

        # 2) 原生 setter + InputEvent + React onChange。
        #    键盘已把 6 位敲全时跳过——受控组件的 onChange 已随真实按键
        #    触发，再灌一次会重复触发完成回调（多一次验证 POST）。
        #    键盘没敲全（组件吃了部分键）才用注入兑底。
        if typed == target:
            self._d("键盘输入已生效，跳过注入")
        else:
            try:
                out = self.page.eval(self._js_otp_fill(s, json.dumps(target)))
                self._d(f"React 注入: {out}")
            except Exception as exc:  # noqa: BLE001
                self._d(f"React 注入异常: {type(exc).__name__}: {exc}")
        self._sleep(0.3, 0.6)
        return sel

    def _otp_progressed(self, before_url: str, before_state: dict) -> tuple[bool, dict]:
        """判断页面是否已经往前走（离开注册页，或冒出新的按钮/报错）。"""
        st = self.page.eval(JS_VISIBLE_STATE) or {}
        url = str(st.get("url") or "")
        if url and url != before_url and "/sign-up" not in url:
            return True, st
        old = {b.get("text") for b in (before_state.get("buttons") or [])}
        new = {b.get("text") for b in (st.get("buttons") or [])}
        if new - old:
            return True, st
        return False, st

    def _dump_html(self, tag: str) -> str:
        """把整页 HTML 存下来，事后能看见 React 到底渲染了什么。"""
        try:
            html = self.page.eval(
                "document.body ? document.body.innerHTML.slice(0, 20000) : ''") or ""
        except Exception:  # noqa: BLE001
            html = ""
        path = self.artifacts / f"cdp_{tag}_{self._uid}.html"
        path.write_text(html, encoding="utf-8", errors="ignore")
        self.report.artifacts.append(str(path))
        return str(path)

    def _goback_recover(self) -> bool:
        """「假通过死页」兑底：组件显示 Verified 但表单不推进（无 button/
        form，只剩 "Go back"）。点 Go back 恢复注册表单。

        根因：滑块判定延迟 + 过早 refresh 打断 success 回调（见
        aliyun_slider_cdp.solve 的延迟判定容忍）。2026-11-17 acct21/22 实测：
        点 Go back（SPAN）后 Email/Password/Continue 表单立即回来。
        中文页面文案是「返回」（acct22），英文是 "Go back"，两者都匹配。
        """
        got = self.page.eval(r"""(() => {
          const vis = e => !!(e.offsetWidth || e.offsetHeight ||
                                e.getClientRects().length);
          const btns = Array.from(document.querySelectorAll(
              'button, [role=button]')).filter(vis);
          if (btns.length) return {error: 'has-buttons'};
          const txt = (document.body ? document.body.innerText : '') || '';
          if (!/go\s*back/i.test(txt) && !txt.includes('返回')) {
            return {error: 'no-goback'};
          }
          const want = e => {
            const t = (e.innerText || '').trim();
            return /^go\s*back$/i.test(t) || t === '返回';
          };
          for (const e of document.querySelectorAll('*')) {
            if (!vis(e) || !want(e)) continue;
            if (e.children.length) continue;      // 要叶子节点
            const r = e.getBoundingClientRect();
            if (r.width < 2) continue;
            return {x: r.x + r.width / 2, y: r.y + r.height / 2};
          }
          return {error: 'goback-not-found'};
        })()""")
        if not got or got.get("error"):
            return False
        self._d(f"假通过死页：点 Go back 恢复表单 {got}")
        self.page.click_xy(float(got["x"]), float(got["y"]), human=self.human)
        # 实测（acct21）：回到的是 Email/Password（②步）表单；也可能是①步全表单
        return self.page.wait_js(
            "(() => !!document.querySelector('#basic_password') || "
            "!!document.querySelector('#basic_firstName'))()", timeout=10)

    def _baxia_present(self) -> bool:
        from app.register.baxia_cdp import BaxiaClickCaptcha
        try:
            if BaxiaClickCaptcha(self.page, logger=self.log).present():
                self._baxia_seen = True
                return True
        except Exception:  # noqa: BLE001
            pass
        return False

    def _baxia_manual_gate(self, code: str, before_url: str,
                           before: dict, ladder: list) -> tuple[bool, dict]:
        """人工模式：检测到 Baxia 不打码不抢点，等用户手动完成并通知。

        与自动模式的 _baxia_solve_resubmit 互斥：人工模式下程序
        完全不碰页面，用户解完格运行 manual:go 后，页面没自动重提
        时才补一次填码触发验证 POST。
        """
        if not self._baxia_present():
            return False, before
        self._baxia_seen = True
        ladder.append("baxia-manual")
        self._d("【人工模式】检测到九宫格，程序停手；请手动完成后运行 "
                "python -m app.cli manual:go 通知接管")
        if not self._wait_manual_gate():
            return False, before
        self._sleep(1.0, 2.0)
        moved, state = self._otp_progressed(before_url, before)
        if moved:
            ladder.append("baxia-manual-ok")
            return True, state
        # 人工解格后页面不会自动重提（同自动模式观察），补一次填码
        self._d("人工解格后页面未推进，重灌一次验证码触发提交")
        self.fill_otp(code)
        for _ in range(3):
            self._sleep(1.5, 2.5)
            moved, state = self._otp_progressed(before_url, before)
            if moved:
                return True, state
        return False, state

    def _secondary_slider_present(self) -> bool:
        """二次验证是否为阿里云滑块（优先于九宫格判定，避免滑块被
        present() 的宽泛 iframe 检查误报成九宫格白烧题分）。"""
        try:
            return bool(self.page.eval(
                "(() => !!document.querySelector('#aliyunCaptcha-img') || "
                "!!document.querySelector('#captcha-element') || "
                "!!document.querySelector('#aliyunCaptcha-sliding-body'))()"))
        except Exception:  # noqa: BLE001
            return False

    def _solve_secondary_captcha(self, code: str, before_url: str,
                                 before: dict, ladder: list) -> tuple[bool, dict]:
        """自动模式二次验证分诊：拼图滑块/FeiLin滑块/九宫格各走各的打码。"""
        from app.register.baxia_puzzle_cdp import BaxiaPuzzleSlider

        pz = BaxiaPuzzleSlider(self.page, paid_client=self.paid,
                               proxy=self.proxy,
                               artifacts_dir=str(self.artifacts),
                               logger=self.log)
        try:
            is_puzzle = pz.present()
        except Exception:  # noqa: BLE001
            is_puzzle = False
        if is_puzzle:
            ladder.append("secondary-puzzle")
            self._d("二次验证=拼图滑块，走拼图打码（超级鹰/2Captcha）")
            out = pz.solve(max_attempts=3)
            if out.ok:
                self._sleep(1.5, 2.5)
                moved, state = self._otp_progressed(before_url, before)
                if moved:
                    return True, state
                # 解完没前进：重灌一次码触发验证 POST
                self.fill_otp(code)
                for _ in range(3):
                    self._sleep(1.5, 2.5)
                    moved, state = self._otp_progressed(before_url, before)
                    if moved:
                        return True, state
                return False, state
            self._d(f"拼图滑块未通过: {out.detail}")
            return False, before
        if self._secondary_slider_present():
            ladder.append("secondary-slider")
            self._d("二次验证=滑块，走滑块打码（超级鹰/2Captcha）")
            if self.step_captcha():
                self._sleep(2.0, 3.5)
                moved, state = self._otp_progressed(before_url, before)
                if moved:
                    return True, state
                # 解完没前进：重灌一次码触发验证 POST
                self.fill_otp(code)
                for _ in range(3):
                    self._sleep(1.5, 2.5)
                    moved, state = self._otp_progressed(before_url, before)
                    if moved:
                        return True, state
                return False, state
            return False, before
        return self._baxia_solve_resubmit(code, before_url, before, ladder)

    def _baxia_solve_resubmit(self, code: str, before_url: str,
                              before: dict, ladder: list) -> tuple[bool, dict]:
        """解 Baxia 九宫格并重发一次 OTP（解格拿到 x5sec 后页面不会自动重提）。"""
        from app.register.baxia_cdp import BaxiaClickCaptcha

        bx = BaxiaClickCaptcha(self.page, logger=self.log)
        try:
            present = bx.present()
        except Exception:  # noqa: BLE001
            present = False
        if not present:
            return False, before
        self._baxia_seen = True
        ladder.append("baxia")
        self._d("检测到 Baxia 九宫格点选，走打码平台（超级鹰优先/2Captcha）")
        try:
            from app import secrets as _secrets
            from app.net.chaojiying import build_click_client
            from app.net.twocaptcha_click import TwoCaptchaClick

            click_cli = build_click_client(TwoCaptchaClick(
                _secrets.get("captcha.twocaptcha_key", "")))
            out = bx.solve(
                click_cli, max_attempts=3,
                save_path=str(self.artifacts / f"baxia_grid_{self._uid}.png"))
            self._d(f"Baxia 结果 ok={out.ok} cells={out.cells} "
                    f"cost=${out.cost:.4f} {out.detail}")
            if out.ok:
                ladder.append(f"baxia-ok({out.cells})")
                self._sleep(1.0, 2.0)
                # 重灌一次码触发验证 POST（旧链路解格后不重提 → 必败）
                self.fill_otp(code)
                for _ in range(3):
                    self._sleep(1.5, 2.5)
                    moved, state = self._otp_progressed(before_url, before)
                    if moved:
                        return True, state
                return False, state
        except Exception as exc:  # noqa: BLE001
            self._d(f"Baxia 处理异常: {type(exc).__name__}: {exc}")
        return False, before

    def step_otp(self, code_provider) -> bool:
        otp_wait = self.page.wait_js(
            "(() => !!document.querySelector("
            "\"input[autocomplete='one-time-code'], input[maxlength='6']\"))()",
            timeout=25)
        if not otp_wait:
            # 实测：滑块过了页面会停在「Verified」，需要再点一次 Continue 才进 OTP
            self._d("OTP 未出现，补点一次提交")
            clicked = self._click_submit()
            if clicked:
                self._d(f"补点: {clicked}")
            otp_wait = self.page.wait_js(
                "(() => !!document.querySelector("
                "\"input[autocomplete='one-time-code'], input[maxlength='6']\"))()",
                timeout=20)
        if not otp_wait:
            body = self.page.text()[:200].replace("\n", " | ")
            self._shot("no_otp_input")
            self._stage("④ OTP 输入框", False, f"没出现。页面: {body}")
            return False

        self._stage("④ OTP 输入框", True, "已出现")
        self._d("等邮箱验证码……")
        # 收码走子线程（只碰 IMAP/DB，不碰 CDP），主线程补真人微动，
        # 避免等码期间整段静止的行为遥测破绽
        import concurrent.futures as _cf
        _wait_t0 = time.time()
        with _cf.ThreadPoolExecutor(max_workers=1) as _ex:
            _fut = _ex.submit(code_provider)
            while not _fut.done():
                # 等码期用户不在操作（人工闸门已过），即使人工模式也
                # 补真人手部微动，避免整段静止的行为遥测破绽
                self._otp_wait_idle(random.uniform(1.5, 3.0))
                waited = time.time() - _wait_t0
                if waited >= 15:
                    self._d(f"等码中… 已等 {waited:.0f}s")
                    _wait_t0 = time.time()
            code = _fut.result()
        if not code:
            self._shot("no_code_mail")
            self._stage("④ 收码", False, "未取到验证码")
            return False
        self._stage("④ 收码", True, f"code={code}")

        # 2026-11-19 用户要求提速：根因已定案为邮箱风险，拟人停留/
        # 切窗读码等戏剧化等待不再保留，收码后立即填码。

        digits = "".join(c for c in code if c.isdigit())[:6]
        before = self.page.eval(JS_VISIBLE_STATE) or {}
        before_url = str(before.get("url") or self.page.url())
        try:
            self.page.cdp.call("Network.enable")
        except Exception:  # noqa: BLE001
            pass

        if self.manual_otp:
            # 【人工填码实验】程序不碰键盘：输出验证码，等用户手动
            # 输入完成后 manual:go 通知，再观察页面是否前进/弹二次验证
            self._d("=" * 56)
            self._d(f"【人工填码】验证码: {digits}")
            self._d("请在页面手动输入验证码；完成后运行 manual:go 通知")
            self._d("=" * 56)
            if not self._wait_manual_gate():
                self._stage("④ 提交", False, "人工填码等待超时")
                return False
            ladder: list[str] = ["manual-otp"]
            moved = False
            state = before
            for _ in range(3):
                self._sleep(1.5, 2.5)
                moved, state = self._otp_progressed(before_url, before)
                if moved:
                    break
            if not moved and self._baxia_present():
                self._d("【人工填码】手动输入后仍出现二次验证 → "
                        "触发点不在填码环节，在前序链路")
                if self.manual_captcha:
                    moved, state = self._baxia_manual_gate(
                        digits, before_url, before, ladder)
            if not moved:
                self._shot("manual_otp_stuck")
                self._stage("④ 提交", False,
                            f"手动填码后未前进 url={self.page.url()}")
                return False
            self._stage("④ 提交", True,
                        f"人工填码后已离开注册页 -> {state.get('url')}（{ladder}）")
            return True

        sel = self.fill_otp(code)
        self._d(f"填码后 url={before_url} buttons={before.get('buttons')}")

        ladder: list[str] = []
        moved = False
        state = before

        # ① 等自动提交（组件 onComplete 自己发请求）
        for _ in range(2):
            self._idle_moves(random.uniform(1.2, 1.8))
            moved, state = self._otp_progressed(before_url, before)
            if moved:
                ladder.append("auto")
                break

        # ② Baxia 感知：惩罚页已在就不再轰 POST。自动模式先分诊
        #    （滑块/九宫格）求解再重提；人工模式停手等用户通知。
        if not moved:
            if self.manual_captcha:
                moved, state = self._baxia_manual_gate(
                    code, before_url, before, ladder)
            else:
                moved, state = self._solve_secondary_captcha(
                    code, before_url, before, ladder)

        # ③ 点可见按钮（至多一次；惩罚页在时不追加提交）
        if not moved and not self._baxia_present():
            clicked = self._click_submit()
            if clicked:
                ladder.append(f"click:{clicked}")
                self._sleep(2.0, 3.0)
                moved, state = self._otp_progressed(before_url, before)

        # ④ 重新注入 + 回车（至多一次；旧版两轮+force+form.submit 连发
        #    多个验证 POST，惩罚后继续轰只会累加风险）
        if not moved and not self._baxia_present():
            sel2 = self._otp_selector() or sel
            if sel2:
                try:
                    self.page.eval(
                        self._js_otp_fill(json.dumps(sel2),
                                          json.dumps(digits)))
                except Exception:  # noqa: BLE001
                    pass
                self._sleep(0.4, 0.8)
                self.page.click(sel2, human=self.human)
                if self.page.os_input is not None and self.page.os_input.ready:
                    try:
                        self.page.os_input.key_enter()
                    except Exception:  # noqa: BLE001
                        self.page.key("Enter")
                else:
                    self.page.key("Enter")
                ladder.append("react+enter")
                self._sleep(2.0, 2.8)
                moved, state = self._otp_progressed(before_url, before)

        # ⑤ OTP 之后可能再弹一次滑块（实测存在，别漏）
        if not moved:
            has_cap = self.page.wait_js(
                "(() => !!document.querySelector('#aliyunCaptcha-img') || "
                "!!document.querySelector('#captcha-element'))()", timeout=5)
            if has_cap:
                ladder.append("captcha")
                self._d("OTP 后又出现验证码，再解一次")
                if self.step_captcha():
                    self._sleep(3.0, 4.5)
                    moved, state = self._otp_progressed(before_url, before)

        # ⑥ 阿里 Baxia 九宫格点选（迟到的惩罚页）：人工模式停手等通知，
        #    自动模式分诊求解 + 重提
        if not moved:
            if self.manual_captcha:
                moved, state = self._baxia_manual_gate(
                    code, before_url, before, ladder)
            else:
                moved, state = self._solve_secondary_captcha(
                    code, before_url, before, ladder)

        url = str(state.get("url") or self.page.url())
        body = str(state.get("body") or "")
        self._d(f"提交阶梯={ladder} 结果 url={url} moved={moved}")

        # 抓网络与页面提示：判断「提交请求到底发没发、回了什么」
        net = self._net_log()
        try:
            toast = self.page.eval(
                """(() => Array.from(document.querySelectorAll(
                    '[class*=message],[class*=alert],[class*=error],'
                    + '[class*=toast],[role=alert]'))
                  .map(e => (e.innerText || '').trim()).filter(Boolean)
                  .slice(0, 5))()""") or []
        except Exception:
            toast = []
        self.report.otp_debug.setdefault("network", net)
        self.report.otp_debug.setdefault("toast", toast)
        if net:
            self._d("网络事件: " + " | ".join(
                f"{n.get('t')}:{n.get('m') or n.get('s')} {n.get('url')}"
                for n in net[-6:]))
        if toast:
            self._d(f"页面提示: {toast}")

        if not moved:
            dbg = self._dump("otp_stuck", JS_DEEP_DUMP)
            self.report.otp_debug = dbg
            self._dump_html("otp_stuck")
            self._shot("otp_stuck")
            self._stage("④ 提交", False,
                        f"阶梯全部失效 {ladder}，otpValue={state.get('otpValue')} "
                        f"buttons={[b.get('text') for b in state.get('buttons') or []]}")
            return False

        self._sleep(1.5, 2.5)
        state = self.page.eval(JS_VISIBLE_STATE) or state
        url = str(state.get("url") or self.page.url())
        body = str(state.get("body") or "")
        if "/sign-up" in url:
            self._dump_html("otp_partial")
            self._stage("④ 提交", False, f"仍在注册页 url={url} 阶梯={ladder}")
            return False

        self._stage("④ 提交", True, f"已离开注册页 -> {url}（{ladder}）")
        low = body.lower()
        ok_markers = ("welcome", "dashboard", "get started", "workspace",
                      "sign out", "logout", "qoder ide")
        if any(m in low for m in ok_markers):
            self._stage("⑤ 登录态", True, "页面出现登录后标志")
        else:
            self._stage("⑤ 登录态", True, f"url 已跳转（正文 {len(body)} 字）")
        return True

    def _retry_flow(self, email: str, first: str, last: str, pwd: str,
                    code_provider) -> bool:
        """Go back 恢复表单后重走 ①②③④。

        实测（acct21）：Go back 回到 Email/Password（②步）表单；有的情况回①步
        全表单，这里两种都兼容。环境已过一次滑块，重走时大多无感通过。
        """
        self._sleep(1.5, 2.5)
        ok = True
        if self.page.exists("#basic_firstName"):          # 回到①步全表单
            ok = self.step_identity(email, first, last)
            self._sleep(1.5, 2.5)
        if not (ok and self.step_password(pwd)):
            return False
        self._sleep(2.0, 3.0)
        if not self.step_captcha():
            return False
        if code_provider is not None and not self.step_otp(code_provider):
            return False
        return True

    # ---------------------------------------------------------- 主流程
    def run(self, email: str, password: str | None = None,
            code_provider=None, stop_before: str = "") -> FlowReport:
        pwd = password or gen_password()
        first, last = pick_name(email)
        # 不再无脑 Page.enable（零 domain 附着原则，导航/截图不需要它）
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

            if self.manual_form:
                # 【人工填表二分实验】①②+滑块全由用户手动，程序零操作
                # 等待（不导航不求值不输入），完成后接 OTP 环节
                if self.form_nav:
                    # 【仅导航实验】程序只导航到注册页，其余全手动
                    self.page.goto(HOME_URL, wait_ms=random.randint(1500, 2500))
                    self.page.goto(SIGNUP_URL, wait_ms=random.randint(1500, 2500))
                    self._d("【仅导航实验】已导航到注册页，请全程手动填表")
                self._d("=" * 56)
                self._d(f"【人工填表】请手动注册：{email}")
                self._d(f"密码: {pwd}")
                self._d("手动填①②并过滑块，到验证码输入页后运行 manual:go")
                self._d("=" * 56)
                if not self._wait_manual_gate():
                    res.stage, res.detail = "step1", "人工填表等待超时"
                    return self._finish(res)
                res.stage, res.detail = "manual-form", "人工填表完成"
            elif not self.step_identity(email, first, last):
                res.stage, res.detail = "step1", "第①步失败"
                return self._finish(res)
            if not self.manual_form:
                self._sleep(2.0, 3.0)
                if not self.step_password(pwd):
                    res.stage, res.detail = "step2", "第②步失败"
                    return self._finish(res)
                self._sleep(2.0, 3.5)
            otp_done = False          # Go back 重走时若已含④，就不再跑一遍
            if not self.manual_form and not self.step_captcha():
                # 兑底：明确拒绝（错误条）后 refresh 常把组件锁死（expand-lock）
                # 或判定延迟被打断成「Verified|Go back」假通过页。点 Go back
                # 恢复表单重走 ①②③④（acct21 实测可恢复）。
                if self._goback_recover() and \
                        self._retry_flow(email, first, last, pwd, code_provider):
                    otp_done = code_provider is not None
                    self._d("③ 失败后 Go back 重走成功")
                else:
                    res.stage, res.detail = "step3", "第③步滑块未通过"
                    return self._finish(res)

            if code_provider is None:
                res.stage, res.detail = "step4", "没有收码回调"
                return self._finish(res)
            if not otp_done and not self.step_otp(code_provider):
                # 兑底：判定延迟被打断后死在「Verified|Go back」假通过页。
                # 点 Go back 恢复表单，重走 ①②③。
                # 已被 Baxia 惩罚的会话不再同会话重试（放大风险），
                # 直接失败回池，下轮换新存储。
                if not self._baxia_seen and self._goback_recover() and \
                        self._retry_flow(email, first, last, pwd, code_provider):
                    res.ok, res.stage = True, "step4"
                    res.detail = "OTP 补救（Go back 恢复）"
                else:
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
