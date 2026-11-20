"""鼠标轨迹肉眼验证：5 倍慢动作在注册页真实移动/点击/打字。

用法（约 90 秒，期间盯着 qfresh_test1 窗口看鼠标）：
    $env:PYTHONPATH='.;.pylibs'
    python tools/mouse_demo.py

每步打印系统光标真实坐标（GetCursorPos）与窗口屏幕区域，
证明 SendInput 确实在移动物理光标，而不是页面内瞬移。
"""
from __future__ import annotations

import ctypes
import sys
import time
from ctypes import wintypes
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / ".pylibs"))

from app import secrets                              # noqa: E402
from app.config import get, load_config              # noqa: E402
from app.register import bb_cdp                      # noqa: E402
from app.register.bitbrowser import BitBrowserClient, resolve_base  # noqa: E402
from app.register.cdp_page import new_page           # noqa: E402
from app.register.os_input import OSInput            # noqa: E402
from app.register.qoder_signup_cdp import SIGNUP_URL  # noqa: E402

user32 = ctypes.windll.user32


def cursor_pos() -> tuple[int, int]:
    p = wintypes.POINT()
    user32.GetCursorPos(ctypes.byref(p))
    return p.x, p.y


def window_rect(hwnd: int) -> tuple:
    r = wintypes.RECT()
    user32.GetWindowRect(hwnd, ctypes.byref(r))
    return r.left, r.top, r.right, r.bottom


def main() -> int:
    secrets.export_env()
    cfg = load_config()
    client = BitBrowserClient(resolve_base(cfg))
    env_id = Path(ROOT / "data/.fresh_env_id").read_text(encoding="utf-8").strip()
    launch = bb_cdp.open_for_register(
        client, proxy=None, region=get(cfg, "region", "sg"),
        env_id=env_id, env_name="qfresh_test1")
    print(f"窗口就绪 {launch.env_name} cdp={launch.endpoint}", flush=True)
    page = new_page(launch.endpoint, "about:blank", reuse_blank=True)
    try:
        osi = OSInput(page, launch.pid)
        if not osi.ready:
            print("OS 输入未就绪，演示中止")
            return 1
        osi.slow_mo = 5.0     # 5 倍慢动作
        print(f"窗口屏幕区域={window_rect(osi.hwnd)}", flush=True)
        print(f"演示前光标={cursor_pos()}", flush=True)

        page.goto(SIGNUP_URL, wait_ms=3500)
        if not page.sel_ready("#basic_firstName", timeout=5):
            # 已登录态（上轮实验的号还在）：先登出再回注册页
            print("未登录态兑底：登出 + 清 cookie", flush=True)
            page.eval(
                "(async()=>{try{const m=document.querySelector("
                "'meta[name=csrf-token]');"
                "await fetch('/users/sign_out',{method:'POST',"
                "credentials:'include',headers:{'X-CSRF-Token':"
                "(m?m.content:''),'X-Requested-With':'XMLHttpRequest'}})}"
                "catch(e){}})()",
                await_promise=True)
            try:
                page.cdp.call("Network.clearBrowserCookies")
            except Exception:
                pass
            page.goto(SIGNUP_URL, wait_ms=3500)
        if not page.sel_ready("#basic_firstName", timeout=20):
            print("注册表单没出现（可能已登录态），演示中止")
            return 2

        print(">>> 慢速移向 firstName 并点击（约 8s）<<<", flush=True)
        r = page.rect("#basic_firstName")
        osi.click_xy(r.cx, r.cy)
        print(f"点击后光标={cursor_pos()}（应落在窗口区域内）", flush=True)
        time.sleep(1.5)

        print(">>> 慢速打字 Amanda（约 12s）<<<", flush=True)
        osi.type_text("Amanda")
        print(f"firstName value={page.value('#basic_firstName')}", flush=True)
        time.sleep(1.5)

        print(">>> 慢速移向 lastName 并点击（约 8s）<<<", flush=True)
        r = page.rect("#basic_lastName")
        osi.click_xy(r.cx, r.cy)
        print(f"点击后光标={cursor_pos()}", flush=True)
        time.sleep(1.5)

        print(">>> 慢速打字 Smith（约 10s）<<<", flush=True)
        osi.type_text("Smith")
        print(f"lastName value={page.value('#basic_lastName')}", flush=True)

        print(">>> 慢速移向 Continue 按钮并悬停（不点，约 8s）<<<", flush=True)
        got = page.eval("""(() => {
          const b = Array.from(document.querySelectorAll('button'))
            .find(e => /continue|继续/i.test(e.innerText || ''));
          if (!b) return null;
          b.scrollIntoView({block:'center'});
          const r = b.getBoundingClientRect();
          return {x: r.x + r.width/2, y: r.y + r.height/2};
        })()""")
        if got:
            # 只移动不点击：把光标送到按钮上悬停给用户看
            osi._ensure()
            osi._foreground()
            from app.register.os_input import _mouse_abs, MOUSEEVENTF_MOVE
            tx, ty = osi._sx(got["x"]), osi._sy(got["y"])
            sx, sy = tx - 150, ty - 90
            _mouse_abs(sx, sy, MOUSEEVENTF_MOVE)
            for i in range(1, 41):
                t = i / 40
                e = 1 - (1 - t) ** 2.2
                _mouse_abs(sx + (tx - sx) * e, sy + (ty - sy) * e,
                           MOUSEEVENTF_MOVE)
                time.sleep(0.09)
            print(f"悬停中光标={cursor_pos()}（应在 Continue 按钮上）",
                  flush=True)
            time.sleep(6)
        print("演示结束：清掉演示输入", flush=True)
        page.click("#basic_firstName", human=True)
        page.clear("#basic_firstName")
        page.click("#basic_lastName", human=True)
        page.clear("#basic_lastName")
    finally:
        try:
            page.goto("about:blank", wait_ms=200)
            page.close()
        except Exception:
            pass
        bb_cdp.close_env(client, launch.env_id)
        print("窗口已关")
    return 0


if __name__ == "__main__":
    sys.exit(main())