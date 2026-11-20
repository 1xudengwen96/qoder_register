"""OS 级真实输入（Windows SendInput）——填表反检测的治本方案。

为什么需要这一层（2026-11-19 事件取证实锤）
-------------------------------------------
CDP ``Input.dispatch*`` 注入的事件虽然 ``isTrusted=true``、事件链完整，
但 pointermove 的 ``getCoalescedEvents()`` 永远只有 1 个样本；真实硬件
鼠标一帧会产生 2~6 个聚合样本（tools/event_forensics.py 对照实验：
程序 25/25 全 1，真人 24 条里 20 条 ≥2）。Baxia 行为遥测查这一项就能
分辨程序注入——放慢打字、拟人轨迹都绕不过去。

SendInput 走系统输入队列，浏览器按真实硬件路径采样聚合，事件流与
人手完全一致。代价：输入落在**当前前台窗口**，所以每次动作前必须把
目标浏览器窗口置前；多 worker 并发时用全局锁串行化输入段。

坐标换算
--------
SendInput 绝对坐标是物理屏幕像素（0..65535 归一化）。视口→屏幕的
偏移不猜工具栏高度：先发一个 CDP mouseMoved 到已知视口点，页面监听
器读事件的 screenX/screenY，差值即偏移（DPR/工具栏/多显示器全自适应）。
"""

from __future__ import annotations

import ctypes
import json
import logging
import math
import random
import threading
import time
from ctypes import wintypes

log = logging.getLogger("os-input")
# 项目日志只给 qoder_register 树挂了 handler，独立名字会被丢弃，
# 挂进去才能看到就绪/失败状态（2026-11-19 排查时踩过）
log.propagate = False
_parent = logging.getLogger("qoder_register")
if not log.handlers:
    for _h in _parent.handlers:
        log.addHandler(_h)
log.setLevel(_parent.level or logging.INFO)

# 多 worker 同时跑时，前台窗口只有一个——OS 输入段必须互斥
_INPUT_LOCK = threading.RLock()

user32 = ctypes.windll.user32
kernel32 = ctypes.windll.kernel32

ULONG_PTR = ctypes.c_size_t
INPUT_MOUSE = 0
INPUT_KEYBOARD = 1
MOUSEEVENTF_MOVE = 0x0001
MOUSEEVENTF_LEFTDOWN = 0x0002
MOUSEEVENTF_LEFTUP = 0x0004
MOUSEEVENTF_ABSOLUTE = 0x8000
KEYEVENTF_KEYUP = 0x0002
KEYEVENTF_UNICODE = 0x0004
SW_RESTORE = 9


class MOUSEINPUT(ctypes.Structure):
    _fields_ = [("dx", wintypes.LONG), ("dy", wintypes.LONG),
                ("mouseData", wintypes.DWORD), ("dwFlags", wintypes.DWORD),
                ("time", wintypes.DWORD), ("dwExtraInfo", ULONG_PTR)]


class KEYBDINPUT(ctypes.Structure):
    _fields_ = [("wVk", wintypes.WORD), ("wScan", wintypes.WORD),
                ("dwFlags", wintypes.DWORD), ("time", wintypes.DWORD),
                ("dwExtraInfo", ULONG_PTR)]


class _INPUT_UNION(ctypes.Union):
    _fields_ = [("mi", MOUSEINPUT), ("ki", KEYBDINPUT)]


class INPUT(ctypes.Structure):
    _fields_ = [("type", wintypes.DWORD), ("u", _INPUT_UNION)]


def _send(inp: INPUT) -> None:
    if user32.SendInput(1, ctypes.byref(inp), ctypes.sizeof(INPUT)) != 1:
        raise OSError(f"SendInput 失败 err={kernel32.GetLastError()}")


def _mouse_abs(x: float, y: float, flags: int) -> None:
    sw = user32.GetSystemMetrics(0)
    sh = user32.GetSystemMetrics(1)
    nx = int(max(0, min(sw - 1, x)) * 65535 / max(1, sw - 1))
    ny = int(max(0, min(sh - 1, y)) * 65535 / max(1, sh - 1))
    _send(INPUT(type=INPUT_MOUSE, u=_INPUT_UNION(
        mi=MOUSEINPUT(dx=nx, dy=ny, dwFlags=flags | MOUSEEVENTF_ABSOLUTE))))


def _unicode_char(ch: str, up: bool = False) -> None:
    _send(INPUT(type=INPUT_KEYBOARD, u=_INPUT_UNION(
        ki=KEYBDINPUT(wVk=0, wScan=ord(ch),
                      dwFlags=KEYEVENTF_UNICODE | (KEYEVENTF_KEYUP if up else 0)))))


def _find_main_window(pid: int) -> int:
    """按进程 PID 找可见的主窗口（比特浏览器每个环境独立进程）。"""
    found: list[int] = []

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def cb(hwnd, _lp):
        if not user32.IsWindowVisible(hwnd):
            return True
        wpid = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(wpid))
        if wpid.value == pid and user32.GetWindow(hwnd, 4) == 0:  # GW_OWNER=4
            found.append(hwnd)
        return True

    user32.EnumWindows(cb, 0)
    return found[0] if found else 0


# 校准用：一次性监听器，读 CDP 探针事件的屏幕坐标
_JS_CALIB = """(() => {
  window.__osCalib = null;
  const h = (e) => {
    window.__osCalib = {sx: e.screenX, sy: e.screenY,
                        cx: e.clientX, cy: e.clientY};
    window.removeEventListener('mousemove', h, true);
  };
  window.addEventListener('mousemove', h, true);
  return true;
})()"""


class OSInput:
    """绑定一条 CDP 页面连接 + 浏览器进程 PID 的真实输入器。

    ``attach`` 失败（找不到窗口/校准不通）时 ``ready=False``，
    上层自动退回 CDP 注入，不影响流程可用性。
    """

    def __init__(self, page, pid: int):
        self.page = page
        self.pid = int(pid or 0)
        self.hwnd = 0
        self.dx = 0.0
        self.dy = 0.0
        self.ready = False
        self._n_clicks = 0
        # 慢动作演示倍率：>1 时所有动作延时放大，供肉眼验证鼠标轨迹
        self.slow_mo = 1.0
        self._attach()

    def _slp(self, lo: float, hi: float) -> None:
        time.sleep(random.uniform(lo, hi) * self.slow_mo)

    def _attach(self) -> None:
        try:
            # 物理像素坐标对齐（DPI 缩放下 screenX 与 SendInput 同空间）
            try:
                ctypes.windll.shcore.SetProcessDpiAwarenessContext(-4)
            except Exception:  # noqa: BLE001
                try:
                    user32.SetProcessDPIAware()
                except Exception:  # noqa: BLE001
                    pass
            if not self.pid:
                log.warning("OS 输入：没有浏览器 PID，退回 CDP")
                return
            self.hwnd = _find_main_window(self.pid)
            if not self.hwnd:
                log.warning("OS 输入：找不到 pid=%s 的窗口，退回 CDP", self.pid)
                return
            self._calibrate()
        except Exception as exc:  # noqa: BLE001
            log.warning("OS 输入初始化失败（退回 CDP）：%s: %s",
                        type(exc).__name__, exc)

    def _calibrate(self) -> None:
        """校准视口→屏幕偏移：CDP 探针点 (120,140)，读事件 screenX/Y。

        挂载时页面可能还在 about:blank（探针收不到就留到首次动作
        时重试，那时已在真实 https 页上）。"""
        self.page.eval(_JS_CALIB)
        self.page.mouse("mouseMoved", 120, 140)
        time.sleep(0.15)
        got = self.page.eval("JSON.stringify(window.__osCalib)")
        info = json.loads(got) if isinstance(got, str) else got
        if not info:
            raise OSError("校准探针没收到事件")
        self.dx = float(info["sx"]) - float(info["cx"])
        self.dy = float(info["sy"]) - float(info["cy"])
        self.ready = True
        log.info("OS 输入就绪 hwnd=%s 偏移=(%.0f,%.0f)",
                 self.hwnd, self.dx, self.dy)

    def _ensure(self) -> None:
        if not self.hwnd:
            raise OSError("OS 输入不可用（无窗口句柄）")
        if not self.ready:
            self._calibrate()

    # ---------------------------------------------------------- 内部
    def _foreground(self) -> None:
        if user32.GetForegroundWindow() == self.hwnd:
            return
        if user32.IsIconic(self.hwnd):
            user32.ShowWindow(self.hwnd, SW_RESTORE)
        # AttachThreadInput 绕过前台锁限制
        fg = user32.GetForegroundWindow()
        ft = user32.GetWindowThreadProcessId(fg, None)
        mt = kernel32.GetCurrentThreadId()
        attached = False
        if ft and ft != mt:
            attached = bool(user32.AttachThreadInput(mt, ft, True))
        try:
            user32.BringWindowToTop(self.hwnd)
            user32.SetForegroundWindow(self.hwnd)
        finally:
            if attached:
                user32.AttachThreadInput(mt, ft, False)
        time.sleep(random.uniform(0.12, 0.25))
        if user32.GetForegroundWindow() != self.hwnd:
            raise OSError("目标窗口置前失败（可能被其它窗口抢占）")

    def _sx(self, x: float) -> float:
        return x + self.dx

    def _sy(self, y: float) -> float:
        return y + self.dy

    # ---------------------------------------------------------- 动作
    def click_xy(self, x: float, y: float) -> None:
        """带轨迹的真人点击（OS 级）。

        轨迹拟人（2026-11-19 行为生物特征层修复）：
        - 贝塞尔弧线而非直线（人手移动带弧度）
        - ease-in-out 速度剖面（起步加速→中段快→到点减速，
          旧版 ease-out 起步即最快是机器特征）
        - 时长随距离变化（Fitts 定律意味）
        - 15% 概率过冲后小幅回修（真人瞄准常见修正亚运动）
        """
        with _INPUT_LOCK:
            self._ensure()
            self._foreground()
            tx = self._sx(x + random.uniform(-4.5, 4.5))
            ty = self._sy(y + random.uniform(-4.5, 4.5))
            ang = random.uniform(0, 2 * math.pi)
            dist = random.uniform(60, 160)
            sx = tx - dist * math.cos(ang)
            sy = ty - dist * math.sin(ang)
            # 弧线控制点：垂直方向随机偏移
            mx = (sx + tx) / 2 + random.uniform(-0.3, 0.3) * (ty - sy)
            my = (sy + ty) / 2 + random.uniform(-0.3, 0.3) * (sx - tx)
            travel = math.hypot(tx - sx, ty - sy)
            # 2026-11-19 提速：轨迹保留弧线/速度剖面特征但压缩时长
            dur = min(0.9, max(0.25, travel / random.uniform(900, 1400)))
            steps = max(18, int(dur / random.uniform(0.016, 0.028)))
            _mouse_abs(sx, sy, MOUSEEVENTF_MOVE)
            self._slp(0.05, 0.15)
            for i in range(1, steps + 1):
                t = i / steps
                e = t * t * (3 - 2 * t)      # smoothstep：两端慢中间快
                u = 1 - e
                px = u * u * sx + 2 * u * e * mx + e * e * tx
                py = u * u * sy + 2 * u * e * my + e * e * ty
                _mouse_abs(px + random.uniform(-0.8, 0.8),
                           py + random.uniform(-0.8, 0.8), MOUSEEVENTF_MOVE)
                step = dur / steps * random.uniform(0.75, 1.3)
                self._slp(step, step)
            # 偶发过冲 + 回修：冲过目标 5~14px 再小幅修正回来
            if random.random() < 0.15:
                oa = random.uniform(0, 2 * math.pi)
                od = random.uniform(5, 14)
                _mouse_abs(tx + od * math.cos(oa), ty + od * math.sin(oa),
                           MOUSEEVENTF_MOVE)
                self._slp(0.07, 0.16)
                for i in range(1, random.randint(4, 7) + 1):
                    t = i / 6
                    _mouse_abs(tx + od * math.cos(oa) * (1 - t),
                               ty + od * math.sin(oa) * (1 - t),
                               MOUSEEVENTF_MOVE)
                    self._slp(0.015, 0.035)
            self._slp(0.04, 0.1)
            # 到点短暂悬停再按下（提速后保留特征即可）
            self._slp(0.05, 0.15)
            _mouse_abs(tx, ty, MOUSEEVENTF_LEFTDOWN)
            self._slp(0.05, 0.11)
            _mouse_abs(tx, ty, MOUSEEVENTF_LEFTUP)
            self._slp(0.05, 0.12)
            self._n_clicks += 1
            if self._n_clicks == 1 or self._n_clicks % 5 == 0:
                log.info("OS 真实鼠标点击 #%d @视口(%.0f,%.0f)",
                         self._n_clicks, x, y)

    def type_text(self, text: str,
                  delay: tuple[float, float] = (0.09, 0.26)) -> None:
        """逐字符真实键盘（UNICODE 注入，任意字符可用）。
        节奏与 CDP 版一致：持键 50~130ms、符号迟疑、偶发长停顿。"""
        with _INPUT_LOCK:
            self._ensure()
            self._foreground()
            for i, ch in enumerate(text):
                _unicode_char(ch)
                self._slp(0.05, 0.13)
                _unicode_char(ch, up=True)
                # 对数正态间隔：多数键快、少数明显慢，比均匀分布更接近
                # 真人击键节奏（均匀随机是可分类的机器特征）
                gap = min(0.4, random.lognormvariate(math.log(0.09), 0.45))
                if not ch.isalnum():
                    gap += random.uniform(0.05, 0.18)
                if i and random.random() < 0.07:
                    gap += random.uniform(0.35, 1.0)
                time.sleep(gap * self.slow_mo)

    def move(self, x: float, y: float) -> None:
        """单点移动（空闲微动用）：同样走系统队列，避免 coal=1 破绽。"""
        with _INPUT_LOCK:
            self._ensure()
            self._foreground()
            _mouse_abs(self._sx(x), self._sy(y), MOUSEEVENTF_MOVE)

    def switch_away_read_mail(self, seconds: float) -> None:
        """模拟「切去邮件应用读码再切回」：开记事本置前→停留→切回浏览器。

        真人读码必然离开注册页（blur/visibilitychange），旧版全程
        焦点钉在页面上几十秒不动，是「程序化读箱」的行为铁证。
        真实窗口切换产生与真人一致的 blur/focus/visibility 事件链。
        """
        import subprocess
        with _INPUT_LOCK:
            self._ensure()
            proc = None
            try:
                proc = subprocess.Popen(["notepad.exe"])
                time.sleep(random.uniform(0.8, 1.4))
                nh = _find_main_window(proc.pid)
                if nh:
                    user32.SetForegroundWindow(nh)
                # 读码停留（此时注册页真实处于后台/失焦状态）
                time.sleep(max(3.0, seconds - 2.0))
            finally:
                self._foreground()      # 切回浏览器（真实 focus 事件）
                time.sleep(random.uniform(0.4, 0.9))
                if proc is not None:
                    try:
                        proc.terminate()
                    except Exception:  # noqa: BLE001
                        pass

    def key_enter(self) -> None:
        with _INPUT_LOCK:
            self._ensure()
            self._foreground()
            _send(INPUT(type=INPUT_KEYBOARD, u=_INPUT_UNION(
                ki=KEYBDINPUT(wVk=0x0D, wScan=0x1C))))
            time.sleep(random.uniform(0.05, 0.12))
            _send(INPUT(type=INPUT_KEYBOARD, u=_INPUT_UNION(
                ki=KEYBDINPUT(wVk=0x0D, wScan=0x1C, dwFlags=KEYEVENTF_KEYUP))))

    def _vk_key(self, vk: int, scan: int, up: bool = False) -> None:
        _send(INPUT(type=INPUT_KEYBOARD, u=_INPUT_UNION(
            ki=KEYBDINPUT(wVk=vk, wScan=scan,
                          dwFlags=KEYEVENTF_KEYUP if up else 0))))

    def clear_keys(self) -> None:
        """真人式清空：Ctrl+A 全选 → Delete。不用 JS setter 合成事件。"""
        with _INPUT_LOCK:
            self._ensure()
            self._foreground()
            self._vk_key(0x11, 0x1D)                    # Ctrl down
            time.sleep(random.uniform(0.04, 0.09))
            self._vk_key(0x41, 0x1E)                    # A down
            time.sleep(random.uniform(0.05, 0.11))
            self._vk_key(0x41, 0x1E, up=True)
            time.sleep(random.uniform(0.03, 0.08))
            self._vk_key(0x11, 0x1D, up=True)
            time.sleep(random.uniform(0.08, 0.2))
            self._vk_key(0x2E, 0x53)                    # Delete down
            time.sleep(random.uniform(0.05, 0.12))
            self._vk_key(0x2E, 0x53, up=True)
            time.sleep(random.uniform(0.1, 0.25))