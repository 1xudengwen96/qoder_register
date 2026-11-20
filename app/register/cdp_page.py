"""基于原始 CDP 的页面操作层（替代 Playwright 的 page 对象）。

为什么要有这一层
----------------
Playwright 的任何 API 都会先 spawn 一个 node 驱动进程，用管道通信。
受限沙箱里创建命名管道被拒（WinError 5），``connect_over_cdp`` 同样起不来。
本层只依赖 ``app.register.cdp_raw``（socket + json），功能覆盖注册流程需要的
全部动作：导航、等待、取元素坐标、鼠标点击/拖动、键盘输入、读文本、截图。

坐标约定
--------
CDP 的 ``Input.dispatchMouseEvent`` 用 **CSS 像素、相对视口左上角**，
与 ``getBoundingClientRect()`` 一致，所以直接取 rect 中心即可，不用换算 DPR。
"""

from __future__ import annotations

import json
import logging
import math
import random
import time
from dataclasses import dataclass

from app.register import cdp_raw


@dataclass(slots=True)
class Rect:
    x: float
    y: float
    w: float
    h: float
    visible: bool = True

    @property
    def cx(self) -> float:
        return self.x + self.w / 2

    @property
    def cy(self) -> float:
        return self.y + self.h / 2


# ============================================================ 键位表
SPECIAL_KEYS = {
    "Enter": ("Enter", 13),
    "Tab": ("Tab", 9),
    "Backspace": ("Backspace", 8),
    "Escape": ("Escape", 27),
    "Space": ("Space", 32),
    "ArrowLeft": ("ArrowLeft", 37),
    "ArrowRight": ("ArrowRight", 39),
}


def _key_meta(ch: str) -> tuple[str, int]:
    if ch.isdigit():
        return f"Digit{ch}", ord(ch)
    if ch.isalpha():
        return f"Key{ch.upper()}", ord(ch.upper())
    table = {" ": ("Space", 32), "@": ("Digit2", 50), ".": ("Period", 190),
             "_": ("Minus", 189), "-": ("Minus", 189), ",": ("Comma", 188),
             "/": ("Slash", 191), "!": ("Digit1", 49), "#": ("Digit3", 51),
             "%": ("Digit5", 53)}
    return table.get(ch, ("Unidentified", 0))


class CDPPage:
    """一条 CDP 页面连接上的常用动作。"""

    def __init__(self, endpoint: str, ws_url: str = "", timeout: float = 60.0):
        self.cdp = cdp_raw.CDP(endpoint, ws_url=ws_url, timeout=timeout)
        self.cdp.enable()
        self.default_timeout = timeout
        # OS 级真实输入器（可选）：挂上后填表/点击走 SendInput，
        # 事件流与真人硬件一致（coalesced 样本）；未挂/失败退回 CDP
        self.os_input = None

    # ---------------------------------------------------------- 基础
    def goto(self, url: str, wait_ms: int = 0) -> str:
        out = self.cdp.navigate(url, timeout=self.default_timeout)
        if wait_ms:
            time.sleep(wait_ms / 1000)
        return out

    def eval(self, expression: str, await_promise: bool = False):
        return self.cdp.evaluate(expression, await_promise=await_promise)

    def url(self) -> str:
        return self.cdp.current_url()

    def text(self) -> str:
        try:
            return str(self.eval("document.body ? document.body.innerText : ''"))
        except cdp_raw.CDPError:
            return ""

    def title(self) -> str:
        try:
            return str(self.eval("document.title"))
        except cdp_raw.CDPError:
            return ""

    def shot(self, path: str) -> str:
        return self.cdp.screenshot(path)

    # ---------------------------------------------------------- 等待
    def wait_js(self, expression: str, timeout: float = 30.0,
                poll: float = 0.25) -> bool:
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                if self.eval(expression):
                    return True
            except cdp_raw.CDPError:
                pass
            time.sleep(poll)
        return False

    def sel_ready(self, selector: str, timeout: float = 30.0,
                  visible: bool = True) -> bool:
        s = json.dumps(selector)
        cond = (f"(() => {{ const e = document.querySelector({s});"
                f" if (!e) return false;"
                + (" if (!(e.offsetWidth || e.offsetHeight || e.getClientRects().length)) return false;"
                   if visible else "")
                + " return true; })()")
        return self.wait_js(cond, timeout=timeout)

    def wait_text(self, needle: str, timeout: float = 30.0) -> bool:
        n = json.dumps(needle.lower())
        return self.wait_js(
            f"(() => ((document.body ? document.body.innerText : '')"
            f".toLowerCase().includes({n})))()", timeout=timeout)

    # ---------------------------------------------------------- 元素
    def rect(self, selector: str, scroll: bool = True) -> Rect | None:
        s = json.dumps(selector)
        js = f"""(() => {{
          const e = document.querySelector({s});
          if (!e) return null;
          { "e.scrollIntoView({block:'center', inline:'center'});" if scroll else "" }
          const r = e.getBoundingClientRect();
          return {{x: r.x, y: r.y, w: r.width, h: r.height,
                   visible: !!(e.offsetWidth || e.offsetHeight || e.getClientRects().length)}};
        }})()"""
        v = self.eval(js)
        if not v:
            return None
        return Rect(float(v["x"]), float(v["y"]), float(v["w"]),
                    float(v["h"]), bool(v.get("visible")))

    def exists(self, selector: str) -> bool:
        return bool(self.eval(f"!!document.querySelector({json.dumps(selector)})"))

    def value(self, selector: str) -> str:
        v = self.eval(
            f"(() => {{ const e = document.querySelector({json.dumps(selector)});"
            f" return e ? String(e.value ?? '') : ''; }})()")
        return str(v or "")

    def attr(self, selector: str, name: str) -> str:
        v = self.eval(
            f"(() => {{ const e = document.querySelector({json.dumps(selector)});"
            f" return e ? String(e.getAttribute({json.dumps(name)}) ?? '') : ''; }})()")
        return str(v or "")

    # ---------------------------------------------------------- 鼠标
    def mouse(self, kind: str, x: float, y: float, buttons: int = 0,
              click_count: int = 0) -> None:
        params: dict = {"type": kind, "x": float(x), "y": float(y),
                        "modifiers": 0, "pointerType": "mouse"}
        if kind in ("mousePressed", "mouseReleased", "mouseMoved"):
            params["button"] = "left" if kind != "mouseMoved" or buttons else "none"
            params["buttons"] = buttons
        if kind == "mousePressed":
            params["clickCount"] = click_count or 1
        self.cdp.call("Input.dispatchMouseEvent", params)

    def move(self, x: float, y: float, buttons: int = 0) -> None:
        # 无按键的纯移动（空闲微动/轨迹）走 OS 队列，避免 coal=1 破绽；
        # 带按键的拖动（滑块）仍走 CDP，保持已验证的求解链路不动
        if buttons == 0 and self.os_input is not None and self.os_input.ready:
            try:
                self.os_input.move(x, y)
                return
            except Exception:  # noqa: BLE001
                pass
        self.mouse("mouseMoved", x, y, buttons)

    def click_xy(self, x: float, y: float, human: bool = True) -> None:
        """点击。``human=True`` 走完整鼠标轨迹（与 drag 同级拟真）。

        背景（2026-11-17 夜，Baxia 九宫格 23 连败实测）：只有 2 个
        move 点的"瞬移式"点击会被 Baxia 服务端异步判废——格子选中态
        在 ~1s 内闪现即被撤销清空，随后无论怎么补点都只剩空选；
        而带 30 步缓动轨迹的滑块拖动一直能过。所以点击也必须带轨迹。
        """
        if human and self.os_input is not None and self.os_input.ready:
            try:
                self.os_input.click_xy(x, y)
                return
            except Exception as exc:  # noqa: BLE001
                # 退回必须可见：静默退 CDP 会让「鼠标没动就点了」
                # 的破绽惄无声息地混进流程（2026-11-19 用户观察反馈）
                logging.getLogger("qoder_register").warning(
                    "OS 点击失败退 CDP（无真实鼠标轨迹）：%s", exc)
        if not human:
            self.move(x, y)
            time.sleep(random.uniform(0.03, 0.07))
            self.mouse("mousePressed", x, y, buttons=1)
            time.sleep(random.uniform(0.03, 0.09))
            self.mouse("mouseReleased", x, y, buttons=0)
            return
        # 目标本身带一点抖动：真人不会点在几何中心上
        tx = x + random.uniform(-4.5, 4.5)
        ty = y + random.uniform(-4.5, 4.5)
        # 轨迹起点：距目标随机方向的远处一点
        ang = random.uniform(0, 2 * math.pi)
        dist = random.uniform(60, 160)
        sx = tx - dist * math.cos(ang)
        sy = ty - dist * math.sin(ang)
        steps = random.randint(24, 40)
        ease = random.uniform(1.8, 2.8)     # 每次轨迹缓动不同，避免模式指纹
        self.move(sx, sy)
        time.sleep(random.uniform(0.05, 0.15))
        for i in range(1, steps + 1):
            t = i / steps
            eased = 1 - (1 - t) ** ease                  # ease-out
            px = sx + (tx - sx) * eased + random.uniform(-1.2, 1.2)
            py = sy + (ty - sy) * eased + random.uniform(-1.2, 1.2)
            self.move(px, py)
            time.sleep(random.uniform(0.008, 0.022))
        time.sleep(random.uniform(0.06, 0.15))          # hover 停顿
        self.mouse("mousePressed", tx, ty, buttons=1)
        time.sleep(random.uniform(0.05, 0.11))
        self.mouse("mouseReleased", tx, ty, buttons=0)
        time.sleep(random.uniform(0.05, 0.12))

    def click(self, selector: str, human: bool = True,
              timeout: float = 15.0) -> bool:
        if not self.sel_ready(selector, timeout=timeout):
            return False
        r = self.rect(selector)
        if not r:
            return False
        time.sleep(0.1)
        self.click_xy(r.cx, r.cy, human=human)
        return True

    def click_text(self, text: str, tag: str = "button",
                   human: bool = True) -> bool:
        """按可见文本点元素（按钮文案是唯一稳定锚点时用）。"""
        found = self.eval(f"""(() => {{
          const nodes = Array.from(document.querySelectorAll({json.dumps(tag)}));
          const hit = nodes.find(e => (e.innerText || '').trim()
                        .toLowerCase() === {json.dumps(text.lower())});
          if (!hit) return null;
          hit.scrollIntoView({{block: 'center'}});
          const r = hit.getBoundingClientRect();
          return {{x: r.x + r.width / 2, y: r.y + r.height / 2}};
        }})()""")
        if not found:
            return False
        time.sleep(0.1)
        self.click_xy(float(found["x"]), float(found["y"]), human=human)
        return True

    def check(self, selector: str) -> bool:
        """勾选 checkbox：先真人轨迹点击（完整鼠标事件链），
        JS click 只做兑底——裸 e.click() 没有前置 mousemove/down/up，
        行为遥测里是明确的机器特征。"""
        if not self.exists(selector):
            return False
        already = self.eval(
            f"(() => {{ const e = document.querySelector({json.dumps(selector)});"
            f" return e ? !!e.checked : false; }})()")
        if already:
            return True
        self.click(selector, human=True)
        # React 受控组件状态更新有延迟：多等几轮再读，避免误判
        # 「没勾上」而补 JS click——那会双切换反而取消勾选
        for _ in range(3):
            time.sleep(random.uniform(0.25, 0.5))
            checked = self.eval(
                f"(() => {{ const e = document.querySelector({json.dumps(selector)});"
                f" return e ? !!e.checked : false; }})()")
            if checked:
                return True
        # 兑底：确实没勾上（被遮挡/坐标偏）才用 JS click
        out = self.eval(f"""(() => {{
          const e = document.querySelector({json.dumps(selector)});
          if (!e) return 'missing';
          if (e.checked) return 'already';
          e.click();
          return 'clicked';
        }})()""")
        time.sleep(random.uniform(0.25, 0.5))
        checked = self.eval(
            f"(() => {{ const e = document.querySelector({json.dumps(selector)});"
            f" return e ? !!e.checked : false; }})()")
        return bool(checked) or out == "already"

    def drag(self, x1: float, y1: float, x2: float, y2: float,
             duration: float = 0.9, steps: int = 30, human: bool = True) -> None:
        """两点间平滑拖动（带缓动），用于滑块。"""
        self.move(x1, y1)
        time.sleep(random.uniform(0.05, 0.12))
        self.mouse("mousePressed", x1, y1, buttons=1)
        time.sleep(random.uniform(0.06, 0.14))
        for i in range(1, steps + 1):
            t = i / steps
            eased = 1 - (1 - t) ** 2.2                # ease-out
            x = x1 + (x2 - x1) * eased
            y = y1 + (y2 - y1) * eased
            if human:
                x += random.uniform(-0.6, 0.6)
                y += random.uniform(-0.6, 0.6)
            self.move(x, y, buttons=1)
            time.sleep(duration / steps * random.uniform(0.75, 1.3))
        time.sleep(random.uniform(0.08, 0.2))
        self.mouse("mouseReleased", x2, y2, buttons=0)

    # ---------------------------------------------------------- 键盘
    def focus(self, selector: str) -> bool:
        out = self.eval(f"""(() => {{
          const e = document.querySelector({json.dumps(selector)});
          if (!e) return false;
          e.scrollIntoView({{block: 'center'}});
          e.focus();
          return document.activeElement === e;
        }})()""")
        return bool(out)

    def clear(self, selector: str) -> None:
        """清空输入框：真人式 Ctrl+A → Delete（真实按键事件链）。

        旧版走原生 setter + 合成 input/change：没有按键事件前置，
        且 React 受控组件对 setter 改值不触发 onChange，是双重破绽。
        仅在框非空时才需要清；OS 输入不可用时退回 CDP 真键。"""
        if not self.value(selector):
            return
        if self.os_input is not None and self.os_input.ready:
            try:
                self.os_input.clear_keys()
                if not self.value(selector):
                    return
            except Exception:  # noqa: BLE001
                pass
        # CDP 真键兑底：Ctrl+A → Delete
        self.cdp.call("Input.dispatchKeyEvent", {
            "type": "keyDown", "key": "Control", "code": "ControlLeft",
            "windowsVirtualKeyCode": 17, "nativeVirtualKeyCode": 17,
            "modifiers": 2})
        for t in ("keyDown", "keyUp"):
            self.cdp.call("Input.dispatchKeyEvent", {
                "type": t, "key": "a", "code": "KeyA",
                "windowsVirtualKeyCode": 65, "nativeVirtualKeyCode": 65,
                "modifiers": 2})
        self.cdp.call("Input.dispatchKeyEvent", {
            "type": "keyUp", "key": "Control", "code": "ControlLeft",
            "windowsVirtualKeyCode": 17, "nativeVirtualKeyCode": 17})
        time.sleep(random.uniform(0.08, 0.2))
        self.key("Backspace")

    def key(self, name: str) -> None:
        code, vk = SPECIAL_KEYS.get(name, (name, 0))
        self.cdp.call("Input.dispatchKeyEvent", {
            "type": "keyDown", "key": name, "code": code,
            "windowsVirtualKeyCode": vk, "nativeVirtualKeyCode": vk})
        self.cdp.call("Input.dispatchKeyEvent", {
            "type": "keyUp", "key": name, "code": code,
            "windowsVirtualKeyCode": vk, "nativeVirtualKeyCode": vk})

    def type_text(self, text: str, delay: tuple[float, float] = (0.09, 0.26)) -> None:
        """逐字符发真实键盘事件（React 只认原生事件流）。

        挂了 OS 输入器时走 SendInput（硬件级事件，coalesced 样本与
        真人一致）；否则用 CDP 注入。拟人节奏（2026-11-19 用户要求：
        所有填表放慢模拟人手）：
        - 键按下后停留 50~130ms 再抬起（真人有持键时长，不是瞬发）
        - 字符间隔放慢到 90~260ms 随机
        - 符号（@ . _ - 等）后略迟疑——换键位需要时间
        - 偶尔来一段「想一下」的长停顿；均匀快打是机器特征
        """
        if self.os_input is not None and self.os_input.ready:
            try:
                self.os_input.type_text(text, delay=delay)
                return
            except Exception as exc:  # noqa: BLE001
                logging.getLogger("qoder_register").warning(
                    "OS 键盘失败退 CDP 注入：%s", exc)
        for i, ch in enumerate(text):
            code, vk = _key_meta(ch)
            self.cdp.call("Input.dispatchKeyEvent", {
                "type": "keyDown", "text": ch, "unmodifiedText": ch,
                "key": ch, "code": code,
                "windowsVirtualKeyCode": vk, "nativeVirtualKeyCode": vk})
            time.sleep(random.uniform(0.05, 0.13))      # 持键时长
            self.cdp.call("Input.dispatchKeyEvent", {
                "type": "keyUp", "key": ch, "code": code,
                "windowsVirtualKeyCode": vk, "nativeVirtualKeyCode": vk})
            gap = random.uniform(*delay)
            if not ch.isalnum():
                gap += random.uniform(0.05, 0.18)       # 符号换手迟疑
            if i and random.random() < 0.07:
                gap += random.uniform(0.35, 1.0)        # 偶尔停下想一下
            time.sleep(gap)

    def fill(self, selector: str, text: str,
             delay: tuple[float, float] = (0.09, 0.26),
             human: bool = True) -> bool:
        """点框 → 清空 → 逐字符输入。

        human=True 时先用带轨迹的真人鼠标点击进框（不是 JS focus
        瞬移焦点），点完停一下「看一眼」再下手打字。
        """
        if not self.sel_ready(selector, timeout=15):
            return False
        # 新建窗口后的首次合成输入偶发被 Windows 前台机制吞掉
        # （焦点未建立→值落空）：值不符时重试一次点击+输入
        for attempt in range(2):
            if human:
                self.click(selector, human=True)
                time.sleep(random.uniform(0.1, 0.3))
            else:
                self.focus(selector)
                time.sleep(random.uniform(0.08, 0.2))
            self.clear(selector)
            time.sleep(random.uniform(0.08, 0.18))
            if human:
                self.type_text(text, delay=delay)
            else:
                self.cdp.call("Input.insertText", {"text": text})
            time.sleep(random.uniform(0.15, 0.35))
            if self.value(selector) == text:
                return True
            logging.getLogger("qoder_register").warning(
                "fill %s 第%d次值不符，重试", selector, attempt + 1)
        return False

    # ---------------------------------------------------------- 收尾
    def close_tab(self) -> None:
        """关掉自己开的页签，再断连接（避免下次启动被会话恢复带出来）。"""
        try:
            self.cdp.close_target()
        except Exception:
            pass
        self.close()

    def close(self) -> None:
        self.cdp.close()

    def __enter__(self) -> "CDPPage":
        return self

    def __exit__(self, *exc) -> None:
        self.close()


# ============================================================ 新建标签页
def new_page(endpoint: str, url: str = "about:blank", timeout: float = 30.0,
             reuse_blank: bool = False) -> CDPPage:
    """在已启动的浏览器里开一个可控标签页。

    ``reuse_blank=True``：优先附着到已有的空白页（启动自带的那个），
    全程单标签——多标签会被风控看出「同一环境反复开一堆页」。
    没有空白页才新建。
    """
    import urllib.parse
    import urllib.request

    base = cdp_raw.normalize_base(endpoint)
    if reuse_blank:
        try:
            with urllib.request.urlopen(base + "/json/list",
                                        timeout=timeout) as resp:
                targets = json.loads(resp.read().decode("utf-8", "replace"))
            for t in targets:
                if t.get("type") != "page":
                    continue
                u = (t.get("url") or "").strip()
                if u in ("", "about:blank", "chrome://newtab/"):
                    ws = t.get("webSocketDebuggerUrl")
                    if ws:
                        return CDPPage(endpoint, ws_url=ws, timeout=timeout)
        except Exception:  # noqa: BLE001
            pass
    target = base + "/json/new?" + urllib.parse.urlencode({"url": url})
    req = urllib.request.Request(target, method="PUT")
    info = None
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            info = json.loads(resp.read().decode("utf-8", "replace"))
    except Exception:
        with urllib.request.urlopen(target, timeout=timeout) as resp:
            info = json.loads(resp.read().decode("utf-8", "replace"))
    ws = (info or {}).get("webSocketDebuggerUrl")
    if not ws:
        raise cdp_raw.CDPError("新标签页没有 webSocketDebuggerUrl")
    return CDPPage(endpoint, ws_url=ws, timeout=timeout)
