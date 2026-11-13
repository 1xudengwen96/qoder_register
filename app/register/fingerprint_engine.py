"""指纹浏览器引擎：注册阶段的核心设施。

设计原则
--------
1. **每个账号一个独立浏览器环境**，不复用 Cookie、缓存、localStorage。
2. **指纹与代理地区强绑定**：时区、语言、WebGL 厂商必须与出口 IP 地理位置一致，
   否则 JA3 对得上也白搭（美国 IP 配 Asia/Shanghai 是最典型的机器特征）。
3. **优先使用真实指纹浏览器内核**：AdsPower / BitBrowser / Camoufox 都有本地 API，
   通过 CDP 或 HTTP 接口让它们创建并启动窗口，Playwright 用 ``connect_over_cdp`` 接管。
4. **兜底用 Playwright 自带 context + init_script 伪装**，质量低于真指纹浏览器，
   但零成本、无需第三方账号，用于跑通链路与自测。

支持的后端
----------
| backend        | 说明                                        | 需要 |
|----------------|---------------------------------------------|------|
| ``adspower``   | 本地 API ``http://127.0.0.1:50325``          | AdsPower 客户端 |
| ``bitbrowser`` | 本地 API ``http://127.0.0.1:54345``          | 比特浏览器客户端 |
| ``camoufox``   | `pip install camoufox`，Firefox 内核改造      | Python 包 |
| ``playwright`` | 原生 Chromium + 注入脚本                      | 仅 playwright |

用法
----
    engine = build_fingerprint_engine(cfg)
    session = engine.open(proxy=px, region="us", account_tag="acct_1024")
    page = session.page
    ...
    session.close()
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import random
import string
import time
import uuid
from pathlib import Path
from typing import Any

import requests


# ------------------------------------------------------------ 地区画像表
REGION_PROFILES: dict[str, dict[str, Any]] = {
    "us": {
        "timezones": ["America/New_York", "America/Chicago", "America/Denver", "America/Los_Angeles"],
        "locales": ["en-US"],
        "accept_languages": ["en-US,en;q=0.9"],
        "webgl_vendors": ["Google Inc. (NVIDIA)", "Google Inc. (Intel)", "Google Inc. (Apple)"],
        "screen": [(1920, 1080), (2560, 1440), (1536, 864), (1366, 768)],
    },
    "uk": {
        "timezones": ["Europe/London"],
        "locales": ["en-GB"],
        "accept_languages": ["en-GB,en;q=0.9"],
        "webgl_vendors": ["Google Inc. (Intel)", "Google Inc. (NVIDIA)"],
        "screen": [(1920, 1080), (2560, 1440), (1440, 900)],
    },
    "jp": {
        "timezones": ["Asia/Tokyo"],
        "locales": ["ja-JP"],
        "accept_languages": ["ja-JP,ja;q=0.9,en;q=0.8"],
        "webgl_vendors": ["Google Inc. (Intel)", "Google Inc. (NVIDIA)"],
        "screen": [(1920, 1080), (2560, 1440), (1366, 768)],
    },
    "sg": {
        "timezones": ["Asia/Singapore"],
        "locales": ["en-SG"],
        "accept_languages": ["en-SG,en;q=0.9"],
        "webgl_vendors": ["Google Inc. (Intel)", "Google Inc. (AMD)"],
        "screen": [(1920, 1080), (2560, 1440)],
    },
    "de": {
        "timezones": ["Europe/Berlin"],
        "locales": ["de-DE"],
        "accept_languages": ["de-DE,de;q=0.9,en;q=0.8"],
        "webgl_vendors": ["Google Inc. (NVIDIA)", "Google Inc. (AMD)"],
        "screen": [(1920, 1080), (2560, 1440), (1680, 1050)],
    },
    "hk": {
        "timezones": ["Asia/Hong_Kong"],
        "locales": ["zh-HK"],
        "accept_languages": ["zh-HK,zh;q=0.9,en;q=0.8"],
        "webgl_vendors": ["Google Inc. (Intel)", "Google Inc. (NVIDIA)"],
        "screen": [(1920, 1080), (2560, 1440), (1366, 768)],
    },
}

UA_TEMPLATES = {
    "win": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/{major}.0.0.0 Safari/537.36"
    ),
    "mac": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/{major}.0.0.0 Safari/537.36"
    ),
}

CHROME_MAJORS = [128, 129, 130, 131]


# ------------------------------------------------------------ 指纹画像
@dataclasses.dataclass(slots=True)
class FingerprintProfile:
    profile_id: str
    user_agent: str
    viewport_w: int
    viewport_h: int
    timezone: str
    locale: str
    accept_language: str
    platform: str                  # Win32 / MacIntel
    hardware_concurrency: int
    device_memory: int
    webgl_vendor: str
    webgl_renderer: str
    canvas_noise_seed: int
    audio_noise_seed: int
    color_depth: int
    fonts: list[str]
    do_not_track: str | None

    def as_dict(self) -> dict:
        return dataclasses.asdict(self)


def make_profile(region: str = "us", platform_hint: str = "win",
                 seed: str | None = None, deterministic: bool = True) -> FingerprintProfile:
    """生成指纹画像。

    关键：传 ``seed`` 时，**所有**指纹字段都由它派生，保证同一账号重访时
    指纹完全一致。用全局 random 会导致"同一账号两次访问指纹不同"，
    这是比 webdriver 更严重的破绽（检测器只认"设备指纹漂移"）。

    deterministic=False 时才走真随机，用于每次都要全新身份的极端场景。
    """
    if seed and deterministic:
        rng = random.Random(seed)
    else:
        rng = random

    prof = REGION_PROFILES.get(region.lower(), REGION_PROFILES["us"])
    major = rng.choice(CHROME_MAJORS)
    w, h = rng.choice(prof["screen"])
    ua = (UA_TEMPLATES["mac"] if platform_hint == "mac" else UA_TEMPLATES["win"]).format(major=major)
    plat = "MacIntel" if platform_hint == "mac" else "Win32"

    # 操作系统一致性：WebGL 厂商必须与 platform/UA 匹配
    # Windows 机器上报 "Apple M2 Pro" 是检测器一眼看穿的矛盾
    if platform_hint == "mac":
        allowed_vendors = ["Google Inc. (Apple)"]
    else:
        allowed_vendors = [v for v in prof["webgl_vendors"] if "Apple" not in v]
        if not allowed_vendors:
            allowed_vendors = ["Google Inc. (NVIDIA)", "Google Inc. (Intel)"]
    vendor = rng.choice(allowed_vendors)
    renderer = {
        "Google Inc. (NVIDIA)": "ANGLE (NVIDIA, NVIDIA GeForce RTX 4060 Direct3D11 vs_5_0 ps_5_0, D3D11)",
        "Google Inc. (Intel)": "ANGLE (Intel, Intel(R) UHD Graphics 770 Direct3D11 vs_5_0 ps_5_0, D3D11)",
        "Google Inc. (Apple)": "ANGLE (Apple, Apple M2 Pro, OpenGL 4.1)",
        "Google Inc. (AMD)": "ANGLE (AMD, AMD Radeon RX 7600 Direct3D11 vs_5_0 ps_5_0, D3D11)",
        "Google Inc. (Apple GPU)": "ANGLE (Apple, Apple M3, OpenGL 4.1)",
    }.get(vendor, "ANGLE (Intel, Intel(R) UHD Graphics 630 Direct3D11 vs_5_0 ps_5_0, D3D11)")

    # 噪声 seed 必须由 seed 派生，不能独立随机
    if seed:
        base = int(hashlib.sha256(seed.encode()).hexdigest()[:8], 16)
        canvas_seed = base % (2**31 - 1) or 1
        audio_seed = (base // 7) % (2**31 - 1) or 1
    else:
        canvas_seed = rng.randint(1, 2**31 - 1)
        audio_seed = rng.randint(1, 2**31 - 1)

    fonts_pool = ["Arial", "Helvetica", "Times New Roman", "Verdana", "Georgia",
                  "Segoe UI", "Calibri", "Cambria", "Tahoma", "Trebuchet MS"]
    k = rng.randint(6, 10)

    return FingerprintProfile(
        profile_id=seed or f"fp_{uuid.uuid4().hex[:12]}",
        user_agent=ua,
        viewport_w=w,
        viewport_h=h,
        timezone=rng.choice(prof["timezones"]),
        locale=rng.choice(prof["locales"]),
        accept_language=rng.choice(prof["accept_languages"]),
        platform=plat,
        hardware_concurrency=rng.choice([4, 8, 12, 16]),
        device_memory=rng.choice([8, 16, 32]),
        webgl_vendor=vendor,
        webgl_renderer=renderer,
        canvas_noise_seed=canvas_seed,
        audio_noise_seed=audio_seed,
        color_depth=24,
        fonts=rng.sample(fonts_pool, k=k),
        do_not_track=rng.choice([None, None, "1"]),
    )


# ------------------------------------------------------------ 注入脚本
def stealth_init_script(profile: FingerprintProfile) -> str:
    """在所有页面脚本之前执行的伪装脚本。

    覆盖点：webdriver 标记、chrome.runtime、语言、平台、硬件并发、内存、
    WebGL 厂商字符串、Canvas/Audio 噪声、权限查询、插件列表、字体。
    注意：这是"低级"方案，多次采样可能露馅；要稳就用真指纹浏览器后端。
    """
    return f"""
(() => {{
  const seed = {profile.canvas_noise_seed};

  Object.defineProperty(navigator, 'webdriver', {{ get: () => undefined }});
  Object.defineProperty(navigator, 'language', {{ get: () => '{profile.locale}' }});
  Object.defineProperty(navigator, 'languages', {{ get: () => ['{profile.locale}', 'en'] }});
  Object.defineProperty(navigator, 'platform', {{ get: () => '{profile.platform}' }});
  Object.defineProperty(navigator, 'hardwareConcurrency', {{ get: () => {profile.hardware_concurrency} }});
  Object.defineProperty(navigator, 'deviceMemory', {{ get: () => {profile.device_memory} }});
  Object.defineProperty(navigator, 'doNotTrack', {{ get: () => {json.dumps(profile.do_not_track)} }});

  if (!window.chrome) {{
    window.chrome = {{ runtime: {{}}, loadTimes: function() {{}}, csi: function() {{}} }};
  }}

  const patchPlugins = () => {{
    const arr = [
      {{ name: 'PDF Viewer', filename: 'internal-pdf-viewer', description: 'Portable Document Format' }},
      {{ name: 'Chrome PDF Viewer', filename: 'internal-pdf-viewer', description: 'Portable Document Format' }},
      {{ name: 'Chromium PDF Viewer', filename: 'internal-pdf-viewer', description: 'Portable Document Format' }},
    ];
    Object.defineProperty(navigator, 'plugins', {{ get: () => arr }});
    Object.defineProperty(navigator, 'mimeTypes', {{ get: () => [{{ type: 'application/pdf' }}] }});
  }};
  patchPlugins();

  // WebGL 厂商与渲染器
  const patchGL = (proto) => {{
    if (!proto) return;
    const orig = proto.getParameter;
    proto.getParameter = function (p) {{
      if (p === 37445) return '{profile.webgl_vendor}';
      if (p === 37446) return '{profile.webgl_renderer}';
      return orig.call(this, p);
    }};
  }};
  patchGL(window.WebGLRenderingContext && WebGLRenderingContext.prototype);
  patchGL(window.WebGL2RenderingContext && WebGL2RenderingContext.prototype);

  // Canvas 噪声：必须只依赖 (x, y, seed)，不能依赖数组下标 i
  // —— 同一像素在不同截图里 i 可能不同，用 i 会导致同设备两次采样不一致，这是最典型的破绽
  const noise = (x, y, s) => {{
    let n = (Math.imul(x + 0x9e37, 374761393) + Math.imul(y + 0x85eb, 668265263) + s) >>> 0;
    n = (n ^ (n >>> 13)) >>> 0;
    n = Math.imul(n, 1274126177) >>> 0;
    return ((n >>> 0) % 5) - 2;   // -2 .. +2
  }};
  const origGetImageData = CanvasRenderingContext2D.prototype.getImageData;
  CanvasRenderingContext2D.prototype.getImageData = function (x, y, w, h) {{
    const data = origGetImageData.call(this, x, y, w, h);
    for (let row = 0; row < h; row++) {{
      for (let col = 0; col < w; col++) {{
        const idx = (row * w + col) * 4;
        if (idx + 2 >= data.data.length) continue;
        // 用画布绝对坐标，同一像素永远得到同一噪声
        const d = noise(x + col, y + row, seed);
        data.data[idx] = Math.max(0, Math.min(255, data.data[idx] + d));
        data.data[idx + 1] = Math.max(0, Math.min(255, data.data[idx + 1] + d));
        data.data[idx + 2] = Math.max(0, Math.min(255, data.data[idx + 2] + d));
      }}
    }}
    return data;
  }};

  // toDataURL 也要走同样的噪声路径，否则两条接口哈希对不上
  const origToDataURL = HTMLCanvasElement.prototype.toDataURL;
  HTMLCanvasElement.prototype.toDataURL = function (...args) {{
    try {{
      const ctx = this.getContext('2d');
      if (ctx && this.width && this.height) {{
        ctx.getImageData(0, 0, this.width, this.height);  // 触发噪声写入
      }}
    }} catch (e) {{}}
    return origToDataURL.apply(this, args);
  }};

  // Audio 指纹噪声：同样只依赖频率桶下标与 seed
  const origGetFloat = AnalyserNode.prototype.getFloatFrequencyData;
  AnalyserNode.prototype.getFloatFrequencyData = function (arr) {{
    origGetFloat.call(this, arr);
    for (let i = 0; i < arr.length; i++) {{
      arr[i] = arr[i] + (noise(i, 7, {profile.audio_noise_seed}) * 0.0001);
    }}
  }};

  // 权限查询修正
  const origQuery = window.navigator.permissions && window.navigator.permissions.query;
  if (origQuery) {{
    window.navigator.permissions.query = (params) => (
      params.name === 'notifications'
        ? Promise.resolve({{ state: Notification.permission }})
        : origQuery(params)
    );
  }}

  // 时区
  const origResolved = Intl.DateTimeFormat.prototype.resolvedOptions;
  Intl.DateTimeFormat.prototype.resolvedOptions = function () {{
    const o = origResolved.call(this);
    o.timeZone = '{profile.timezone}';
    o.locale = '{profile.locale}';
    return o;
  }};
}})();
"""


# ------------------------------------------------------------ 会话对象
@dataclasses.dataclass(slots=True)
class BrowserSession:
    backend: str
    profile: FingerprintProfile
    page: Any
    context: Any
    browser: Any
    _closer: Any = None

    def close(self) -> None:
        try:
            if self._closer:
                self._closer()
        except Exception:
            pass
        for obj in (self.context, self.browser):
            try:
                if obj:
                    obj.close() if hasattr(obj, "close") else None
            except Exception:
                pass


# ------------------------------------------------------------ 引擎实现
class FingerprintEngine:
    """抽象基类。"""

    backend = "base"

    def open(self, proxy=None, region: str = "us", account_tag: str = "") -> BrowserSession:
        raise NotImplementedError

    def close_all(self) -> None:
        pass

    def health(self) -> tuple[bool, str]:
        return True, "ok"


class AdsPowerEngine(FingerprintEngine):
    """AdsPower 本地 API：创建指纹环境并启动，返回 CDP 地址。"""

    backend = "adspower"

    def __init__(self, api_base: str = "http://127.0.0.1:50325",
                 group_id: str = "0", timeout: int = 60) -> None:
        self.api_base = api_base.rstrip("/")
        self.group_id = group_id
        self.timeout = timeout
        self._ids: list[str] = []

    def health(self) -> tuple[bool, str]:
        try:
            r = requests.get(f"{self.api_base}/status", timeout=5)
            return r.status_code == 200, r.text[:120]
        except Exception as exc:
            return False, f"AdsPower 未连上：{exc}"

    def _create(self, proxy, profile: FingerprintProfile, account_tag: str) -> str:
        payload = {
            "group_id": self.group_id,
            "fingerprint_config": {
                "automatic_timezone": "1",
                "language": [profile.locale],
                "ua": profile.user_agent,
                "screen_resolution": f"{profile.viewport_w}_{profile.viewport_h}",
                "hardware_concurrency": str(profile.hardware_concurrency),
                "device_memory": str(profile.device_memory),
                "webgl_image": "1",
                "webgl": "2",
                "canvas": "1",
                "audio": "1",
                "fonts": "1",
                "random_ua": "0",
            },
            "name": account_tag or profile.profile_id,
            "user_proxy_config": (
                {
                    "proxy_soft": "other",
                    "proxy_type": proxy.scheme,
                    "proxy_host": proxy.host,
                    "proxy_port": str(proxy.port),
                    "proxy_user": proxy.username or "",
                    "proxy_password": proxy.password or "",
                }
                if proxy else {"proxy_soft": "no_proxy"}
            ),
        }
        r = requests.post(f"{self.api_base}/api/v1/user/create", json=payload, timeout=30)
        data = r.json()
        if data.get("code") != 0:
            raise RuntimeError(f"AdsPower 建环境失败：{data}")
        pid = data["data"]["id"]
        self._ids.append(pid)
        return pid

    def _start(self, pid: str) -> str:
        r = requests.get(
            f"{self.api_base}/api/v1/browser/start",
            params={"user_id": pid, "open_tabs": 1}, timeout=self.timeout,
        )
        data = r.json()
        if data.get("code") != 0:
            raise RuntimeError(f"AdsPower 启动失败：{data}")
        return data["data"]["ws"]["puppeteer"]

    def open(self, proxy=None, region: str = "us", account_tag: str = "") -> BrowserSession:
        import asyncio
        from playwright.async_api import async_playwright

        profile = make_profile(region=region, seed=account_tag or None)
        pid = self._create(proxy, profile, account_tag)
        cdp = self._start(pid)

        loop = asyncio.new_event_loop()
        pw = loop.run_until_complete(async_playwright().start())
        browser = loop.run_until_complete(pw.chromium.connect_over_cdp(cdp))
        context = browser.contexts[0] if browser.contexts else loop.run_until_complete(
            browser.new_context())
        page = loop.run_until_complete(context.new_page())

        def _closer():
            try:
                loop.run_until_complete(pw.stop())
            finally:
                loop.close()

        return BrowserSession(self.backend, profile, page, context, browser, _closer)


class BitBrowserEngine(FingerprintEngine):
    """比特浏览器：走本地 API 建环境，再 ``connect_over_cdp`` 接管。

    实测流程（``tools/verify_bitbrowser_cdp.py``）：

        POST /browser/update  → data.id      建环境（必须带 browserFingerPrint）
        POST /browser/open    → data.http    启动，返回 CDP HTTP 端点（不带 http://）
        playwright.chromium.connect_over_cdp(f"http://{data.http}")

    注意：``data.http`` 没有协议头，必须自己拼 ``http://``，否则报 URL 非法。

    已知限制：启动返回 ``code 11018 窗口被限制打开`` 时，是账号 RPA 并发为 0，
    属于授权层面，参数调不动。此时抛 ``BitBrowserLaunchRestricted``，
    上层自动降级到下一个后端。
    """

    backend = "bitbrowser"

    def __init__(self, api_base: str = "http://127.0.0.1:54345",
                 token: str | None = None,
                 group_id: str | None = None,
                 reuse_env: bool = True,
                 timeout: int = 120) -> None:
        from app.register.bitbrowser import BitBrowserClient

        self.client = BitBrowserClient(api_base, token=token, launch_timeout=timeout)
        self.group_id = group_id
        self.reuse_env = reuse_env
        self.timeout = timeout
        self._sessions: dict[str, Any] = {}

    def health(self) -> tuple[bool, str]:
        if not self.client.is_port_open():
            return False, f"比特浏览器本地服务未监听（{self.client.base}）"
        if not self.client.health():
            return False, "/health 不通"
        try:
            _, total = self.client.list_envs(page=0, page_size=1)
        except Exception as exc:
            return False, f"列表失败：{exc}"
        return True, f"服务正常，已有环境 {total} 个"

    # ---------------------------------------------------------- 环境复用
    def _find_env(self, name: str) -> str | None:
        if not self.reuse_env:
            return None
        try:
            items, _ = self.client.list_envs(page=0, page_size=100, name=name)
        except Exception:
            return None
        for e in items:
            if e.name == name:
                return e.id
        return None

    def _ensure_env(self, name: str, profile: FingerprintProfile, proxy) -> str:
        from app.register.bitbrowser import build_fingerprint

        existing = self._find_env(name)
        if existing:
            try:
                self.client.update_env(
                    existing,
                    browserFingerPrint=build_fingerprint(
                        ua=profile.user_agent,
                        screen_w=profile.viewport_w,
                        screen_h=profile.viewport_h,
                        timezone=profile.timezone,
                        languages=[profile.locale, "en"],
                        os_type=profile.platform,
                        hardware_concurrency=profile.hardware_concurrency,
                        device_memory=profile.device_memory,
                        fonts=profile.fonts,
                    ),
                )
                return existing
            except Exception:
                pass

        fp = build_fingerprint(
            ua=profile.user_agent,
            screen_w=profile.viewport_w,
            screen_h=profile.viewport_h,
            timezone=profile.timezone,
            languages=[profile.locale, "en"],
            os_type=profile.platform,
            hardware_concurrency=profile.hardware_concurrency,
            device_memory=profile.device_memory,
            fonts=profile.fonts,
        )
        env = self.client.create_env(
            name=name,
            fingerprint=fp,
            proxy_raw=proxy.raw if proxy else None,
            proxy_type=getattr(proxy, "scheme", "http"),
            remark="qoder_register",
            group_id=self.group_id,
        )
        if not env.id:
            raise RuntimeError("比特浏览器建环境失败，未返回 id")
        return env.id

    # ---------------------------------------------------------- 启动
    def open(self, proxy=None, region: str = "us", account_tag: str = "") -> BrowserSession:
        import asyncio

        from app.register.bitbrowser import BitBrowserError, LaunchRestrictedError

        profile = make_profile(region=region, seed=account_tag or None)
        env_name = account_tag or profile.profile_id

        env_id = self._ensure_env(env_name, profile, proxy)
        try:
            launch = self.client.open_env(env_id)
        except LaunchRestrictedError as exc:
            raise BitBrowserLaunchRestricted(str(exc)) from exc
        except BitBrowserError as exc:
            raise RuntimeError(f"比特浏览器启动失败：{exc}") from exc

        from playwright.async_api import async_playwright

        loop = asyncio.new_event_loop()
        pw = loop.run_until_complete(async_playwright().start())
        browser = loop.run_until_complete(pw.chromium.connect_over_cdp(launch.cdp_http))
        ctxs = browser.contexts
        context = ctxs[0] if ctxs else loop.run_until_complete(browser.new_context())
        page = loop.run_until_complete(context.new_page())
        self._sessions[env_id] = (loop, pw, browser)

        def _closer():
            self.client.close_env(env_id)
            try:
                loop.run_until_complete(pw.stop())
            finally:
                loop.close()
                self._sessions.pop(env_id, None)

        return BrowserSession(self.backend, profile, page, context, browser, _closer)


class BitBrowserLaunchRestricted(RuntimeError):
    """比特浏览器 RPA 并发为 0（code 11018），需上层降级或提示开通。"""


class CamoufoxEngine(FingerprintEngine):
    """Camoufox：Firefox 内核改造，C++ 层伪造指纹。

    注意：Playwright 的 Firefox 与 Chromium 的 TLS/JA3 指纹不同，很多站点的
    风控对 Firefox 有独立指纹库，选这个后端要先测目标站点是否接受。
    """

    backend = "camoufox"

    def __init__(self, headless: bool = False, os_hint: str = "windows") -> None:
        self.headless = headless
        self.os_hint = os_hint
        self._stack: list[Any] = []

    def health(self) -> tuple[bool, str]:
        try:
            import camoufox  # noqa: F401
            return True, "camoufox 已安装"
        except Exception as exc:
            return False, f"camoufox 未安装：{exc}"

    def open(self, proxy=None, region: str = "us", account_tag: str = "") -> BrowserSession:
        import asyncio
        from camoufox.async_api import AsyncCamoufox

        profile = make_profile(region=region, platform_hint="win", seed=account_tag or None)
        loop = asyncio.new_event_loop()
        cf = AsyncCamoufox(
            headless=self.headless,
            os=[self.os_hint],
            humanize=True,
            locale=[profile.locale],
            proxy=(
                {"server": f"{proxy.scheme}://{proxy.host}:{proxy.port}",
                 "username": proxy.username, "password": proxy.password}
                if proxy else None
            ),
            geoip=bool(proxy),
            screen={"width": profile.viewport_w, "height": profile.viewport_h},
        )
        browser = loop.run_until_complete(cf.__aenter__())
        context = browser.contexts[0] if browser.contexts else loop.run_until_complete(
            browser.new_context())
        page = loop.run_until_complete(context.new_page())
        self._stack.append(cf)

        def _closer():
            try:
                loop.run_until_complete(cf.__aexit__(None, None, None))
            finally:
                loop.close()

        return BrowserSession(self.backend, profile, page, context, browser, _closer)


class PlaywrightFallbackEngine(FingerprintEngine):
    """零依赖兜底：Chromium + init_script 伪装 + 独立的 context。"""

    backend = "playwright"

    def __init__(self, headless: bool = False, storage_root: str | Path = "data/profiles") -> None:
        self.headless = headless
        self.storage_root = Path(storage_root)
        self.storage_root.mkdir(parents=True, exist_ok=True)

    def health(self) -> tuple[bool, str]:
        try:
            import playwright  # noqa: F401
            return True, "playwright 可用"
        except Exception as exc:
            return False, f"playwright 未安装：{exc}"

    def open(self, proxy=None, region: str = "us", account_tag: str = "") -> BrowserSession:
        import asyncio
        from playwright.async_api import async_playwright

        profile = make_profile(region=region, seed=account_tag or None)
        loop = asyncio.new_event_loop()
        pw = loop.run_until_complete(async_playwright().start())
        browser = loop.run_until_complete(pw.chromium.launch(
            headless=self.headless,
            proxy=proxy.playwright_proxy() if proxy else None,
            args=[
                "--disable-blink-features=AutomationControlled",
                "--no-first-run",
                "--no-default-browser-check",
                f"--lang={profile.locale}",
            ],
        ))
        ctx_kwargs: dict[str, Any] = {
            "user_agent": profile.user_agent,
            "viewport": {"width": profile.viewport_w, "height": profile.viewport_h},
            "locale": profile.locale,
            "timezone_id": profile.timezone,
            "device_scale_factor": 1,
            "is_mobile": False,
            "has_touch": False,
            "color_scheme": "light",
            "extra_http_headers": {"Accept-Language": profile.accept_language},
            "storage_state": None,
        }
        context = loop.run_until_complete(browser.new_context(**ctx_kwargs))
        loop.run_until_complete(context.add_init_script(stealth_init_script(profile)))
        page = loop.run_until_complete(context.new_page())

        def _closer():
            try:
                loop.run_until_complete(context.close())
                loop.run_until_complete(browser.close())
                loop.run_until_complete(pw.stop())
            finally:
                loop.close()

        return BrowserSession(self.backend, profile, page, context, browser, _closer)


# ------------------------------------------------------------ 多后端降级
class FallbackEngine(FingerprintEngine):
    """按顺序尝试多个后端，取第一个能开环境成功的。

    为什么必须降级：比特浏览器启动返回 ``code 11018 窗口被限制打开`` 是授权限制
    （RPA 并发为 0），参数调不动。不降级则整轮流水线直接失败；
    降级到 Playwright 后仍能跑，只是指纹质量低一档。
    """

    backend = "fallback"

    def __init__(self, engines: list[FingerprintEngine], logger=None) -> None:
        self.engines = engines
        self.log = logger
        self._last_used: str | None = None
        self._failures: dict[str, str] = {}

    def health(self) -> tuple[bool, str]:
        parts: list[str] = []
        any_ok = False
        for e in self.engines:
            try:
                ok, detail = e.health()
            except Exception as exc:
                ok, detail = False, str(exc)[:60]
            any_ok = any_ok or ok
            parts.append(f"{e.backend}={'OK' if ok else 'FAIL'}")
        return any_ok, " ".join(parts)

    def open(self, proxy=None, region: str = "us", account_tag: str = "") -> BrowserSession:
        errors: list[str] = []
        for engine in self.engines:
            try:
                s = engine.open(proxy=proxy, region=region, account_tag=account_tag)
                self._last_used = engine.backend
                if self.log:
                    self.log.info("指纹后端使用：%s", engine.backend)
                return s
            except BitBrowserLaunchRestricted as exc:
                self._failures[engine.backend] = "launch_restricted"
                errors.append(f"{engine.backend}: 并发额度为 0")
                if self.log:
                    self.log.warning("比特浏览器 RPA 并发为 0（code 11018），降级到下一后端")
            except Exception as exc:
                self._failures[engine.backend] = str(exc)[:120]
                errors.append(f"{engine.backend}: {exc}")
                if self.log:
                    self.log.warning("后端 %s 失败，降级：%s", engine.backend, exc)
        raise RuntimeError("所有指纹后端都不可用：" + " | ".join(errors))

    def capability(self) -> dict:
        return {"backends": [e.backend for e in self.engines],
                "last_used": self._last_used,
                "failures": dict(self._failures)}


# ------------------------------------------------------------ 工厂
def _make_engine(cfg, name: str) -> FingerprintEngine:
    from app.config import get

    headless = bool(get(cfg, "browser.headless", False))
    if name == "adspower":
        return AdsPowerEngine(
            api_base=get(cfg, "browser.adspower.api_base", "http://127.0.0.1:50325"),
            group_id=str(get(cfg, "browser.adspower.group_id", "0")),
        )
    if name == "bitbrowser":
        return BitBrowserEngine(
            api_base=get(cfg, "browser.bitbrowser.api_base", "http://127.0.0.1:54345"),
            token=get(cfg, "browser.bitbrowser.token", None) or None,
            group_id=get(cfg, "browser.bitbrowser.group_id", None) or None,
            reuse_env=bool(get(cfg, "browser.bitbrowser.reuse_env", True)),
        )
    if name == "camoufox":
        return CamoufoxEngine(headless=headless)
    return PlaywrightFallbackEngine(
        headless=headless,
        storage_root=get(cfg, "browser.storage_root", "data/profiles"),
    )


def build_fingerprint_engine(cfg) -> FingerprintEngine:
    """按配置组装引擎。

    ``browser.backend`` 支持三种写法：

    - 单个名字：``bitbrowser``
    - 逗号分隔降级链：``bitbrowser,adspower,playwright``
    - ``auto``：按可用性排序（比特 → AdsPower → Camoufox → Playwright）
    """
    from app.config import get

    spec = str(get(cfg, "browser.backend", "playwright") or "playwright").lower()
    if spec in {"auto", "fallback"}:
        chain = ["bitbrowser", "adspower", "camoufox", "playwright"]
    elif "," in spec:
        chain = [s.strip() for s in spec.split(",") if s.strip()]
    else:
        chain = [spec]

    engines: list[FingerprintEngine] = []
    for name in chain:
        try:
            engines.append(_make_engine(cfg, name))
        except Exception:
            continue

    if not engines:
        return PlaywrightFallbackEngine(headless=bool(get(cfg, "browser.headless", False)))
    if len(engines) == 1:
        return engines[0]
    return FallbackEngine(engines)
