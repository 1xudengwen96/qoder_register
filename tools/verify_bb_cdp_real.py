"""比特浏览器 CDP 真接管验证：启动 → Playwright 接管 → 采集指纹。

这是整条链最关键的验证：比特浏览器（Chromium 156）+ 静态代理 + CDP，
Playwright 通过 connect_over_cdp 接管后，采集真实指纹看是否生效。

用法：
    python tools/verify_bb_cdp_real.py
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.register.bitbrowser import (  # noqa: E402
    BitBrowserClient,
    BitBrowserError,
    LaunchRestrictedError,
    build_fingerprint,
)

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")

PROBE = r"""
() => {
  const gl = document.createElement('canvas').getContext('webgl');
  const dbg = gl && gl.getExtension('WEBGL_debug_renderer_info');
  const c = document.createElement('canvas');
  c.width = 220; c.height = 60;
  const ctx = c.getContext('2d');
  ctx.textBaseline = 'top'; ctx.font = '15px Arial';
  ctx.fillStyle = '#f60'; ctx.fillRect(0, 0, 220, 60);
  ctx.fillStyle = '#069'; ctx.fillText('bb-cdp-probe', 3, 3);
  const d = ctx.getImageData(0, 0, 220, 60).data;
  let h = 0;
  for (let i = 0; i < d.length; i += 61) { h = ((h << 5) - h + d[i]) | 0; }
  return {
    webdriver: navigator.webdriver,
    ua: navigator.userAgent,
    tz: Intl.DateTimeFormat().resolvedOptions().timeZone,
    locale: Intl.DateTimeFormat().resolvedOptions().locale,
    platform: navigator.platform,
    hw: navigator.hardwareConcurrency,
    mem: navigator.deviceMemory,
    plugins: navigator.plugins.length,
    vendor: dbg ? gl.getParameter(dbg.UNMASKED_VENDOR_WEBGL) : null,
    renderer: dbg ? gl.getParameter(dbg.UNMASKED_RENDERER_WEBGL) : null,
    canvas: h.toString(16),
    screen: [screen.width, screen.height],
  };
}
"""


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--proxy-raw", default="http://127.0.0.1:7897")
    ap.add_argument("--keep", action="store_true", help="保留环境不删")
    args = ap.parse_args()

    c = BitBrowserClient()
    if not c.is_port_open():
        print("比特浏览器未运行")
        return 2

    u = urlparse(args.proxy_raw if "://" in args.proxy_raw
                 else "http://" + args.proxy_raw)
    proxy_fields = {
        "proxyMethod": 2,
        "proxyType": u.scheme or "http",
        "host": u.hostname or "127.0.0.1",
        "port": str(u.port or 7897),
        "proxyUserName": u.username or "",
        "proxyPassword": u.password or "",
    }

    fp = build_fingerprint(
        ua=UA, screen_w=1920, screen_h=1080,
        timezone="America/New_York", languages=["en-US", "en"],
        os_type="Win32", hardware_concurrency=8, device_memory=8,
    )

    checks: list[tuple[str, bool, str]] = []
    env_id = ""

    # ---- 1. 建环境 ----
    print("=== 1. 建环境 ===")
    try:
        d = c.call("/browser/update", {
            "name": f"cdp_real_{int(time.time())}",
            "remark": "cdp-real-test",
            "browserFingerPrint": fp,
            **proxy_fields,
        }, timeout=90).get("data") or {}
        env_id = d.get("id", "")
        checks.append(("建环境", bool(env_id), env_id[:16]))
        print(f"env_id = {env_id}")
    except BitBrowserError as exc:
        print(f"失败：{exc}")
        return 2

    # ---- 2. 启动 ----
    print("\n=== 2. 启动并拿 CDP ===")
    launch = None
    try:
        launch = c.open_env(env_id)
        checks.append(("启动窗口", True, f"pid={launch.pid}"))
        checks.append(("CDP 端点", bool(launch.cdp_http), launch.cdp_http))
        print(f"CDP  = {launch.cdp_http}")
        print(f"pid  = {launch.pid}  core = {launch.core_version}")
        time.sleep(3)
    except LaunchRestrictedError as exc:
        checks.append(("启动窗口", False, str(exc)))
        print(f"被限流：{exc}")
        c.delete_env(env_id)
        return 2
    except BitBrowserError as exc:
        checks.append(("启动窗口", False, str(exc)))
        print(f"失败：{exc}")
        c.delete_env(env_id)
        return 2

    # ---- 3. Playwright 接管 ----
    print("\n=== 3. Playwright connect_over_cdp 接管 ===")
    info: dict = {}
    try:
        from playwright.sync_api import sync_playwright

        with sync_playwright() as pw:
            browser = pw.chromium.connect_over_cdp(launch.cdp_http)
            checks.append(("连接 CDP", True, f"contexts={len(browser.contexts)}"))
            ctx = browser.contexts[0] if browser.contexts else browser.new_context()
            page = ctx.pages[0] if ctx.pages else ctx.new_page()
            page.goto("data:text/html,<html><body><h1>probe</h1></body></html>",
                      timeout=30000)
            info = page.evaluate(PROBE)
            print(json.dumps(info, ensure_ascii=False, indent=2))
    except Exception as exc:
        checks.append(("连接 CDP", False, str(exc)[:150]))
        print(f"接管失败：{exc}")

    # ---- 4. 指纹判定 ----
    if info:
        print("\n=== 4. 指纹判定 ===")
        checks.append(("webdriver 已隐藏", info.get("webdriver") is None,
                       str(info.get("webdriver"))))
        checks.append(("UA 是 Windows Chrome", "Windows NT" in info.get("ua", ""),
                       info.get("ua", "")[:60]))
        checks.append(("时区已改写", info.get("tz") == "America/New_York",
                       info.get("tz", "")))
        checks.append(("硬件并发已改写", info.get("hw") == 8, str(info.get("hw"))))
        checks.append(("内存已改写", info.get("mem") == 8, str(info.get("mem"))))
        checks.append(("WebGL 厂商可读", bool(info.get("vendor")),
                       str(info.get("vendor"))[:50]))
        checks.append(("Canvas 可采集", bool(info.get("canvas")),
                       info.get("canvas", "")))
        # 关键：比特浏览器的操作系统一致性
        plat = info.get("platform", "")
        vendor = str(info.get("vendor", ""))
        consistent = not ("Win" in plat and "Apple" in vendor)
        checks.append(("操作系统一致（Win 无 Apple GPU）", consistent,
                       f"platform={plat} vendor={vendor[:30]}"))

    # ---- 5. 清理 ----
    if env_id and not args.keep:
        print("\n=== 5. 清理 ===")
        print("close:", c.close_env(env_id))
        time.sleep(1)
        print("delete:", c.delete_env(env_id))

    # ---- 汇总 ----
    print("\n=== 结果 ===")
    passed = 0
    width = max(len(x[0]) for x in checks) + 2
    for name, ok, detail in checks:
        print(f"[{'PASS' if ok else 'FAIL'}] {name:<{width}} {str(detail)[:70]}")
        passed += ok
    print(f"\n{passed}/{len(checks)} 通过")
    return 0 if passed == len(checks) else 2


if __name__ == "__main__":
    raise SystemExit(main())
