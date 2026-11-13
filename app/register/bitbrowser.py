"""比特浏览器（BitBrowser）本地 API 完整封装。

本文档基于**对本机真实服务的接口普查**（`tools/probe_bitbrowser.py`）得出，
不是照抄文档。所有字段名与错误码均为实测结果。

服务地址：http://127.0.0.1:54345
认证：本地 config.json 里 ``enableAuth: false`` 时无需 token；
      开启认证后需在请求头带 ``Authorization``（见 ``BitBrowserClient.token``）。

--------------------------------------------------------------
一、已实测存在的接口
--------------------------------------------------------------
| 方法 | 路径                          | 作用              | 必需参数 |
|------|-------------------------------|-------------------|----------|
| POST | /health                       | 服务健康检查      | 无       |
| POST | /browser/list                 | 环境列表          | page, pageSize |
| POST | /browser/detail               | 环境详情          | id       |
| POST | /browser/update               | 创建/修改环境     | name 或 id + browserFingerPrint |
| POST | /browser/delete               | 删除环境          | id       |
| POST | /browser/open                 | 启动浏览器        | id       |
| POST | /browser/close                | 关闭浏览器        | id       |
| POST | /browser/close/all            | 关闭全部          | 无       |
| POST | /browser/pids                 | 查询运行中 PID    | ids      |
| POST | /browser/fingerprint/random   | 生成随机指纹      | browserId |
| POST | /group/list                   | 分组列表          | page, pageSize |

已确认不存在（404）：``/browser/version``、``/proxy/list``、
``/browser/proxy/check``、``/browser/update/remark``、``/browser/cookie/get``、
``/browser/tabs``、``/browser/running``、``/browser/group/list``。

--------------------------------------------------------------
二、错误码（实测）
--------------------------------------------------------------
| code  | 含义                   | 处理方式 |
|-------|------------------------|----------|
| 11018 | 窗口被限制打开         | RPA 并发额度为 0，需开通或换档次 |
| -     | 请求频繁，请调整请求频率 | 本地限流，≥1.2 秒间隔重试 |
| -     | ID不合法               | 环境 id 不存在或已删除 |
| -     | 请传入 browserFingerPrint | 建环境缺指纹对象 |
| -     | 请传入 proxyType       | 建环境缺代理类型 |

--------------------------------------------------------------
三、CDP 连接方式（重点）
--------------------------------------------------------------
``/browser/open`` 成功时返回：

    {
      "success": true,
      "data": {
        "http": "127.0.0.1:58123",     # CDP HTTP 端点（无 http:// 前缀）
        "ws":   "ws://127.0.0.1:58123/devtools/browser/xxxx",
        "pid":  12345,
        "coreVersion": "156"
      }
    }

Playwright 接管用 ``connect_over_cdp``：

    browser = playwright.chromium.connect_over_cdp(f"http://{data['http']}")
    ctx = browser.contexts[0]          # 比特浏览器已验证过的 context
    page = ctx.new_page()

注意：``data.http`` 不带协议头，必须自己拼 ``http://``；直接传会报 URL 非法。

--------------------------------------------------------------
四、实测限制
--------------------------------------------------------------
启动被拒时返回 ``code 11018 / 窗口被限制打开``，同时客户端日志出现：

    (rpa) set rpa concurrent ========>>>>>>>>  0

说明当前账号的 **RPA 并发额度为 0**。这是授权层面限制，
``queue`` / ``args`` / ``headless`` / ``ignoreDefaultUrls`` 任意组合都无法绕过。
CDP 只是传输协议，不是权限开关，因此必须先把账号额度开出来。
"""

from __future__ import annotations

import json
import socket
import time
import urllib.parse
from dataclasses import dataclass, field
from typing import Any

import requests


# ============================================================ 异常
class BitBrowserError(RuntimeError):
    """比特浏览器 API 通用错误。"""

    def __init__(self, message: str, code: int | None = None, raw: dict | None = None):
        super().__init__(message)
        self.code = code
        self.raw = raw or {}


class LaunchRestrictedError(BitBrowserError):
    """code 11018：窗口被限制打开（RPA 并发为 0）。"""


class RateLimitedError(BitBrowserError):
    """本地 API 限流。"""


# ============================================================ 数据类
@dataclass(slots=True)
class BitBrowserEnv:
    """一个浏览器环境的摘要。"""

    id: str
    name: str
    seq: int = 0
    status: int = 0              # 0 未启动 / 1 运行中
    proxy_type: str = ""
    proxy_method: int = 0        # 1 自定义 2 代理池 3 直连
    host: str = ""
    port: str = ""
    proxy_user: str = ""
    proxy_password: str = ""
    remark: str = ""
    group_ids: list = field(default_factory=list)
    raw: dict = field(default_factory=dict)

    @classmethod
    def from_api(cls, d: dict) -> "BitBrowserEnv":
        return cls(
            id=d.get("id", ""),
            name=d.get("name", ""),
            seq=d.get("seq", 0) or 0,
            status=d.get("status", 0) or 0,
            proxy_type=d.get("proxyType", "") or "",
            proxy_method=d.get("proxyMethod", 0) or 0,
            host=d.get("host", "") or "",
            port=str(d.get("port", "") or ""),
            proxy_user=d.get("proxyUserName", "") or "",
            proxy_password=d.get("proxyPassword", "") or "",
            remark=d.get("remark", "") or "",
            group_ids=d.get("groupIDs") or [],
            raw=d,
        )


@dataclass(slots=True)
class LaunchResult:
    """启动成功后的连接信息。"""

    env_id: str
    http: str                    # 补好协议的 http 端点
    ws: str
    pid: int
    core_version: str = ""
    raw: dict = field(default_factory=dict)

    @property
    def cdp_http(self) -> str:
        """Playwright connect_over_cdp 用的地址。"""
        if self.http.startswith("http"):
            return self.http
        return f"http://{self.http}"

    @property
    def cdp_ws(self) -> str:
        if self.ws.startswith("ws"):
            return self.ws
        return f"ws://{self.ws}"


# ============================================================ 指纹构造
def build_fingerprint(
    *,
    ua: str,
    screen_w: int = 1920,
    screen_h: int = 1080,
    timezone: str = "America/New_York",
    languages: list[str] | None = None,
    os_type: str = "Win32",
    core_version: str = "131",
    hardware_concurrency: int = 8,
    device_memory: int = 8,
    fonts: list[str] | None = None,
    canvas_noise: bool = True,
    webgl_noise: bool = True,
    audio_noise: bool = True,
    webrtc: str = "disabled",
    do_not_track: bool = False,
    browser_kernel: str = "chrome",
) -> dict:
    """构造 ``browserFingerPrint`` 对象。

    比特浏览器实测**必需**字段：``coreVersion``、``ostype``、``os``。
    其余字段不传会走默认值，但明确传可以保证指纹可控。

    ``canvas`` / ``webGL`` / ``audioContext`` 传 ``"1"`` 表示开启噪声（推荐），
    这是它比 JS 注入强的地方 —— 噪声在 C++ 渲染层生成，页面脚本看不到注入痕迹。
    """
    langs = languages or ["en-US", "en"]
    return {
        "coreVersion": core_version,
        "ostype": "PC",
        "os": os_type,
        "isIpv6": False,
        "isRandomFinger": False,
        "ua": ua,
        "screenWidth": screen_w,
        "screenHeight": screen_h,
        "fonts": fonts or ["Arial", "Verdana", "Segoe UI", "Tahoma"],
        "language": langs,
        "displayLanguages": ",".join(langs),
        "timeZone": timezone,
        "webRTC": webrtc,
        "canvas": "1" if canvas_noise else "0",
        "webGL": "1" if webgl_noise else "0",
        "audioContext": "1" if audio_noise else "0",
        "hardwareConcurrency": hardware_concurrency,
        "deviceMemory": device_memory,
        "doNotTrack": do_not_track,
        "position": "1",                 # 1 = 基于 IP 自动定位
        "browserKernel": browser_kernel,
        "isIpCreateTimeZone": True,
        "isIpCreateLanguage": True,
        "isIpCreatePosition": True,
    }


def build_proxy_config(
    proxy_raw: str | None,
    proxy_type: str = "http",
) -> dict:
    """把 ``http://user:pass@host:port`` 转成比特浏览器的代理字段。

    返回可直接 ``**`` 展开进 ``/browser/update`` 请求体的 dict。
    直连时用 ``proxyMethod=3, proxyType=noproxy``。
    """
    if not proxy_raw:
        return {"proxyMethod": 3, "proxyType": "noproxy"}

    raw = proxy_raw if "://" in proxy_raw else "http://" + proxy_raw
    u = urllib.parse.urlparse(raw)
    scheme = u.scheme.lower()
    if scheme.startswith("socks5"):
        ptype = "socks5"
    elif scheme.startswith("https"):
        ptype = "https"
    else:
        ptype = "http"

    return {
        "proxyMethod": 2,             # 2 = 自定义代理
        "proxyType": ptype,
        "host": u.hostname or "",
        "port": str(u.port or ""),
        "proxyUserName": urllib.parse.unquote(u.username or ""),
        "proxyPassword": urllib.parse.unquote(u.password or ""),
        "enableSocks5Udp": ptype == "socks5",
    }


# ============================================================ 客户端
class BitBrowserClient:
    """比特浏览器本地 API 客户端。

    特性：
    - 自动限流（默认 1.2 秒最小间隔，实测 0.3 秒会触发"请求频繁"）
    - 错误码分类（11018 抛 ``LaunchRestrictedError``）
    - 启动结果直接给出 Playwright 可用的 CDP 地址
    """

    def __init__(
        self,
        base: str = "http://127.0.0.1:54345",
        token: str | None = None,
        min_interval: float = 1.2,
        timeout: int = 60,
        launch_timeout: int = 120,
    ) -> None:
        self.base = base.rstrip("/")
        self.token = token
        self.min_interval = min_interval
        self.timeout = timeout
        self.launch_timeout = launch_timeout
        self._last_call = 0.0

    # ---------------------------------------------------------- 底层
    def _headers(self) -> dict:
        h = {"Content-Type": "application/json"}
        if self.token:
            h["Authorization"] = self.token
        return h

    def _throttle(self) -> None:
        gap = time.time() - self._last_call
        if gap < self.min_interval:
            time.sleep(self.min_interval - gap)
        self._last_call = time.time()

    def call(self, path: str, body: dict | None = None,
             timeout: int | None = None, retries: int = 2) -> dict:
        self._throttle()
        last_err = ""
        for attempt in range(retries + 1):
            try:
                r = requests.post(self.base + path, json=body or {},
                                  headers=self._headers(),
                                  timeout=timeout or self.timeout)
                data = r.json()
            except requests.exceptions.ConnectionError as exc:
                raise BitBrowserError(f"连不上比特浏览器本地服务 {self.base}：{exc}") from exc
            except json.JSONDecodeError:
                raise BitBrowserError(f"{path} 返回非 JSON：{r.text[:200]}")

            if data.get("success"):
                return data

            msg = data.get("msg") or ""
            if "请求频繁" in msg and attempt < retries:
                time.sleep(1.5 * (attempt + 1))
                last_err = msg
                continue
            if "窗口被限制打开" in msg:
                raise LaunchRestrictedError(
                    "窗口被限制打开（code 11018）：当前账号 RPA 并发为 0。"
                    "需在比特浏览器客户端开通 RPA/API 并发额度。",
                    code=11018, raw=data,
                )
            if "请求频繁" in msg:
                raise RateLimitedError(f"本地 API 限流：{msg}", raw=data)
            raise BitBrowserError(msg or f"{path} 调用失败", raw=data)
        raise BitBrowserError(last_err or "重试耗尽")

    # ---------------------------------------------------------- 健康
    def health(self) -> bool:
        try:
            r = requests.post(self.base + "/health", json={}, timeout=5)
            return bool(r.json().get("success"))
        except Exception:
            return False

    def is_port_open(self, host: str = "127.0.0.1") -> bool:
        u = urllib.parse.urlparse(self.base)
        port = u.port or 54345
        with socket.socket() as s:
            s.settimeout(2)
            return s.connect_ex((host, port)) == 0

    # ---------------------------------------------------------- 环境
    def list_envs(self, page: int = 0, page_size: int = 100,
                  name: str | None = None, group_id: str | None = None) -> tuple[list[BitBrowserEnv], int]:
        body: dict[str, Any] = {"page": page, "pageSize": page_size}
        if name:
            body["name"] = name
        if group_id:
            body["groupId"] = group_id
        d = self.call("/browser/list", body).get("data") or {}
        items = [BitBrowserEnv.from_api(x) for x in (d.get("list") or [])]
        return items, int(d.get("totalNum") or 0)

    def list_all_envs(self, page_size: int = 100) -> list[BitBrowserEnv]:
        all_items: list[BitBrowserEnv] = []
        page = 0
        while True:
            items, total = self.list_envs(page=page, page_size=page_size)
            all_items.extend(items)
            if not items or len(all_items) >= total:
                break
            page += 1
        return all_items

    def get_env(self, env_id: str) -> BitBrowserEnv:
        d = self.call("/browser/detail", {"id": env_id}).get("data") or {}
        return BitBrowserEnv.from_api(d)

    def create_env(
        self,
        name: str,
        fingerprint: dict,
        proxy_raw: str | None = None,
        proxy_type: str = "http",
        remark: str = "",
        group_id: str | None = None,
        extra: dict | None = None,
    ) -> BitBrowserEnv:
        """创建环境。返回带 id 的环境对象。"""
        body = {
            "name": name,
            "remark": remark,
            "browserFingerPrint": fingerprint,   # 注意大小写：FingerPrint
            **build_proxy_config(proxy_raw, proxy_type),
        }
        if group_id:
            body["groupIds"] = [group_id]
        if extra:
            body.update(extra)
        d = self.call("/browser/update", body, timeout=90).get("data") or {}
        return BitBrowserEnv.from_api(d)

    def update_env(self, env_id: str, **fields) -> BitBrowserEnv:
        """修改环境。必须带 ``browserFingerPrint``（实测强制要求）。"""
        current = self.get_env(env_id).raw
        fp = fields.pop("browserFingerPrint", None) or current.get("browserFingerPrint") or {}
        body = {"id": env_id, "browserFingerPrint": fp}
        body.update(fields)
        d = self.call("/browser/update", body, timeout=90).get("data") or {}
        return BitBrowserEnv.from_api(d)

    def delete_env(self, env_id: str) -> bool:
        try:
            self.call("/browser/delete", {"id": env_id})
            return True
        except BitBrowserError:
            return False

    # ---------------------------------------------------------- 启动
    def open_env(
        self,
        env_id: str,
        queue: bool = True,
        ignore_default_urls: bool = False,
        args: list[str] | None = None,
        headless: bool = False,
    ) -> LaunchResult:
        """启动浏览器，返回 CDP 连接信息。

        启动失败抛 ``LaunchRestrictedError``（并发为 0）或 ``BitBrowserError``。
        """
        body: dict[str, Any] = {"id": env_id, "queue": queue}
        if ignore_default_urls:
            body["ignoreDefaultUrls"] = True
        if args:
            body["args"] = args
        if headless:
            body["headless"] = True

        data = self.call("/browser/open", body, timeout=self.launch_timeout).get("data") or {}
        http = data.get("http") or ""
        ws = data.get("ws") or ""
        if not http and not ws:
            raise BitBrowserError(f"启动返回里没有 CDP 端点：{data}")
        return LaunchResult(
            env_id=env_id,
            http=http,
            ws=ws,
            pid=int(data.get("pid") or 0),
            core_version=str(data.get("coreVersion") or ""),
            raw=data,
        )

    def close_env(self, env_id: str) -> bool:
        try:
            self.call("/browser/close", {"id": env_id})
            return True
        except BitBrowserError:
            return False

    def close_all(self) -> bool:
        try:
            self.call("/browser/close/all", {})
            return True
        except BitBrowserError:
            return False

    def running_pids(self, env_ids: list[str]) -> dict[str, int]:
        d = self.call("/browser/pids", {"ids": env_ids}).get("data") or {}
        return {str(k): int(v) for k, v in d.items() if v}

    # ---------------------------------------------------------- 其它
    def random_fingerprint(self, env_id: str) -> dict:
        """让客户端生成一套随机指纹（比手搓更自然）。"""
        return self.call("/browser/fingerprint/random", {"browserId": env_id}).get("data") or {}

    def list_groups(self, page: int = 0, page_size: int = 100) -> list[dict]:
        d = self.call("/group/list", {"page": page, "pageSize": page_size}).get("data") or {}
        return d.get("list") or (d if isinstance(d, list) else [])

    # ---------------------------------------------------------- CDP 自检
    def probe_cdp(self, endpoint: str, timeout: int = 5) -> dict | None:
        """直接打 CDP 的 ``/json/version``，确认端口真的在监听。"""
        url = endpoint if endpoint.startswith("http") else f"http://{endpoint}"
        try:
            r = requests.get(url.rstrip("/") + "/json/version", timeout=timeout)
            return r.json()
        except Exception:
            return None

    # ---------------------------------------------------------- 能力自检
    def capability_report(self) -> dict:
        """一次性把所有能力探测清楚，给 CLI 用。"""
        rep: dict[str, Any] = {
            "base": self.base,
            "port_open": self.is_port_open(),
            "health": False,
            "env_total": 0,
            "can_create": False,
            "can_launch": False,
            "launch_error": "",
            "launch_code": None,
            "concurrency": None,
            "cdp_ok": False,
        }
        if not rep["port_open"]:
            rep["launch_error"] = "本地服务端口未监听，比特浏览器客户端没启动"
            return rep

        rep["health"] = self.health()
        if not rep["health"]:
            rep["launch_error"] = "/health 不通"
            return rep

        try:
            _, total = self.list_envs(page=0, page_size=1)
            rep["env_total"] = total
        except BitBrowserError as exc:
            rep["launch_error"] = f"列表失败：{exc}"

        # 用一个临时环境试启动，测完删掉
        try:
            fp = build_fingerprint(ua=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"))
            env = self.create_env(name=f"cap_probe_{int(time.time())}", fingerprint=fp)
            rep["can_create"] = bool(env.id)
            if env.id:
                try:
                    lr = self.open_env(env.id)
                    rep["can_launch"] = True
                    rep["cdp_ok"] = self.probe_cdp(lr.cdp_http) is not None
                    self.close_env(env.id)
                except LaunchRestrictedError as exc:
                    rep["launch_code"] = exc.code
                    rep["launch_error"] = str(exc)
                self.delete_env(env.id)
        except BitBrowserError as exc:
            rep["launch_error"] = rep["launch_error"] or str(exc)
        return rep
