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
from pathlib import Path
from typing import Any

import requests

from app.net.proxy import _locked_file


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


# ============================================================ 常量
# 比特本地 API 默认端口（客户端设置里可改；前端控制台亦可配置）
DEFAULT_PORT = 54345


def resolve_base(cfg: dict | None = None) -> str:
    """比特 API 地址：``browser.bitbrowser.port`` 优先（前端可改），
    没配 port 时兼容旧配置 ``api_base``，再退回默认 54345。"""
    from app.config import load_config, get

    if cfg is None:
        cfg = load_config()
    try:
        port = int(get(cfg, "browser.bitbrowser.port", 0) or 0)
    except (TypeError, ValueError):
        port = 0
    if port:
        return f"http://127.0.0.1:{port}"
    return str(get(cfg, "browser.bitbrowser.api_base",
                   f"http://127.0.0.1:{DEFAULT_PORT}"))


# ------------------------------------------------------------ 全局限流闸
# 实测 0.3s 间隔即触发「请求频繁」；频繁报错会进一步触发比特客户端
# 安全机制（自动退出登录）。限流必须跨客户端实例、跨进程全局生效：
# webui 每请求新建 client、多 tab 轮询、注册进程并发——实例级节流拦不住。
GLOBAL_MIN_INTERVAL = 1.2   # 任意两次 API 调用的最小起始间隔（秒）
RATE_BACKOFF = 2.5          # 撞限流后的退避基数（秒），按重试次数递增
_ROOT = Path(__file__).resolve().parents[2]
_GATE_LOCK = _ROOT / "data" / ".bb_api_gate.lock"
_GATE_TS = _ROOT / "data" / ".bb_api_gate.ts"


def _api_gate(interval: float) -> None:
    """全局限流排队：O_EXCL 锁文件线程+进程互斥，持锁读上次调用时间戳
    → sleep 补足间隔 → 写回。持锁 sleep 即串行排队，崩溃 stale=10s 回收。"""
    interval = max(float(interval), GLOBAL_MIN_INTERVAL)
    with _locked_file(_GATE_LOCK, timeout=30, stale=10):
        try:
            last = float(_GATE_TS.read_text(encoding="utf-8").strip() or 0)
        except (OSError, ValueError):
            last = 0.0
        wait = last + interval - time.time()
        if wait > 0:
            time.sleep(wait)
        try:
            _GATE_TS.parent.mkdir(parents=True, exist_ok=True)
            _GATE_TS.write_text(str(time.time()), encoding="utf-8")
        except OSError:
            pass


# /browser/update 必须回传的代理字段（漏一个就报「请传入 proxyMethod」）
PROXY_FIELDS = (
    "proxyMethod", "proxyType", "host", "port",
    "proxyUserName", "proxyPassword", "enableSocks5Udp",
    "dynamicIpUrl", "isDynamicIpChangeIp", "ipCheckService",
    "isIpNoChange", "refreshProxyUrl",
)


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
        if not self.http:
            return ""
        if self.http.startswith(("http://", "https://")):
            return self.http
        return f"http://{self.http}"

    @property
    def cdp_ws(self) -> str:
        if not self.ws:
            return ""
        if self.ws.startswith(("ws://", "wss://")):
            return self.ws
        return f"ws://{self.ws}"

    @property
    def cdp_endpoint(self) -> str:
        endpoint = self.cdp_http or self.cdp_ws
        if not endpoint:
            raise BitBrowserError("Missing CDP endpoint")
        return endpoint


# ============================================================ 指纹构造
def build_fingerprint(
    *,
    ua: str = "",
    screen_w: int = 1920,
    screen_h: int = 1080,
    timezone: str = "",
    languages: list[str] | None = None,
    os_type: str = "Win32",
    core_version: str = "156",
    hardware_concurrency: int = 8,
    device_memory: int = 8,
    fonts: list[str] | None = None,
    canvas_noise: bool = True,
    webgl_noise: bool = True,
    audio_noise: bool = True,
    webrtc: str = "2",
    do_not_track: bool = False,
    browser_kernel: str = "chrome",
) -> dict:
    """构造 ``browserFingerPrint`` 对象（对齐 2026 版比特 API 文档）。

    实测教训（2026-11-17 acct30 复盘）：旧版字段名 ``ua``/``language``/
    ``screenWidth``/``fonts`` 已全部不被识别，比特回退随机指纹，出现
    "美国出口 IP + Europe/Helsinki + fi-FI" 的地理错位 → Baxia 直接弹
    九宫格盯死。新版字段注意两个坑：

    - ``canvas``/``webGL``/``audioContext`` 是 '0'=噪声、'1'=禁用，
      与旧语义相反，传错等于把噪声保护关掉；
    - 时区/语言/经纬度用 ``isIpCreate*`` 按**出口 IP** 自动生成，
      永远与 IP 一致，比手工指定更稳。

    ``ua`` 默认留空（比特按内核自动生成，与 coreVersion 一致——手工传
    旧版本号反而制造 UA/内核矛盾）。
    """
    fp = {
        "coreProduct": browser_kernel,
        "coreVersion": core_version,       # 比特当前内核，156=Chrome 156
        "ostype": "PC",
        "os": os_type,                     # navigator.platform 值
        # 时区/语言/经纬度随出口 IP 自动生成（防地理错位的正解）
        "isIpCreateTimeZone": True,
        "isIpCreateLanguage": True,
        "isIpCreatePosition": True,
        "position": "1",
        "webRTC": webrtc,                  # '0'替换 | '1'允许 | '2'禁用
        # 噪声类：'0'=开噪声（保护），'1'=禁用噪声（裸奔）
        "canvas": "0" if canvas_noise else "1",
        "webGL": "0" if webgl_noise else "1",
        "webGLMeta": "0",
        "audioContext": "0" if audio_noise else "1",
        "mediaDevice": "0",
        "speechVoices": "0",
        "clientRectNoiseEnabled": True,
        "hardwareConcurrency": str(hardware_concurrency),
        "deviceMemory": str(min(device_memory, 8)),  # 文档要求 ≤8
        "doNotTrack": "1" if do_not_track else "0",
        "resolutionType": "1",
        "resolution": f"{screen_w} x {screen_h}",
        "devicePixelRatio": 1,
        "fontType": "2",                   # 服务端随机字体列表
        "font": "",
        "portScanProtect": "0",            # 开端口扫描防护（防 CDP 探测）
        "deviceInfoEnabled": True,
        "computerName": "",
        "macAddr": "",
    }
    # userAgent 空串会被比特判"userAgent 数据格式错误"，
    # 不传该字段=按内核自动生成（与 coreVersion 一致）
    if ua:
        fp["userAgent"] = ua
    return fp


def build_proxy_config(
    proxy=None,
    proxy_type: str = "http",
) -> dict:
    """把代理转成比特浏览器的代理字段。

    支持三种入参：
    - ``None``                       → 直连（``proxyMethod=3``）
    - ``app.net.proxy.Proxy`` 对象   → 直接取字段
    - ``str``                        → ``host:port:user:pass`` / ``user:pass@host:port`` /
                                       ``http://user:pass@host:port`` 都认（走 parse_line）

    返回可直接 ``**`` 展开进 ``/browser/update`` 请求体的 dict。

    直连的正确组合（官方文档示例）：``proxyMethod=2`` + ``proxyType='noproxy'``
    + host 留空。**不是** proxyMethod=3——3 是"提取 IP"模式，open 时会报
    ``请填写提取URL: dynamicIpUrl``。
    """
    if not proxy:
        return {"proxyMethod": 2, "proxyType": "noproxy"}

    if isinstance(proxy, dict):
        if proxy.get("proxyMethod"):
            return dict(proxy)
        raw = proxy.get("raw") or proxy.get("url")
        if raw:
            return build_proxy_config(raw, proxy_type)
        host = str(proxy.get("host") or "")
        port = str(proxy.get("port") or "")
        if not host or not port:
            return {"proxyMethod": 3, "proxyType": "noproxy"}
        ptype = str(proxy.get("proxyType") or proxy.get("scheme") or proxy_type or "http")
        return {
            "proxyMethod": 2,
            "proxyType": ptype,
            "host": host,
            "port": port,
            "proxyUserName": str(proxy.get("proxyUserName") or proxy.get("username") or ""),
            "proxyPassword": str(proxy.get("proxyPassword") or proxy.get("password") or ""),
            "enableSocks5Udp": ptype.startswith("socks5"),
        }

    if hasattr(proxy, "host") and hasattr(proxy, "port"):
        scheme = (getattr(proxy, "scheme", "") or proxy_type or "http").lower()
        ptype = "socks5" if scheme.startswith("socks5") else (
            "https" if scheme.startswith("https") else "http")
        return {
            "proxyMethod": 2,             # 2 = 自定义代理
            "proxyType": ptype,
            "host": str(proxy.host),
            "port": str(proxy.port),
            "proxyUserName": str(getattr(proxy, "username", "") or ""),
            "proxyPassword": str(getattr(proxy, "password", "") or ""),
            "enableSocks5Udp": ptype == "socks5",
        }

    from app.net.proxy import parse_line

    px = parse_line(str(proxy))
    if px is None:
        raise ValueError(f"无法解析代理：{proxy}")
    return build_proxy_config(px, proxy_type)


# ============================================================ 客户端
class BitBrowserClient:
    """比特浏览器本地 API 客户端。

    特性：
    - 全局限流（跨实例/跨进程 ≥1.2s 间隔；实测 0.3s 触发"请求频繁"，
      频繁报错会触发客户端安全机制自动退登）
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
        _api_gate(self.min_interval)

    def call(self, path: str, body: dict | None = None,
             timeout: int | None = None, retries: int = 2) -> dict:
        if not path.startswith("/") or "://" in path:
            raise ValueError("Expected local API path")
        for attempt in range(retries + 1):
            self._throttle()
            try:
                r = requests.post(self.base + path, json=body or {},
                                  headers=self._headers(),
                                  timeout=timeout or self.timeout)
                r.raise_for_status()
                data = r.json()
            except (requests.RequestException, ValueError):
                raise BitBrowserError(f"{path}: HTTP/transport/JSON failure") from None
            if not isinstance(data, dict):
                raise BitBrowserError(f"{path}: expected response object")
            if data.get("success") is True:
                return data
            msg = str(data.get("msg") or "")
            if "请求频繁" in msg:
                if attempt < retries:
                    time.sleep(RATE_BACKOFF * (attempt + 1))
                    continue
                raise RateLimitedError("Local API rate limited")
            if "窗口被限制打开" in msg:
                raise LaunchRestrictedError(
                    "Browser launch restricted; check client permissions and limits",
                    code=data.get("code"))
            raise BitBrowserError(f"{path}: API rejected request", code=data.get("code"))
        raise BitBrowserError("Retries exhausted")

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
        """修改环境。

        实测两个强制项：
        - 必须带 ``browserFingerPrint``
        - 必须带 ``proxyMethod``（显式传或从现有配置继承），
          否则报 ``请传入 proxyMethod``，code 为 null
        """
        current = self.get_env(env_id).raw
        fp = fields.pop("browserFingerPrint", None) or current.get("browserFingerPrint") or {}
        body: dict = {"id": env_id, "browserFingerPrint": fp}
        for key in PROXY_FIELDS:
            if key in fields:
                continue
            val = current.get(key)
            if val is not None:
                body[key] = val
        body.update(fields)
        d = self.call("/browser/update", body, timeout=90).get("data") or {}
        return BitBrowserEnv.from_api(d)

    def apply_proxy(self, env_id: str, proxy) -> BitBrowserEnv:
        """把代理写进已存在的环境（注册前自动填代理的入口）。

        实测 ``/browser/update`` 必须带 ``browserFingerPrint`` 和 ``proxyMethod``，
        这里统一从详情继承指纹，只覆盖代理那几个字段。
        """
        cfg = build_proxy_config(proxy)
        fields = {k: v for k, v in cfg.items() if k in PROXY_FIELDS}
        return self.update_env(env_id, **fields)

    def delete_env(self, env_id: str) -> bool:
        try:
            self.call("/browser/delete", {"id": env_id})
            return True
        except BitBrowserError:
            return False

    # ---------------------------------------------------------- 干净启动
    def prepare_fresh_launch(self, env_id: str,
                             startup_url: str = "about:blank") -> dict:
        """把环境改成「每次启动都是干净新窗口」。三个字段缺一不可：

        - ``url``：原来可能塞了多条（``about:blank,https://qoder.com/``），
          每次启动全开一遍。固定成一条。
        - ``syncTabs``：标签同步，会把上次的标签带回来。
        - ``workbench``：非空时客户端都会打开 ``console.bitbrowser.net``
          工作台标签；置空后不再打开。

        实测：三项都设好后启动只有一个标签页。
        """
        fields = {"url": startup_url, "syncTabs": False, "workbench": ""}
        self.update_env(env_id, **fields)
        return fields

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
        d = self.call("/group/list", {"page": page, "pageSize": page_size}).get("data")
        if isinstance(d, list):
            return d
        if isinstance(d, dict):
            return d.get("list") or []
        raise BitBrowserError("Invalid groups response")

    # ---------------------------------------------------------- CDP 自检
    def probe_cdp(self, endpoint: str, timeout: int = 5) -> dict | None:
        """直接打 CDP 的 ``/json/version``，确认端口真的在监听。"""
        url = endpoint if endpoint.startswith("http") else f"http://{endpoint}"
        try:
            r = requests.get(url.rstrip("/") + "/json/version", timeout=timeout)
            r.raise_for_status()
            data = r.json()
            if isinstance(data, dict) and data.get("webSocketDebuggerUrl"):
                return data
            return None
        except (requests.RequestException, ValueError):
            return None

    # ---------------------------------------------------------- 能力自检
    def capability_report(self, allow_lifecycle: bool = False) -> dict:
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

        # Default diagnostics are read-only. Lifecycle probe must be explicit.
        if not allow_lifecycle:
            rep["lifecycle_tested"] = False
            return rep
        rep["lifecycle_tested"] = True
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
