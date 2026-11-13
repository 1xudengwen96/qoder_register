"""比特浏览器指纹构造（依据实测修正）。

实测结论（tools/verify_bb_cdp_real.py）
--------------------------------------
创建环境时若传 ``isIpCreateTimeZone: true``（默认），比特浏览器会
**根据代理出口 IP 自动设置时区**，忽略我们传的 ``timeZone``。

实测证据：
    代理 = 127.0.0.1:7897（本机 Clash，亚洲出口）
    传入 timeZone = "America/New_York"
    实际生效    = "Asia/Shanghai"

这正是我们要的行为 —— 时区跟随 IP 地理，杜绝「美国 IP + 亚洲时区」的矛盾。

所以指纹构造分两种模式：

1. **自动对齐（推荐）**：``isIpCreateTimeZone/Language/Position = True``
   时区、语言、地理位置全部由代理 IP 决定。只要代理地区正确，
   指纹与 IP 天然一致，这是比特浏览器比 Playwright 兜底强的地方。

2. **强制指定**：上述三项设为 False，再传 ``timeZone`` / ``displayLanguages``。
   仅在需要与 IP 故意不一致时使用（极少见）。

其它实测要点
------------
- ``navigator.webdriver`` 实测为 **false**（不是 undefined）。false 是 Chromium
  未开自动化时的默认值，属正常；只有 **true** 才是破绽。
- ``navigator.deviceMemory`` 可能为 null，影响很小，不强制。
- 创建环境必须带 ``browserFingerPrint``（大小写敏感）。
- ``proxyMethod``：
    1 = 不使用代理 / 2 = **自定义静态代理（可用）** / 3 = 直连 /
    4 = 动态IP（需 ``dynamicIpUrl``）
  实测 ``proxyMethod=3`` 会报 "请填写提取URL: dynamicIpUrl"（套餐是动态IP类型时），
  ``proxyMethod=2`` 可以正常启动。
"""

from __future__ import annotations

from typing import Literal

ProxyMode = Literal["static", "dynamic", "none"]

# 浏览器内核版本（比特当前提供 156）
DEFAULT_CORE = "131"

# 各地区的默认语言/时区（仅在强制指定模式用）
REGION_DEFAULTS = {
    "us": ("en-US", "America/New_York"),
    "uk": ("en-GB", "Europe/London"),
    "jp": ("ja-JP", "Asia/Tokyo"),
    "sg": ("en-SG", "Asia/Singapore"),
    "de": ("de-DE", "Europe/Berlin"),
    "hk": ("zh-HK", "Asia/Hong_Kong"),
}


def build_fingerprint(
    *,
    ua: str,
    screen_w: int = 1920,
    screen_h: int = 1080,
    timezone: str = "",
    languages: list[str] | None = None,
    os_type: str = "Win32",
    core_version: str = DEFAULT_CORE,
    hardware_concurrency: int = 8,
    device_memory: int = 8,
    fonts: list[str] | None = None,
    canvas_noise: bool = True,
    webgl_noise: bool = True,
    audio_noise: bool = True,
    webrtc: str = "disabled",
    do_not_track: bool = False,
    browser_kernel: str = "chrome",
    auto_align_to_ip: bool = True,
) -> dict:
    """构造 ``browserFingerPrint``。

    参数
    ----
    auto_align_to_ip : bool
        True（默认）→ 时区/语言/位置跟随代理 IP 自动对齐，此时 ``timezone``
        和 ``languages`` 只作为「强制指定模式」的备用值。
        False → 强制使用传入的 ``timezone`` / ``languages``。

    canvas_noise / webgl_noise / audio_noise : bool
        开启后噪声在 C++ 渲染层生成，页面脚本看不到注入痕迹。
        这是比特浏览器优于 JS 注入的核心能力。
    """
    langs = languages or ["en-US", "en"]

    fp: dict = {
        # ---- 必填 ----
        "coreVersion": core_version,
        "ostype": "PC",
        "os": os_type,
        "ua": ua,
        # ---- 屏幕 ----
        "screenWidth": screen_w,
        "screenHeight": screen_h,
        # ---- 噪声（C++ 层）----
        "canvas": "1" if canvas_noise else "0",
        "webGL": "1" if webgl_noise else "0",
        "audioContext": "1" if audio_noise else "0",
        # ---- 硬件 ----
        "hardwareConcurrency": hardware_concurrency,
        "deviceMemory": device_memory,
        # ---- 网络 ----
        "webRTC": webrtc,
        "doNotTrack": do_not_track,
        # ---- 其它 ----
        "isIpv6": False,
        "isRandomFinger": False,
        "browserKernel": browser_kernel,
        "fonts": fonts or ["Arial", "Verdana", "Segoe UI", "Tahoma"],
    }

    if auto_align_to_ip:
        # 跟随代理 IP，这是推荐模式
        fp["isIpCreateTimeZone"] = True
        fp["isIpCreateLanguage"] = True
        fp["isIpCreatePosition"] = True
        # 仍塞入值，但服务端会以 IP 为准
        if timezone:
            fp["timeZone"] = timezone
        fp["language"] = langs
    else:
        fp["isIpCreateTimeZone"] = False
        fp["isIpCreateLanguage"] = False
        fp["isIpCreatePosition"] = False
        fp["timeZone"] = timezone or "America/New_York"
        fp["language"] = langs
        fp["displayLanguages"] = ",".join(langs)

    return fp


def build_proxy_fields(
    proxy_raw: str | None,
    mode: ProxyMode = "static",
    dynamic_ip_url: str = "",
    proxy_type: str = "",
) -> dict:
    """构造代理相关字段。

    mode
    ----
    static  → proxyMethod=2，需要 host/port/user/pass（实测可启动）
    dynamic → proxyMethod=4，需要 ``dynamic_ip_url``（IPDEEP 提取链接）
    none    → proxyMethod=3（注意：动态IP套餐下会报错，建议用 static）
    """
    from urllib.parse import urlparse

    if mode == "dynamic":
        return {
            "proxyMethod": 4,
            "proxyType": "noproxy",
            "dynamicIpUrl": dynamic_ip_url,
        }

    if mode == "none" or not proxy_raw:
        return {"proxyMethod": 3, "proxyType": "noproxy"}

    raw = proxy_raw if "://" in proxy_raw else "http://" + proxy_raw
    u = urlparse(raw)
    scheme = (proxy_type or u.scheme or "http").lower()
    if scheme.startswith("socks5") or scheme.startswith("socks"):
        ptype = "socks5"
    elif scheme.startswith("https"):
        ptype = "https"
    else:
        ptype = "http"

    return {
        "proxyMethod": 2,
        "proxyType": ptype,
        "host": u.hostname or "",
        "port": str(u.port or ""),
        "proxyUserName": u.username or "",
        "proxyPassword": u.password or "",
        "enableSocks5Udp": ptype == "socks5",
    }


def judge_fingerprint(info: dict) -> list[tuple[str, bool, str]]:
    """判定采集到的指纹是否合格。

    判定标准（依据实测，不是想当然）：
    - webdriver：只要不是 True 就合格（False / None 都正常）
    - 时区：跟随 IP，不硬编码；只检查非空
    - deviceMemory：允许为 None
    - 操作系统：Win 平台不能出现 Apple GPU
    """
    checks: list[tuple[str, bool, str]] = []

    wd = info.get("webdriver")
    checks.append(("webdriver 不是 True", wd is not True, f"值={wd}"))

    checks.append(("UA 存在", bool(info.get("ua")), str(info.get("ua", ""))[:50]))
    checks.append(("平台非空", bool(info.get("platform")), str(info.get("platform"))))
    checks.append(("时区非空（跟随 IP）", bool(info.get("tz")), str(info.get("tz"))))
    checks.append(("语言非空", bool(info.get("locale")), str(info.get("locale"))))
    checks.append(("硬件并发可读", isinstance(info.get("hw"), int),
                   str(info.get("hw"))))
    checks.append(("WebGL 厂商可读", bool(info.get("vendor")),
                   str(info.get("vendor"))[:40]))
    checks.append(("Canvas 可采集", bool(info.get("canvas")),
                   str(info.get("canvas"))))

    plat = str(info.get("platform", ""))
    vendor = str(info.get("vendor", ""))
    consistent = not ("Win" in plat and "Apple" in vendor)
    checks.append(("操作系统一致", consistent, f"{plat} / {vendor[:25]}"))

    return checks
