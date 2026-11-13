"""零依赖 Chrome DevTools Protocol 客户端。

为什么不用 Playwright 的 CDP 连接：Playwright 任何 API 都会先 spawn 一个
node 驱动进程并通过管道通信。受限沙箱里创建命名管道会被拒（WinError 5），
``connect_over_cdp`` 一样起不来。本模块直接对 CDP 的 WebSocket 说话：
只用 ``socket`` + ``json`` + ``base64``，不依赖 websocket-client / node。

能力：
- ``http_targets(base)``      取 /json/version 与 /json/list
- ``CDP``                     一条 CDP 连接（自动 id 匹配、跳过事件）
- ``CDP.evaluate(...)``       跑 JS，支持 awaitPromise / returnByValue
- ``CDP.navigate(url)``       打开页面并等 load
- ``CDP.screenshot(path)``    整页截图
- ``CDP.click_text(...)``     按可见文本点按钮（可选，给轻量场景用）
"""

from __future__ import annotations

import base64
import itertools
import json
import os
import socket
import struct
import time
import urllib.parse
import urllib.request
from typing import Any


class CDPError(RuntimeError):
    def __init__(self, msg: str, code: int | None = None, data: Any = None):
        super().__init__(msg)
        self.code = code
        self.data = data


# ============================================================ HTTP 端点
def _http_json(url: str, timeout: float = 5.0) -> Any:
    req = urllib.request.Request(url, headers={"Host": urllib.parse.urlparse(url).netloc})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8", "replace"))


def normalize_base(endpoint: str) -> str:
    """把 ``127.0.0.1:51001`` / ``http://...`` 统一成 http 基址。"""
    if not endpoint:
        raise CDPError("空 CDP 端点")
    if endpoint.startswith("ws://") or endpoint.startswith("wss://"):
        endpoint = endpoint.replace("ws://", "http://", 1).replace("wss://", "https://", 1)
    if not endpoint.startswith("http"):
        endpoint = "http://" + endpoint
    return endpoint.rstrip("/")


def http_version(endpoint: str, timeout: float = 5.0) -> dict:
    """``GET /json/version``：确认 CDP 活着并拿浏览器版本。"""
    return _http_json(normalize_base(endpoint) + "/json/version", timeout=timeout)


def http_targets(endpoint: str, timeout: float = 5.0) -> list[dict]:
    """``GET /json/list``：拿到每个页面的独立 ws 地址。"""
    data = _http_json(normalize_base(endpoint) + "/json/list", timeout=timeout)
    return data if isinstance(data, list) else []


def pick_page_target(endpoint: str, prefer_url: str = "") -> dict:
    targets = [t for t in http_targets(endpoint) if t.get("type") == "page"]
    if not targets:
        raise CDPError("没有 page 目标（浏览器可能只有空白窗口且未建页面）")
    if prefer_url:
        for t in targets:
            if prefer_url in (t.get("url") or ""):
                return t
    return targets[0]


# ============================================================ WebSocket
class _WS:
    """最小 WebSocket 客户端（仅 ws://，消息按需整包拼接）。"""

    def __init__(self, url: str, timeout: float = 30.0):
        u = urllib.parse.urlparse(url)
        if u.scheme != "ws":
            raise CDPError(f"只支持 ws://，收到 {u.scheme}://")
        host = u.hostname or "127.0.0.1"
        port = u.port or 80
        path = u.path or "/"
        if u.query:
            path += "?" + u.query

        self.sock = socket.create_connection((host, port), timeout=timeout)
        self.sock.settimeout(timeout)
        key = base64.b64encode(os.urandom(16)).decode()
        req = (
            f"GET {path} HTTP/1.1\r\n"
            f"Host: {host}:{port}\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key}\r\n"
            "Sec-WebSocket-Version: 13\r\n\r\n"
        )
        self.sock.sendall(req.encode())
        buf = b""
        while b"\r\n\r\n" not in buf:
            chunk = self.sock.recv(4096)
            if not chunk:
                raise CDPError("WebSocket 握手连接被关闭")
            buf += chunk
        head, rest = buf.split(b"\r\n\r\n", 1)
        status_line = head.split(b"\r\n", 1)[0].decode("latin-1")
        if "101" not in status_line:
            raise CDPError(f"WebSocket 握手失败: {status_line}")
        self._buf = rest
        self._frag = b""
        self._frag_opcode = 0

    # ---------------------------------------------------------- 发送
    def send_text(self, text: str) -> None:
        payload = text.encode("utf-8")
        n = len(payload)
        header = bytearray([0x81])
        if n < 126:
            header.append(0x80 | n)
        elif n < 65536:
            header.append(0x80 | 126)
            header += struct.pack(">H", n)
        else:
            header.append(0x80 | 127)
            header += struct.pack(">Q", n)
        mask = os.urandom(4)
        header += mask
        masked = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
        self.sock.sendall(bytes(header) + masked)

    # ---------------------------------------------------------- 接收
    def _read_exact(self, n: int) -> bytes:
        while len(self._buf) < n:
            chunk = self.sock.recv(max(4096, n - len(self._buf)))
            if not chunk:
                raise CDPError("连接已关闭")
            self._buf += chunk
        out, self._buf = self._buf[:n], self._buf[n:]
        return out

    def recv_frame(self) -> tuple[int, bytes]:
        b0, b1 = self._read_exact(2)
        fin = b0 & 0x80
        opcode = b0 & 0x0F
        masked = b1 & 0x80
        n = b1 & 0x7F
        if n == 126:
            n = struct.unpack(">H", self._read_exact(2))[0]
        elif n == 127:
            n = struct.unpack(">Q", self._read_exact(8))[0]
        mask = self._read_exact(4) if masked else b""
        payload = self._read_exact(n) if n else b""
        if masked:
            payload = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
        return (opcode | (0x80 if fin else 0)), payload

    def recv_text(self) -> str:
        """取一条完整消息（处理分片与 ping/pong）。"""
        while True:
            frame, payload = self.recv_frame()
            fin = bool(frame & 0x80)
            opcode = frame & 0x0F
            if opcode == 0x8:
                raise CDPError("服务端关闭了 WebSocket")
            if opcode == 0x9:                      # ping → pong
                self._pong(payload)
                continue
            if opcode == 0xA:                      # pong
                continue
            if opcode in (0x1, 0x2):
                self._frag = payload
                self._frag_opcode = opcode
            elif opcode == 0x0:
                self._frag += payload
            if fin:
                data, self._frag = self._frag, b""
                return data.decode("utf-8", "replace")

    def _pong(self, payload: bytes) -> None:
        n = len(payload)
        header = bytearray([0x8A])
        if n < 126:
            header.append(0x80 | n)
        else:
            header.append(0x80 | 126)
            header += struct.pack(">H", n)
        mask = os.urandom(4)
        header += mask
        self.sock.sendall(bytes(header) + bytes(b ^ mask[i % 4]
                                               for i, b in enumerate(payload)))

    def close(self) -> None:
        try:
            self.sock.sendall(bytes([0x88, 0x80]) + os.urandom(4))
        except Exception:
            pass
        try:
            self.sock.close()
        except Exception:
            pass


# ============================================================ CDP 连接
class CDP:
    """一个 CDP 会话。默认连到 page 目标，可直接 Runtime.evaluate。"""

    def __init__(self, endpoint: str, ws_url: str = "", timeout: float = 60.0,
                 event_sink: list | None = None):
        if not ws_url:
            target = pick_page_target(endpoint)
            ws_url = target.get("webSocketDebuggerUrl") or ""
            if not ws_url:
                raise CDPError("目标没有 webSocketDebuggerUrl")
            self.target = target
        else:
            self.target = {}
        self.ws = _WS(ws_url, timeout=timeout)
        self._ids = itertools.count(1)
        self.events: list[dict] = event_sink if event_sink is not None else []

    # ---------------------------------------------------------- 调用
    def call(self, method: str, params: dict | None = None,
             timeout: float = 60.0) -> dict:
        mid = next(self._ids)
        self.ws.send_text(json.dumps({"id": mid, "method": method,
                                      "params": params or {}}))
        deadline = time.time() + timeout
        while time.time() < deadline:
            msg = json.loads(self.ws.recv_text())
            if msg.get("id") == mid:
                if "error" in msg:
                    err = msg["error"]
                    raise CDPError(err.get("message", "CDP error"),
                                   code=err.get("code"), data=err.get("data"))
                return msg.get("result") or {}
            if "method" in msg:
                self.events.append(msg)
        raise CDPError(f"{method} 超时 {timeout}s")

    # ---------------------------------------------------------- 常用
    def enable(self) -> None:
        for dom in ("Page", "Runtime", "Network"):
            try:
                self.call(f"{dom}.enable")
            except CDPError:
                pass

    def evaluate(self, expression: str, await_promise: bool = True,
                 timeout: float = 60.0) -> Any:
        res = self.call("Runtime.evaluate", {
            "expression": expression,
            "awaitPromise": await_promise,
            "returnByValue": True,
            "userGesture": True,
        }, timeout=timeout)
        if res.get("exceptionDetails"):
            det = res["exceptionDetails"]
            text = (det.get("exception") or {}).get("description") or det.get("text")
            raise CDPError(f"JS 异常: {text}")
        return (res.get("result") or {}).get("value")

    def navigate(self, url: str, wait_load: bool = True,
                 timeout: float = 60.0) -> str:
        res = self.call("Page.navigate", {"url": url}, timeout=timeout)
        if res.get("errorText"):
            raise CDPError(f"导航失败: {res['errorText']}")
        if wait_load:
            deadline = time.time() + timeout
            while time.time() < deadline:
                try:
                    state = self.evaluate("document.readyState")
                except CDPError:
                    state = ""
                if state in ("interactive", "complete"):
                    return self.current_url()
                time.sleep(0.25)
        return self.current_url()

    def current_url(self) -> str:
        try:
            return str(self.evaluate("location.href", await_promise=False))
        except CDPError:
            return ""

    def screenshot(self, path: str, full_page: bool = True) -> str:
        params: dict = {"format": "png"}
        if full_page:
            try:
                m = self.call("Page.getLayoutMetrics")
                size = (m.get("cssContentSize") or m.get("contentSize") or {})
                w = float(size.get("width") or 0)
                h = float(size.get("height") or 0)
                if w and h:
                    params["clip"] = {"x": 0, "y": 0, "width": w,
                                      "height": min(h, 8000), "scale": 1}
            except CDPError:
                pass
        res = self.call("Page.captureScreenshot", params)
        data = res.get("data") or ""
        if not data:
            raise CDPError("截图返回为空")
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        with open(path, "wb") as fh:
            fh.write(base64.b64decode(data))
        return path

    def set_input_files(self, selector: str, files: list[str]) -> None:
        node = self.call("Runtime.evaluate", {
            "expression": f"document.querySelector({json.dumps(selector)})",
        }).get("result", {}).get("objectId")
        if not node:
            raise CDPError(f"找不到 {selector}")
        self.call("DOM.setFileInputFiles", {"files": files, "objectId": node})

    def close(self) -> None:
        self.ws.close()

    def __enter__(self) -> "CDP":
        return self

    def __exit__(self, *exc) -> None:
        self.close()


# ============================================================ JS 工具
PROBE_JS = r"""
(async () => {
  const nav = navigator, scr = screen;
  const out = {
    webdriver: nav.webdriver, ua: nav.userAgent, platform: nav.platform,
    languages: nav.languages,
    timezone: Intl.DateTimeFormat().resolvedOptions().timeZone,
    offsetMin: new Date().getTimezoneOffset(),
    screen: `${scr.width}x${scr.height}`,
    colorDepth: scr.colorDepth, devicePixelRatio: window.devicePixelRatio,
    hardwareConcurrency: nav.hardwareConcurrency,
    deviceMemory: nav.deviceMemory === undefined ? null : nav.deviceMemory,
    plugins: nav.plugins.length,
    canvasHash: null, webglVendor: null, webglRender: null,
    ip: null, ipCountry: null, ipTz: null,
  };
  try {
    const gl = document.createElement('canvas').getContext('webgl');
    const dbg = gl.getExtension('WEBGL_debug_renderer_info');
    out.webglVendor = gl.getParameter(dbg.UNMASKED_VENDOR_WEBGL);
    out.webglRender = gl.getParameter(dbg.UNMASKED_RENDERER_WEBGL);
  } catch (e) {}
  try {
    const c = document.createElement('canvas');
    c.width = 220; c.height = 40;
    const ctx = c.getContext('2d');
    ctx.textBaseline = 'top'; ctx.font = '14px Arial';
    ctx.fillStyle = '#f60'; ctx.fillRect(0, 0, 220, 40);
    ctx.fillStyle = '#069'; ctx.fillText('qoder-canvas-probe', 2, 15);
    const d = c.toDataURL();
    let h = 0;
    for (let i = 0; i < d.length; i++) { h = ((h << 5) - h + d.charCodeAt(i)) | 0; }
    out.canvasHash = h;
  } catch (e) {}
  try {
    const ctl = new AbortController();
    const t = setTimeout(() => ctl.abort(), 15000);
    const r = await fetch('https://api.ip.sb/geoip', {signal: ctl.signal});
    clearTimeout(t);
    const j = await r.json();
    out.ip = j.ip; out.ipCountry = j.country_code; out.ipTz = j.timezone;
  } catch (e) { out.ipError = String(e).slice(0, 120); }
  return out;
})()
"""
