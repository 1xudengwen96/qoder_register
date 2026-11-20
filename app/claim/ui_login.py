"""领取阶段的登录自动化：链接获取 → 浏览器登录 → 回跳客户端 → 落 token。

客户端（Qoder 0.5.7）的登录是这样走的：

    客户端点「登录」
      → 生成 PKCE 设备绑定链接
        https://www.qoder.com/device/selectAccounts?nonce=…&challenge=…
            &redirect_uri=qoder://…/login-success&machine_token=…&machine_id=…
      → 用默认浏览器打开该链接
      → 网页登录（邮箱 + 密码 [+ 6 位邮箱验证码]）
      → 回跳 qoder://…/login-success
      → 客户端写入 %APPDATA%\\Qoder\\SharedClientCache\\cache\\machine_token.json
      → 我们从该文件抓 token

能自动的部分：打开链接、填邮箱密码、取验证码、捕捉 qoder:// 回跳、拉起客户端回调、抓 token。
「登录」按钮：旧版客户端需人手点一次；2026-11-19 实测新版客户端接受
--remote-debugging-port（cdp 模式），auto_click_client_login 可经客户端
自身 CDP 自动点「立即登录」+「复制登录链接」，实现全程无人值守；
失败自动回退人工点一次提示。

链接来源按优先级：
1. 剪贴板（点了「复制登录链接」）
2. Chrome / Edge 历史记录里最近的 device/selectAccounts 链接（客户端自动开浏览器时）
3. 轮询等待，直到超时
"""

from __future__ import annotations

import dataclasses
import glob
import json
import logging
import os
import re
import shutil
import sqlite3
import tempfile
import time
from pathlib import Path

log = logging.getLogger("claim.login")
# 挂进项目日志树，否则领取进度在 run-*.log / 控制台不可见
log.propagate = False
_parent = logging.getLogger("qoder_register")
if not log.handlers:
    for _h in _parent.handlers:
        log.addHandler(_h)
log.setLevel(_parent.level or logging.INFO)

LINK_RE = re.compile(r"https://(?:www\.)?qoder\.com/device/selectAccounts\?[^\s\"'<>]+",
                     re.I)
DEEP_LINK_RE = re.compile(r"(qoder(?:-ide)?://[^\s\"'<>]+)", re.I)


@dataclasses.dataclass(slots=True)
class LoginOutcome:
    ok: bool
    link: str = ""
    deep_link: str = ""
    final_url: str = ""
    env_id: str = ""
    error: str = ""
    screenshot: str = ""
    credits: int = 0
    plan: str = ""
    credits_detail: dict = dataclasses.field(default_factory=dict)

    def as_dict(self) -> dict:
        return {
            "ok": self.ok, "link": self.link[:90], "deep_link": self.deep_link[:90],
            "final_url": self.final_url, "env_id": self.env_id, "error": self.error,
            "credits": self.credits, "plan": self.plan,
        }


# ------------------------------------------------------------ 链接获取
def from_clipboard() -> str:
    """读剪贴板里的登录链接（PowerShell 取，避免额外依赖）。"""
    if os.name != "nt":
        return ""
    try:
        import subprocess

        out = subprocess.run(
            ["powershell", "-NoProfile", "-Command", "Get-Clipboard -Raw"],
            capture_output=True, timeout=10,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        text = out.stdout.decode("utf-8", "ignore")
    except Exception:  # noqa: BLE001
        return ""
    m = LINK_RE.search(text or "")
    return m.group(0) if m else ""


def _chrome_history_files() -> list[Path]:
    roots = [
        Path(os.environ.get("LOCALAPPDATA", "")) / "Google/Chrome/User Data",
        Path(os.environ.get("LOCALAPPDATA", "")) / "Microsoft/Edge/User Data",
        Path(os.environ.get("LOCALAPPDATA", "")) / "BraveSoftware/Brave-Browser/User Data",
    ]
    files: list[Path] = []
    for root in roots:
        if not root.exists():
            continue
        for pat in ("*/History", "History"):
            files.extend(Path(p) for p in glob.glob(str(root / pat)))
    return files


def from_browser_history(max_age_sec: int = 1800,
                         since: float | None = None) -> str:
    """从 Chrome/Edge 历史里捞最近的设备登录链接。

    浏览器运行时会锁库，所以先拷到临时文件再读。
    """
    cutoff = time.time() - max_age_sec
    if since is not None:
        cutoff = max(cutoff, since - 120)   # 只认本次操作之后产生的链接，旧 nonce 无效
    best: tuple[float, str] | None = None
    for db in _chrome_history_files():
        tmp = ""
        try:
            fd, tmp = tempfile.mkstemp(suffix=".sqlite")
            os.close(fd)
            shutil.copy2(db, tmp)
            con = sqlite3.connect(tmp)
            try:
                rows = con.execute(
                    "SELECT url, last_visit_time FROM urls "
                    "WHERE url LIKE '%qoder.com/device/selectAccounts%' "
                    "ORDER BY last_visit_time DESC LIMIT 5").fetchall()
            finally:
                con.close()
            for url, visit in rows:
                # Chrome 时间：1601-01-01 起的微秒
                ts = (int(visit) / 1_000_000) - 11644473600
                if ts < cutoff:
                    continue
                if best is None or ts > best[0]:
                    best = (ts, str(url))
        except Exception:  # noqa: BLE001
            continue
        finally:
            if tmp:
                try:
                    os.unlink(tmp)
                except OSError:
                    pass
    return best[1] if best else ""


def wait_link(timeout: int = 180, poll: int = 3,
              prefer_clipboard: bool = True,
              since: float | None = None) -> tuple[str, str]:
    """等一条可用的登录链接，返回 (链接, 来源)。

    `since` 传本次操作开始的时间戳：历史记录里早于它的链接一律不认，
    避免把上一轮已经消费掉的 nonce 拿来重放（页面能打开但绑定必然失败）。
    """
    if since is None:
        since = time.time()
    deadline = time.time() + timeout
    while time.time() < deadline:
        if prefer_clipboard:
            got = from_clipboard()
            if got:
                return got, "clipboard"
        got = from_browser_history(since=since)
        if got:
            return got, "browser-history"
        if not prefer_clipboard:
            got = from_clipboard()
            if got:
                return got, "clipboard"
        time.sleep(poll)
    return "", ""


# ------------------------------------------------------------ 浏览器登录
_JS_PAGE_STATE = """(() => ({
  url: location.href,
  text: (document.body ? document.body.innerText : '').slice(0, 400),
  inputs: Array.from(document.querySelectorAll('input')).map(i => ({
    id: i.id || '', type: i.type, ph: i.placeholder || ''})),
  buttons: Array.from(document.querySelectorAll('button, [role=button]'))
    .map(b => (b.innerText || '').trim()).filter(Boolean).slice(0, 8),
  deepLink: (() => {
    const all = Array.from(document.querySelectorAll('a[href]'))
      .map(a => a.getAttribute('href'))
      .filter(h => h && h.startsWith('qoder'));
    const inText = (document.body ? document.body.innerText : '')
      .match(/qoder(?:-ide)?:\\/\\/[^\\s"'<>]+/);
    return all[0] || (inText ? inText[0] : '');
  })(),
}))()"""


def _fill(page, selector: str, text: str) -> bool:
    try:
        if not page.exists(selector):
            return False
        page.click(selector, human=True)
        page.fill(selector, text, human=True)
        return True
    except Exception as exc:  # noqa: BLE001
        log.warning("填写 %s 失败：%s", selector, exc)
        return False


def _click_text(page, *names: str) -> str:
    for name in names:
        try:
            if page.click_text(name):
                return name
        except Exception:  # noqa: BLE001
            continue
    return ""


def _state(page) -> dict:
    try:
        return page.eval(_JS_PAGE_STATE) or {}
    except Exception:  # noqa: BLE001
        return {}


def _visible(page, selector: str) -> bool:
    try:
        return page.exists(selector) and page.sel_ready(selector, timeout=1, visible=True)
    except Exception:  # noqa: BLE001
        return False


def _solve_slider(page, cfg, proxy_raw: str | None) -> str:
    """人机验证（阿里云 FeiLin）：只走 2Captcha（本地识别已移除）。"""
    from app.net.twocaptcha_dragdrop import TwoCaptchaDragDrop
    from app.register.aliyun_slider_cdp import AliyunSliderCDP

    key = ""
    if cfg is not None:
        from app.config import get
        key = str(get(cfg, "captcha.providers.2captcha.api_key", "") or "")
    key = key or os.environ.get("TWOCAPTCHA_KEY", "")
    paid = None
    if key:
        paid = TwoCaptchaDragDrop(key, proxy=None)
        try:
            log.info("2Captcha 余额 %.4f", paid.balance())
        except Exception as exc:  # noqa: BLE001
            log.warning("余额查询失败（继续）：%s", exc)
    # 超级鹰试验通道：启用时优先，API 异常自动回退 2Captcha
    from app.net.chaojiying import build_slider_paid

    paid = build_slider_paid(paid)
    solver = AliyunSliderCDP(page, human=True, paid_client=paid,
                             prefer_paid=paid is not None, proxy=proxy_raw)
    res = solver.solve(max_attempts=4)
    return f"{'OK' if res.ok else 'FAIL'} {res.detail} method={res.method}"


def drive_login(
    cfg,
    link: str,
    email: str,
    password: str,
    *,
    env_name: str = "",
    region: str = "us",
    timeout: int = 300,
    artifacts_dir: str | Path = "data/artifacts",
    proxy=None,
    close_when_done: bool = True,
) -> LoginOutcome:
    """在比特浏览器里把登录走完，返回是否拿到回跳链接 / 是否已登录。

    状态机（每 2 秒读一次页面，按当前状态动作，不按固定顺序硬点）：

        sign-in 第一步   #basic_email       → 填邮箱 → Continue
        sign-in 第二步   #password_password → 填密码 → Sign in
        人机验证         "are you human"    → 点开验证器 → 滑块求解（2Captcha）
        选择账号         selectAccounts     → 点第一个账号 / Continue
        已登录           url 离开 sign-in   → 成功
        回跳             qoder://…          → 拿到深链
    """
    from app.register import bb_cdp
    from app.register.bitbrowser import BitBrowserClient, resolve_base
    from app.register.cdp_page import new_page

    out = LoginOutcome(False, link=link)
    artifacts = Path(artifacts_dir)
    artifacts.mkdir(parents=True, exist_ok=True)

    client = BitBrowserClient(resolve_base(cfg))
    launch = None
    page = None
    proxy_raw = getattr(proxy, "raw", None)
    try:
        env_name = env_name or "qoder_login"
        reused_id = bb_cdp.find_env_id(client, env_name) or ""
        if reused_id:
            # 一一对应：注册环境的指纹 / 代理 / IP / 登录 cookie 全部
            # 保持原样（不重写代理，直连重写会换 IP 踢会话），只启动。
            endpoint, res = bb_cdp.launch_env(client, reused_id)
            launch = bb_cdp.BBLaunch(
                env_id=reused_id, env_name=env_name, endpoint=endpoint,
                created=False, pid=res.pid, core_version=res.core_version,
                proxy_label="复用注册环境", proxy_raw=(proxy_raw or ""),
            )
            log.info("复用环境 %s（注册一一对应，代理不改写）cdp=%s",
                      env_name, endpoint)
        else:
            try:
                launch = bb_cdp.open_for_register(
                    client, proxy=proxy, region=region,
                    env_name=env_name, reuse_env=True,
                )
            except Exception as exc:  # noqa: BLE001
                # 新建/重配被拒（环境上限等）：回退任一现有池窗，
                # 无会话 cookie 时状态机会走邮箱+密码登录分支
                log.warning("登录窗创建/复用失败（%s），回退现有池窗", exc)
                envs, _ = client.list_envs(page=0, page_size=200)
                cand = next((e for e in envs
                             if (e.name or "").startswith("qpool_")), None)
                if cand is None:
                    raise
                endpoint, res = bb_cdp.launch_env(client, cand.id)
                launch = bb_cdp.BBLaunch(
                    env_id=cand.id, env_name=cand.name, endpoint=endpoint,
                    created=False, pid=res.pid,
                    core_version=res.core_version,
                    proxy_label="回退池窗", proxy_raw="",
                )
        out.env_id = launch.env_id
        log.info("登录窗口 env=%s cdp=%s 代理=%s", launch.env_name,
                 launch.endpoint, launch.proxy_label)
        page = new_page(launch.endpoint, "about:blank")
        # 填表走 OS 级真实输入（与注册段同一套反检测对策）
        try:
            from app.register.os_input import OSInput

            page.os_input = OSInput(page, launch.pid)
        except Exception as exc:  # noqa: BLE001
            log.warning("OS 输入器初始化异常（用 CDP 注入）：%s", exc)
        page.goto(link, wait_ms=4000)

        deadline = time.time() + timeout
        did = {"email": False, "password": False, "verify": 0, "account": 0}
        last_text = ""

        while time.time() < deadline:
            st = _state(page)
            url = str(st.get("url") or "")
            text = str(st.get("text") or "")
            low = text.lower()
            # 键名必须与 _JS_PAGE_STATE 返回的 deepLink 一致
            # （之前误写成 st.get("deep")，导致 qoder:// 回跳永远抓不到）
            deep = str(st.get("deepLink") or "")
            if deep:
                out.deep_link = deep
                out.ok = True
                out.final_url = url
                break

            # 授权完成贺屏：URL 仍停在 selectAccounts、无 anchor，
            # 但正文已「登录成功/一切就绪」（实测 2026-11-19），
            # 客户端经协议回跳/轮询完成绑定——认正文为成功信号
            if any(k in text for k in ("登录成功", "一切就绪", "返回 Qoder",
                                       "Login successful", "You're all set",
                                       "all set")):
                out.ok = True
                out.final_url = url
                log.info("授权页显示登录成功，客户端绑定生效")
                break

            if url and "users/sign-in" not in url and "users/sign-up" not in url \
                    and "device/selectAccounts" not in url:
                out.ok = True
                out.final_url = url
                log.info("已离开登录页 -> %s", url[:100])
                break

            human = ("are you human" in low or "sure you are human" in low
                     or "点击验证" in text
                     or _visible(page, "#captcha-button")
                     or _visible(page, "#aliyunCaptcha-img"))
            if human:
                if did["verify"] < 5:
                    did["verify"] += 1
                    log.info("人机验证（第 %d 次）", did["verify"])
                    clicked = False
                    for sel in ("#captcha-button", "#aliyunCaptcha-start-icon",
                                "#captcha-element"):
                        try:
                            if _visible(page, sel):
                                page.click(sel, human=True)
                                clicked = True
                                log.info("点了验证器 %s", sel)
                                break
                        except Exception:  # noqa: BLE001
                            continue
                    if not clicked:
                        for name in ("Click to verify", "点击验证", "验证", "Verify"):
                            try:
                                if page.click_text(name):
                                    clicked = True
                                    log.info("点了文本按钮 %s", name)
                                    break
                            except Exception:  # noqa: BLE001
                                continue
                    # 等一下：智能验证可能直接过，也可能弹出滑块
                    appeared = False
                    for _ in range(10):
                        time.sleep(1)
                        if _visible(page, "#aliyunCaptcha-img"):
                            appeared = True
                            break
                        now_url = str(_state(page).get("url") or "")
                        if now_url and now_url != url and "users/sign-in" not in now_url:
                            break
                    if appeared:
                        try:
                            log.info("滑块：%s", _solve_slider(page, cfg, proxy_raw))
                        except Exception as exc:  # noqa: BLE001
                            log.warning("滑块求解异常：%s", exc)
                    else:
                        log.info("智能验证没弹滑块，继续观察")
                    time.sleep(4)
                continue

            if _visible(page, "#password_password") or \
                    ("credentialStep" in url and _visible(page, "#basic_password")):
                sel = "#password_password" if _visible(page, "#password_password") \
                    else "#basic_password"
                if not did["password"]:
                    did["password"] = True
                    log.info("填密码 -> %s", sel)
                    try:
                        page.click(sel, human=True)
                        page.fill(sel, password, human=True)
                    except Exception as exc:  # noqa: BLE001
                        log.warning("填密码失败：%s", exc)
                    time.sleep(0.6)
                    for name in ("Sign in", "登录", "Continue", "继续"):
                        try:
                            if page.click_text(name):
                                log.info("点了 %s", name)
                                break
                        except Exception:  # noqa: BLE001
                            continue
                    time.sleep(4)
                continue

            if _visible(page, "#basic_email") and not did["email"]:
                did["email"] = True
                log.info("填邮箱")
                try:
                    page.click("#basic_email", human=True)
                    page.fill("#basic_email", email, human=True)
                except Exception as exc:  # noqa: BLE001
                    log.warning("填邮箱失败：%s", exc)
                time.sleep(0.6)
                for name in ("Continue", "继续", "Next"):
                    try:
                        if page.click_text(name):
                            log.info("点了 %s", name)
                            break
                    except Exception:  # noqa: BLE001
                        continue
                time.sleep(3)
                continue

            if "selectAccounts" in url or "选择账" in text or "Select account" in text:
                if did["account"] < 3:
                    did["account"] += 1
                    log.info("账号选择页（第 %d 次）", did["account"])
                    # 主确认按钮（繼續/Continue/继续）统一是 ant-btn-primary：
                    # 语言无关（us 英 / hk 繁中 / 简中三套文案都不用猜），
                    # 原生 click 实测有效。文本匹配只做兜底。
                    clicked = False
                    try:
                        clicked = bool(page.eval(
                            "(() => { const b = document.querySelector("
                            "'button.ant-btn-primary');"
                            " if (!b) return false; b.click(); return true; })()"))
                    except Exception:  # noqa: BLE001
                        clicked = False
                    if not clicked:
                        for name in ("Continue", "继续", "繼續", "Select",
                                     "选择", "選擇", "确认", "確認"):
                            try:
                                if page.click_text(name):
                                    clicked = True
                                    break
                            except Exception:  # noqa: BLE001
                                continue
                    log.info("账号选择页点击 -> %s", clicked)
                    time.sleep(3)
                continue

            if text != last_text:
                last_text = text
                log.debug("页面：%s", text[:120].replace(chr(10), " "))
            time.sleep(2)

        out.final_url = out.final_url or str(_state(page).get("url") or "")

        # 登录态还在这个浏览器 profile 里：立刻读额度，别等下次换 IP 把 session 踢了
        if out.ok:
            try:
                from app.claim.credit_page import PROFILE_URL, USAGE_URL, parse_plan, parse_usage

                page.goto(USAGE_URL, wait_ms=4500)
                time.sleep(1.5)
                usage = parse_usage(page.text())
                page.goto(PROFILE_URL, wait_ms=3500)
                time.sleep(1.0)
                usage.plan = parse_plan(page.text()) or usage.plan
                out.credits = usage.credits
                out.plan = usage.plan
                out.credits_detail = usage.as_dict()
                log.info("会话内额度：%s 计划=%s", usage.as_dict(), usage.plan)
            except Exception as exc:  # noqa: BLE001
                log.warning("会话内读额度失败：%s", exc)

        out.screenshot = page.shot(str(artifacts / "login_final.png"))
        if not out.ok:
            out.error = f"登录未完成，停在 {out.final_url[:90]}"
        return out
    except Exception as exc:  # noqa: BLE001
        out.error = f"{type(exc).__name__}: {exc}"
        log.exception("登录流程异常")
        return out
    finally:
        try:
            if page is not None:
                page.close()
        except Exception:  # noqa: BLE001
            pass
        if launch and close_when_done:
            bb_cdp.close_env(client, launch.env_id)


def dispatch_deep_link(deep_link: str) -> bool:
    """把 qoder://… 交给系统协议处理器（会拉起 Qoder 客户端完成绑定）。"""
    if not deep_link:
        return False
    try:
        if os.name == "nt":
            os.startfile(deep_link)  # noqa: S606
        else:
            import subprocess

            opener = "open" if os.uname().sysname == "Darwin" else "xdg-open"
            subprocess.Popen([opener, deep_link])
        log.info("已投递回跳链接：%s", deep_link[:80])
        return True
    except Exception as exc:  # noqa: BLE001
        log.warning("投递回跳链接失败：%s", exc)
        return False


# ------------------------------------------------------------ 客户端自动点登录
JS_CLIENT_CLICK_LOGIN = """(() => {
  const els = Array.from(document.querySelectorAll('button,a,[role=button]'));
  const txt = (e) => (e.innerText || '').trim();
  let hit = els.find(e => ['立即登录', 'log in', 'sign in', 'login']
    .includes(txt(e).toLowerCase()));
  if (!hit) hit = els.find(e => {
    const t = txt(e).toLowerCase();
    return t.includes('登录') && !t.includes('反馈') && !t.includes('无法');
  });
  if (!hit) return {clicked: false};
  hit.click();
  return {clicked: true, text: txt(hit)};
})()"""

JS_CLIENT_COPY_LINK = """(() => {
  const els = Array.from(document.querySelectorAll('button,a,[role=button]'));
  const hit = els.find(e => {
    const t = (e.innerText || '').trim().toLowerCase();
    return t.includes('复制登录链接') || t.includes('copy login link') ||
           t.includes('copy link');
  });
  if (!hit) return {clicked: false};
  hit.click();
  return {clicked: true};
})()"""


def auto_click_client_login(port: int = 9222, timeout: float = 25) -> bool:
    """无人值守：客户端自身 CDP 点「立即登录」→「复制登录链接」入剪贴板。

    2026-11-19 实测：客户端 cdp 模式带 --remote-debugging-port，
    登录浮层按钮可 DOM click；点后进入「等待浏览器确认」态并出现
    「复制登录链接」。失败返回 False，回退人工点一次提示。
    """
    import urllib.request

    from app.register.cdp_page import CDPPage
    try:
        with urllib.request.urlopen(
                f"http://127.0.0.1:{port}/json/list", timeout=3) as r:
            ts = json.loads(r.read().decode("utf-8", "replace"))
    except Exception:  # noqa: BLE001
        return False
    pages = [t for t in ts if t.get("type") == "page"
             and t.get("webSocketDebuggerUrl")]
    # 登录浮层优先：workbench 页启动期求值可能挂起，放后面
    pages.sort(key=lambda t: 0 if ("onboarding" in (t.get("url") or "")
                                   or "entry=login" in (t.get("url") or "")) else 1)
    deadline = time.time() + timeout
    for t in pages:
        p = None
        try:
            # 短超时：客户端启动期个别页求值会挂起，长超时一次耗光
            # 重试预算（2026-11-19 实测 60s 挂起导致自动登录失败）
            p = CDPPage(f"http://127.0.0.1:{port}",
                        ws_url=t["webSocketDebuggerUrl"], timeout=8)
            r = p.eval(JS_CLIENT_CLICK_LOGIN) or {}
            if not r.get("clicked"):
                continue
            log.info("[ui-login] 客户端 CDP 点了「%s」", r.get("text"))
            while time.time() < deadline:
                c = p.eval(JS_CLIENT_COPY_LINK) or {}
                if c.get("clicked"):
                    log.info("[ui-login] 客户端 CDP 点了「复制登录链接」")
                    time.sleep(0.6)
                    return True
                time.sleep(1.0)
            return True   # 登录已点，链接也可能会进浏览器历史
        except Exception as exc:  # noqa: BLE001
            log.warning("[ui-login] 客户端自动登录异常：%s", exc)
        finally:
            if p is not None:
                try:
                    p.close()
                except Exception:  # noqa: BLE001
                    pass
    return False


# ------------------------------------------------------------ 客户端弹层/权益
JS_ONBOARD_SKIP = """(() => {
  const els = Array.from(document.querySelectorAll('button,a,[role=button],div,span'));
  const hit = els.find(e => ['跳过', 'skip', '开始使用', 'get started']
    .includes((e.innerText || '').trim().toLowerCase())
    && e.getBoundingClientRect().width > 0);
  if (!hit) return false;
  hit.click();
  return true;
})()"""

JS_DISMISS = """(() => {
  const want = ['忽略', '跳过', '开始使用', '知道了', '稍后', '以后再说', '关闭'];
  const els = Array.from(document.querySelectorAll('button,a,[role=button],div,span'))
    .filter(e => want.includes((e.innerText || '').trim())
      && e.getBoundingClientRect().width > 0);
  if (!els.length) return null;
  els.sort((a, b) => (a.innerText || '').length - (b.innerText || '').length);
  const r = els[0].getBoundingClientRect();
  return {x: r.x + r.width / 2, y: r.y + r.height / 2};
})()"""

JS_CLAIM_BTN = """(() => {
  const want = ['领取', '立即领取', '领取权益', '领取新手礼', 'claim',
                'claim now', 'claim benefits', '激活', '开始试用',
                'start trial', '立即体验'];
  const els = Array.from(document.querySelectorAll('button,a,[role=button]'))
    .filter(e => {
      const t = (e.innerText || '').trim().toLowerCase();
      return want.some(w => t === w || t.startsWith(w))
        && e.getBoundingClientRect().width > 0;
    });
  if (!els.length) return null;
  const r = els[0].getBoundingClientRect();
  return {text: (els[0].innerText || '').trim(),
          x: r.x + r.width / 2, y: r.y + r.height / 2};
})()"""


def _client_pages(port: int = 9222) -> list:
    import urllib.request
    try:
        with urllib.request.urlopen(
                f"http://127.0.0.1:{port}/json/list", timeout=3) as r:
            ts = json.loads(r.read().decode("utf-8", "replace"))
    except Exception:  # noqa: BLE001
        return []
    return [t for t in ts if t.get("type") == "page"
            and t.get("webSocketDebuggerUrl")]


def dismiss_client_overlays(port: int = 9222, rounds: int = 8) -> int:
    """清客户端新手引导/促销弹层（onboarding 多步跳过 + workbench 忽略）。

    引导浮层会挡住工作区一切点击（实测权益中心点不动的根因之一）。
    返回点击次数。"""
    from app.register.cdp_page import CDPPage
    n = 0
    for _ in range(rounds):
        acted = False
        for t in _client_pages(port):
            url = t.get("url") or ""
            try:
                p = CDPPage(f"http://127.0.0.1:{port}",
                            ws_url=t["webSocketDebuggerUrl"], timeout=8)
            except Exception:  # noqa: BLE001
                continue
            try:
                if "onboarding" in url:
                    if p.eval(JS_ONBOARD_SKIP):
                        n += 1
                        acted = True
                elif "workbench" in url:
                    d = p.eval(JS_DISMISS)
                    if d:
                        p.mouse("mouseMoved", float(d["x"]), float(d["y"]))
                        time.sleep(0.2)
                        p.mouse("mousePressed", float(d["x"]), float(d["y"]),
                                buttons=1)
                        time.sleep(0.1)
                        p.mouse("mouseReleased", float(d["x"]), float(d["y"]),
                                buttons=0)
                        n += 1
                        acted = True
            except Exception:  # noqa: BLE001
                pass
            finally:
                try:
                    p.close()
                except Exception:  # noqa: BLE001
                    pass
        if not acted:
            break
        time.sleep(2)
    if n:
        log.info("[ui-login] 客户端弹层清理 %d 次", n)
    return n


def claim_client_campaign(port: int = 9222, rounds: int = 4) -> bool:
    """领取客户端「恭喜成为 Qoder 新用户」贺屏/权益中心里的领取按钮。

    扫所有客户端 page target（贺屏可能是独立 webview），多轮捕获
    跳过引导后才弹出的贺屏；best-effort，不阻断领取主流程。
    """
    from app.register.cdp_page import CDPPage
    claimed = False
    for _ in range(rounds):
        hit_any = False
        for t in _client_pages(port):
            url = t.get("url") or ""
            try:
                p = CDPPage(f"http://127.0.0.1:{port}",
                            ws_url=t["webSocketDebuggerUrl"], timeout=8)
            except Exception:  # noqa: BLE001
                continue
            try:
                if "workbench" in url:
                    # 开用量面板（Credits 入口），campaign 内容嵌在其中
                    p.eval("""(() => {
                      const els = Array.from(document.querySelectorAll('button,a,[role=button],div,span'));
                      const hit = els.find(e => (e.innerText || '').trim() === 'Credits'
                        && e.getBoundingClientRect().width > 0);
                      if (hit) hit.click();
                    })()""")
                    time.sleep(2)
                btn = p.eval(JS_CLAIM_BTN)
                if btn:
                    log.info("[ui-login] 客户端权益按钮: %s @%s",
                             btn.get("text"), url[:50])
                    p.mouse("mouseMoved", float(btn["x"]), float(btn["y"]))
                    time.sleep(0.2)
                    p.mouse("mousePressed", float(btn["x"]), float(btn["y"]),
                            buttons=1)
                    time.sleep(0.1)
                    p.mouse("mouseReleased", float(btn["x"]), float(btn["y"]),
                            buttons=0)
                    time.sleep(3)
                    claimed = True
                    hit_any = True
            except Exception as exc:  # noqa: BLE001
                log.warning("[ui-login] 权益领取尝试异常：%s", exc)
            finally:
                try:
                    p.close()
                except Exception:  # noqa: BLE001
                    pass
        if not hit_any:
            break
        time.sleep(2)
    return claimed


# ------------------------------------------------------------ 回调工厂
def make_ui_login(
    cfg,
    email: str,
    password: str,
    *,
    env_name: str = "",
    region: str = "us",
    link_timeout: int = 180,
    login_timeout: int = 300,
    artifacts_dir: str | Path = "data/artifacts",
) -> tuple[object, dict]:
    """构造给 ``Claimer.perform(ui_login=...)`` 用的半自动登录回调。

    cli ``claim:run --login`` 与 orchestrator 领取段（``claim.login_mode: cdp``）
    共用这套逻辑：提示人点一次客户端「登录」（或「复制登录链接」）→
    ``wait_link`` 拿 selectAccounts 链接 → 取代理 → ``drive_login`` 走网页
    登录 → ``dispatch_deep_link`` 回跳客户端写 token。

    返回 ``(回调, state)``；登录成功后 ``state`` 带 usage 页读到的
    credits / plan / credits_detail（给额度兜底用）。
    """
    from app.config import get
    from app.net.proxy import ProxyLedger, ProxyPool, acquire_once

    state: dict = {}

    def _ui(_claimer) -> bool:
        # 客户端重置后重启需要几秒 CDP 才监听：轮询重试自动点击，
        # 旧版只试一次→客户端还没起来就放弃→空等人工点击超时
        clicked = False
        dl = time.time() + 45
        while time.time() < dl and not clicked:
            clicked = auto_click_client_login(timeout=6)
            if not clicked:
                time.sleep(2)
        if clicked:
            log.info("客户端登录已自动点击，等链接进剪贴板/历史")
        else:
            print("请在弹出的 Qoder 客户端窗口点一次「登录」（或点「复制登录链接」）…")
        print(f"我在等登录链接，最多 {link_timeout}s（剪贴板 / 浏览器历史都会看）")
        started = time.time()
        link, src = wait_link(timeout=link_timeout, since=started)
        if not link:
            log.warning("没等到登录链接")
            return False
        log.info("拿到链接（来源=%s）: %s…", src, link[:100])
        pool = ProxyPool.from_file(get(cfg, "proxy.file", "data/proxies.txt"))
        ledger = ProxyLedger(get(cfg, "proxy.state_file", "data/.proxy_state.json"))
        px = acquire_once(pool, ledger, region=region)
        out = drive_login(
            cfg, link, email, password,
            env_name=env_name or "qoder_login",
            region=region, timeout=login_timeout, proxy=px,
            artifacts_dir=artifacts_dir,
        )
        log.info("浏览器登录结果: %s", out.as_dict())
        if not out.ok:
            return False
        ledger.mark(px, env=out.env_id, note="login")
        state["credits"] = out.credits
        state["plan"] = out.plan
        state["detail"] = out.credits_detail
        if out.deep_link:
            return dispatch_deep_link(out.deep_link)
        return True

    return _ui, state
