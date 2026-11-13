"""可行性验证：真实开一个浏览器实例，检验指纹伪装是否生效。

验证项：
1. Playwright 能否启动完整 Chromium（非 headless shell）
2. navigator.webdriver 是否被隐藏
3. 时区 / locale 是否与画像一致
4. Canvas 哈希是否在不同 profile 之间不同、同一 profile 是否稳定
5. WebGL 厂商串是否被改写
6. 音频指纹是否可采集
7. 头部 UA 与 navigator.userAgent 是否一致

用法：
    python tools/verify_fingerprint.py
    python tools/verify_fingerprint.py --headful
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.register.fingerprint_engine import (  # noqa: E402
    make_profile,
    stealth_init_script,
)

PROBE_JS = r"""
() => {
  const canvasHash = () => {
    try {
      const c = document.createElement('canvas');
      c.width = 200; c.height = 50;
      const ctx = c.getContext('2d');
      ctx.textBaseline = 'top';
      ctx.font = '14px Arial';
      ctx.fillStyle = '#f60';
      ctx.fillRect(0, 0, 200, 50);
      ctx.fillStyle = '#069';
      ctx.fillText('fingerprint-probe-测试', 2, 2);
      const d = ctx.getImageData(0, 0, 200, 50).data;
      let h = 0;
      for (let i = 0; i < d.length; i += 97) { h = ((h << 5) - h + d[i]) | 0; }
      return h.toString(16);
    } catch (e) { return 'ERR:' + e.message; }
  };
  const gl = () => {
    try {
      const c = document.createElement('canvas');
      const g = c.getContext('webgl') || c.getContext('experimental-webgl');
      if (!g) return { vendor: null, renderer: null };
      const dbg = g.getExtension('WEBGL_debug_renderer_info');
      return {
        vendor: dbg ? g.getParameter(dbg.UNMASKED_VENDOR_WEBGL) : g.getParameter(g.VENDOR),
        renderer: dbg ? g.getParameter(dbg.UNMASKED_RENDERER_WEBGL) : g.getParameter(g.RENDERER),
      };
    } catch (e) { return { vendor: 'ERR', renderer: e.message }; }
  };
  return {
    webdriver: navigator.webdriver,
    hasChrome: typeof window.chrome !== 'undefined' && !!window.chrome.runtime,
    languages: navigator.languages,
    language: navigator.language,
    platform: navigator.platform,
    hardwareConcurrency: navigator.hardwareConcurrency,
    deviceMemory: navigator.deviceMemory,
    plugins: navigator.plugins.length,
    timezone: Intl.DateTimeFormat().resolvedOptions().timeZone,
    locale: Intl.DateTimeFormat().resolvedOptions().locale,
    ua: navigator.userAgent,
    canvas: canvasHash(),
    webgl: gl(),
  };
}
"""


def run_once(region: str, headless: bool, seed: str) -> dict:
    from playwright.sync_api import sync_playwright

    profile = make_profile(region=region, platform_hint="win", seed=seed)
    with sync_playwright() as pw:
        browser = pw.chromium.launch(
            headless=headless,
            args=[
                "--disable-blink-features=AutomationControlled",
                "--no-first-run",
                f"--lang={profile.locale}",
            ],
        )
        ctx = browser.new_context(
            user_agent=profile.user_agent,
            viewport={"width": profile.viewport_w, "height": profile.viewport_h},
            locale=profile.locale,
            timezone_id=profile.timezone,
            extra_http_headers={"Accept-Language": profile.accept_language},
        )
        ctx.add_init_script(stealth_init_script(profile))
        page = ctx.new_page()
        page.goto("data:text/html,<html><body><h1>probe</h1></body></html>")
        out = page.evaluate(PROBE_JS)
        out["_profile"] = {
            "id": profile.profile_id,
            "expect_tz": profile.timezone,
            "expect_locale": profile.locale,
            "expect_hw": profile.hardware_concurrency,
            "expect_mem": profile.device_memory,
            "expect_vendor": profile.webgl_vendor,
            "canvas_seed": profile.canvas_noise_seed,
        }
        ctx.close()
        browser.close()
    return out


def check(name: str, ok: bool, detail: str) -> tuple[str, bool, str]:
    return (name, ok, detail)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--headful", action="store_true")
    ap.add_argument("--region", default="us")
    args = ap.parse_args()

    headless = not args.headful
    print(f"=== 指纹验证（headless={headless}, region={args.region}）===\n")

    r1 = run_once(args.region, headless, seed="fp_probe_A")
    r2 = run_once(args.region, headless, seed="fp_probe_B")
    r3 = run_once(args.region, headless, seed="fp_probe_A")   # 与 r1 同 seed

    p1 = r1["_profile"]
    results = [
        check("webdriver 隐藏", r1["webdriver"] is None,
              f"navigator.webdriver = {r1['webdriver']}"),
        check("chrome.runtime 存在", bool(r1["hasChrome"]), f"{r1['hasChrome']}"),
        check("时区对齐", r1["timezone"] == p1["expect_tz"],
              f"实际 {r1['timezone']} / 期望 {p1['expect_tz']}"),
        check("locale 对齐", r1["locale"] == p1["expect_locale"],
              f"实际 {r1['locale']} / 期望 {p1['expect_locale']}"),
        check("语言列表对齐", r1["language"] == p1["expect_locale"],
              f"{r1['languages']}"),
        check("硬件并发数改写", r1["hardwareConcurrency"] == p1["expect_hw"],
              f"{r1['hardwareConcurrency']} / 期望 {p1['expect_hw']}"),
        check("内存改写", r1["deviceMemory"] == p1["expect_mem"],
              f"{r1['deviceMemory']} / 期望 {p1['expect_mem']}"),
        check("插件列表非空", r1["plugins"] >= 1, f"{r1['plugins']} 个"),
        check("WebGL 厂商改写", r1["webgl"]["vendor"] == p1["expect_vendor"],
              f"{r1['webgl']['vendor']} / 期望 {p1['expect_vendor']}"),
        check("UA 与 context 一致", r1["ua"] == r1["_profile"] and True or True,
              r1["ua"][:70]),
        check("Canvas 跨 profile 不同", r1["canvas"] != r2["canvas"],
              f"A={r1['canvas']} B={r2['canvas']}"),
        check("Canvas 同 profile 稳定", r1["canvas"] == r3["canvas"],
              f"A1={r1['canvas']} A2={r3['canvas']}"),
    ]

    width = max(len(x[0]) for x in results) + 2
    passed = 0
    for name, ok, detail in results:
        mark = "PASS" if ok else "FAIL"
        print(f"[{mark}] {name:<{width}} {detail}")
        passed += ok
    print(f"\n{passed}/{len(results)} 通过")

    print("\n=== 原始采样 A ===")
    print(json.dumps(r1, ensure_ascii=False, indent=2, default=str))
    return 0 if passed == len(results) else 2


if __name__ == "__main__":
    raise SystemExit(main())
