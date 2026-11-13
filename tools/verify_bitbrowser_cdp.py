"""比特浏览器 CDP 连接验证。

做三件事：
1. 全量 API 能力普查（能不能建环境、能不能启动、启动报什么错）
2. 拿真实环境试启动，检查 CDP 端点
3. 如果能启动，用 Playwright ``connect_over_cdp`` 真接管一次，验证指纹是否生效

用法：
    python tools/verify_bitbrowser_cdp.py
    python tools/verify_bitbrowser_cdp.py --try-cdp     # 尝试真接管
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.register.bitbrowser import (  # noqa: E402
    BitBrowserClient,
    BitBrowserError,
    LaunchRestrictedError,
    build_fingerprint,
    build_proxy_config,
)

PROBE_JS = r"""
() => {
  const gl = document.createElement('canvas').getContext('webgl');
  const dbg = gl && gl.getExtension('WEBGL_debug_renderer_info');
  const c = document.createElement('canvas');
  c.width = 200; c.height = 50;
  const ctx = c.getContext('2d');
  ctx.textBaseline = 'top'; ctx.font = '14px Arial';
  ctx.fillStyle = '#f60'; ctx.fillRect(0, 0, 200, 50);
  ctx.fillStyle = '#069'; ctx.fillText('cdp-probe', 2, 2);
  const d = ctx.getImageData(0, 0, 200, 50).data;
  let h = 0;
  for (let i = 0; i < d.length; i += 97) { h = ((h << 5) - h + d[i]) | 0; }
  return {
    webdriver: navigator.webdriver,
    ua: navigator.userAgent,
    tz: Intl.DateTimeFormat().resolvedOptions().timeZone,
    locale: Intl.DateTimeFormat().resolvedOptions().locale,
    platform: navigator.platform,
    hw: navigator.hardwareConcurrency,
    mem: navigator.deviceMemory,
    vendor: dbg ? gl.getParameter(dbg.UNMASKED_VENDOR_WEBGL) : null,
    renderer: dbg ? gl.getParameter(dbg.UNMASKED_RENDERER_WEBGL) : null,
    canvas: h.toString(16),
  };
}
"""


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="http://127.0.0.1:54345")
    ap.add_argument("--try-cdp", action="store_true", help="尝试 Playwright 真接管")
    ap.add_argument("--keep", action="store_true", help="保留测试环境不删")
    args = ap.parse_args()

    c = BitBrowserClient(args.base)
    checks: list[tuple[str, bool, str]] = []

    # ---------- 1. 服务可用 ----------
    print("=== 1. 本地服务 ===")
    port_ok = c.is_port_open()
    checks.append(("端口 54345 监听", port_ok, args.base))
    if not port_ok:
        print("端口没开，比特浏览器客户端没启动")
        return 2
    health = c.health()
    checks.append(("健康检查", health, c.base + "/health"))
    print(f"health={health}")

    # ---------- 2. 环境列表 ----------
    print("\n=== 2. 环境列表 ===")
    try:
        envs, total = c.list_envs(page=0, page_size=5)
        checks.append(("能列环境", True, f"总量 {total}"))
        print(f"totalNum={total}")
        for e in envs[:5]:
            print(f"  id={e.id[:12]}… name={e.name} status={e.status} "
                  f"proxyMethod={e.proxy_method} proxyType={e.proxy_type}")
    except BitBrowserError as exc:
        checks.append(("能列环境", False, str(exc)))
        print(f"失败：{exc}")

    # ---------- 3. 建环境 ----------
    print("\n=== 3. 创建环境 ===")
    fp = build_fingerprint(
        ua=("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"),
        screen_w=1920, screen_h=1080,
        timezone="America/New_York", languages=["en-US", "en"],
        os_type="Win32", hardware_concurrency=8, device_memory=8,
    )
    print("指纹字段:", sorted(fp.keys()))
    print("代理配置:", build_proxy_config(None))

    env_id = ""
    try:
        env = c.create_env(name=f"cdp_verify_{id(fp) % 10000}", fingerprint=fp,
                           remark="cdp-verify")
        env_id = env.id
        checks.append(("能建环境", bool(env_id), f"id={env_id[:16]}… seq={env.seq}"))
        print(f"创建成功 id={env_id}")
    except BitBrowserError as exc:
        checks.append(("能建环境", False, str(exc)))
        print(f"创建失败：{exc}")

    # ---------- 4. 启动 + CDP ----------
    launch = None
    if env_id:
        print("\n=== 4. 启动浏览器（CDP）===")
        try:
            launch = c.open_env(env_id)
            checks.append(("能启动窗口", True, f"pid={launch.pid}"))
            checks.append(("拿到 CDP http 端点", bool(launch.http), launch.cdp_http))
            checks.append(("拿到 CDP ws 端点", bool(launch.ws), launch.cdp_ws[:60]))
            print(f"http = {launch.cdp_http}")
            print(f"ws   = {launch.cdp_ws}")
            print(f"pid  = {launch.pid}  coreVersion = {launch.core_version}")

            ver = c.probe_cdp(launch.cdp_http)
            checks.append(("CDP /json/version 可达", ver is not None,
                           json.dumps(ver, ensure_ascii=False)[:120] if ver else "不可达"))
            if ver:
                print(f"CDP version: {ver.get('Browser')}")
        except LaunchRestrictedError as exc:
            checks.append(("能启动窗口", False,
                           f"code={exc.code} {exc}"))
            print(f"被拒（预期的授权限制）：code={exc.code} {exc}")
        except BitBrowserError as exc:
            checks.append(("能启动窗口", False, str(exc)))
            print(f"启动失败：{exc}")

    # ---------- 5. Playwright 真接管 ----------
    if launch and args.try_cdp:
        print("\n=== 5. Playwright connect_over_cdp 真接管 ===")
        try:
            from playwright.sync_api import sync_playwright

            with sync_playwright() as pw:
                browser = pw.chromium.connect_over_cdp(launch.cdp_http)
                ctxs = browser.contexts
                ctx = ctxs[0] if ctxs else browser.new_context()
                page = ctx.new_page()
                page.goto("data:text/html,<html><body><h1>cdp</h1></body></html>")
                info = page.evaluate(PROBE_JS)
                print(json.dumps(info, ensure_ascii=False, indent=2))

                checks.append(("Playwright 接管成功", True, ""))
                checks.append(("比特浏览器指纹生效：webdriver 为空",
                               info["webdriver"] is None, f"{info['webdriver']}"))
                checks.append(("UA 与配置一致", "Chrome/131" in info["ua"], info["ua"][:60]))
                checks.append(("时区与配置一致", info["tz"] == "America/New_York", info["tz"]))
                checks.append(("硬件并发被改写", info["hw"] == 8, str(info["hw"])))
                checks.append(("Canvas 可采集", bool(info["canvas"]), info["canvas"]))
                ctx.close()
                browser.close()
        except Exception as exc:
            checks.append(("Playwright 接管成功", False, str(exc)[:200]))
            print(f"接管失败：{exc}")

    # ---------- 6. 清理 ----------
    if env_id and not args.keep:
        print("\n=== 6. 清理 ===")
        print("close:", c.close_env(env_id))
        print("delete:", c.delete_env(env_id))

    # ---------- 汇总 ----------
    print("\n=== 结果 ===")
    passed = 0
    width = max(len(x[0]) for x in checks) + 2
    for name, ok, detail in checks:
        print(f"[{'PASS' if ok else 'FAIL'}] {name:<{width}} {detail[:100]}")
        passed += ok
    print(f"\n{passed}/{len(checks)} 通过")

    # ---------- 能力报告 ----------
    print("\n=== 能力报告 ===")
    rep = c.capability_report()
    print(json.dumps(rep, ensure_ascii=False, indent=2))
    return 0 if passed == len(checks) else 2


if __name__ == "__main__":
    raise SystemExit(main())
