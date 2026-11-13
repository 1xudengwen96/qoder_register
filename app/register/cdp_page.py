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
        self.mouse("mouseMoved", x, y, buttons)

    def click_xy(self, x: float, y: float, human: bool = True) -> None:
        if human:
            self.move(x - random.uniform(8, 25), y + random.uniform(-12, 12))
            time.sleep(random.uniform(0.04, 0.08))
        self.move(x, y)
        time.sleep(random.uniform(0.03, 0.07))
        self.mouse("mousePressed", x, y, buttons=1)
        time.sleep(random.uniform(0.03, 0.09))
        self.mouse("mouseReleased", x, y, buttons=0)
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
        """勾选 checkbox（React 受控组件走原生 click 才触发 onChange）。"""
        js = f"""(() => {{
          const e = document.querySelector({json.dumps(selector)});
          if (!e) return 'missing';
          if (e.checked) return 'already';
          e.click();
          return 'clicked';
        }})()"""
        out = self.eval(js)
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
        """清空输入框，走原生 setter 让 React 感知。"""
        self.eval(f"""(() => {{
          const e = document.querySelector({json.dumps(selector)});
          if (!e) return false;
          const setter = Object.getOwnPropertyDescriptor(
              window.HTMLInputElement.prototype, 'value').set;
          setter.call(e, '');
          e.dispatchEvent(new Event('input', {{bubbles: true}}));
          e.dispatchEvent(new Event('change', {{bubbles: true}}));
          return true;
        }})()""")

    def key(self, name: str) -> None:
        code, vk = SPECIAL_KEYS.get(name, (name, 0))
        self.cdp.call("Input.dispatchKeyEvent", {
            "type": "keyDown", "key": name, "code": code,
            "windowsVirtualKeyCode": vk, "nativeVirtualKeyCode": vk})
        self.cdp.call("Input.dispatchKeyEvent", {
            "type": "keyUp", "key": name, "code": code,
            "windowsVirtualKeyCode": vk, "nativeVirtualKeyCode": vk})

    def type_text(self, text: str, delay: tuple[float, float] = (0.045, 0.14)) -> None:
        """逐字符发真实键盘事件（React 只认原生事件流）。"""
        for ch in text:
            code, vk = _key_meta(ch)
            self.cdp.call("Input.dispatchKeyEvent", {
                "type": "keyDown", "text": ch, "unmodifiedText": ch,
                "key": ch, "code": code,
                "windowsVirtualKeyCode": vk, "nativeVirtualKeyCode": vk})
            self.cdp.call("Input.dispatchKeyEvent", {
                "type": "keyUp", "key": ch, "code": code,
                "windowsVirtualKeyCode": vk, "nativeVirtualKeyCode": vk})
            time.sleep(random.uniform(*delay))

    def fill(self, selector: str, text: str,
             delay: tuple[float, float] = (0.045, 0.14),
             human: bool = True) -> bool:
        """聚焦 → 清空 → 逐字符输入。"""
        if not self.sel_ready(selector, timeout=15):
            return False
        self.focus(selector)
        time.sleep(random.uniform(0.08, 0.2))
        self.clear(selector)
        time.sleep(random.uniform(0.05, 0.12))
        if human:
            self.type_text(text, delay=delay)
        else:
            self.cdp.call("Input.insertText", {"text": text})
        time.sleep(random.uniform(0.15, 0.35))
        return self.value(selector) == text

    # ---------------------------------------------------------- 收尾
    def close(self) -> None:
        self.cdp.close()

    def __enter__(self) -> "CDPPage":
        return self

    def __exit__(self, *exc) -> None:
        self.close()


# ============================================================ 新建标签页
def new_page(endpoint: str, url: str = "about:blank", timeout: float = 30.0) -> CDPPage:
    """在已启动的浏览器里新开一个标签页（不动比特浏览器自带的工作台页）。"""
    import urllib.parse
    import urllib.request

    base = cdp_raw.normalize_base(endpoint)
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
