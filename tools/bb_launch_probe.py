"""比特浏览器：配代理 → CDP 启动 → 指纹/代理/时区实测。

用法：
    # 复用已有环境，改成静态代理后启动
    python tools/bb_launch_probe.py --env-id 23729bb207534cceb43d0e27a9dd2252 \
        --proxy http://127.0.0.1:7897

    # 新建环境（一号一环境），带代理启动
    python tools/bb_launch_probe.py --name qoder_acct_01 --proxy http://127.0.0.1:7897

    # 动态代理（IPDEEP 提取链接）
    python tools/bb_launch_probe.py --name qoder_acct_02 --dynamic-url "https://..."

    # 启动后打开指定页并截图
    python tools/bb_launch_probe.py --name qoder_acct_01 --proxy http://127.0.0.1:7897 \
        --url https://qoder.com/users/sign-up --screenshot data/artifacts/bb_signup.png

退出码：0 全部通过；1 有检查项失败；2 参数/环境问题。
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import secrets  # noqa: E402
from app.register.bitbrowser import (  # noqa: E402
    BitBrowserClient,
    BitBrowserError,
    LaunchRestrictedError,
    build_fingerprint,
    build_proxy_config,
)

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")

# 在页面里跑：指纹 + 代理出口 + 时区，一次取全
PROBE_JS = r"""
async () => {
  const nav = navigator, scr = screen;
  const out = {
    webdriver: nav.webdriver,
    ua: nav.userAgent,
    platform: nav.platform,
    languages: nav.languages,
    timezone: Intl.DateTimeFormat().resolvedOptions().timeZone,
    offsetMin: new Date().getTimezoneOffset(),
    screen: `${scr.width}x${scr.height}`,
    colorDepth: scr.colorDepth,
    devicePixelRatio: window.devicePixelRatio,
    hardwareConcurrency: nav.hardwareConcurrency,
    deviceMemory: nav.deviceMemory === undefined ? null : nav.deviceMemory,
    plugins: nav.plugins.length,
    webglVendor: null, webglRender: null,
    canvasHash: null,
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
    ctx.textBaseline = 'top';
    ctx.font = '14px Arial';
    ctx.fillStyle = '#f60';
    ctx.fillRect(0, 0, 220, 40);
    ctx.fillStyle = '#069';
    ctx.fillText('qoder-canvas-probe', 2, 15);
    const d = c.toDataURL();
    let h = 0;
    for (let i = 0; i < d.length; i++) { h = ((h << 5) - h + d.charCodeAt(i)) | 0; }
    out.canvasHash = h;
  } catch (e) {}
  try {
    const ctl = new AbortController();
    const t = setTimeout(() => ctl.abort(), 12000);
    const r = await fetch('https://api.ip.sb/geoip', {signal: ctl.signal});
    clearTimeout(t);
    const j = await r.json();
    out.ip = j.ip; out.ipCountry = j.country_code; out.ipTz = j.timezone;
  } catch (e) { out.ipError = String(e).slice(0, 80); }
  return out;
}
"""


def configure_proxy(client: BitBrowserClient, env_id: str, args,
                    keep_existing: bool = False) -> dict:
    """把代理写进环境配置，返回写入的字段。"""
    if keep_existing and not args.proxy and not args.dynamic_url:
        env = client.get_env(env_id)
        return {"_kept": f"proxyMethod={env.proxy_method} {env.host}:{env.port}"}
    if args.dynamic_url:
        fields = {
            "proxyMethod": 4,
            "proxyType": args.dynamic_type,
            "dynamicIpUrl": args.dynamic_url,
            "isDynamicIpChangeIp": bool(args.dynamic_change_ip),
        }
    else:
        fields = build_proxy_config(args.proxy, args.proxy_type)
    client.update_env(env_id, **fields)
    return fields


def align_fingerprint(client: BitBrowserClient, env_id: str, args) -> dict:
    """对齐指纹的时区 / 语言 / UA。

    比特浏览器的「跟随代理 IP」需要它自己的地理探测能走通代理；本机
    127.0.0.1 的本地代理常探测失败，于是时区语言落回本机（实测 Asia/Shanghai
    + zh-CN，和 SG 出口矛盾）。这里改成**显式指定**，把 isIpCreate* 关掉，
    让时区语言固定成与出口一致的值。
    """
    env = client.get_env(env_id)
    fp = dict(env.raw.get("browserFingerPrint") or {})
    changed: dict = {}

    if args.follow_geo:
        # 出口 IP 已经由远端代理（IPDEEP）决定，让比特自己按 IP 派生
        # 时区/语言/地理 —— 这比手钉更准（手钉会跟实际出口打架）
        fp["isIpCreateTimeZone"] = True
        fp["isIpCreateLanguage"] = True
        fp["isIpCreatePosition"] = True
        fp["isIpCreateDisplayLanguage"] = True
        fp["position"] = "1"
        changed["follow_geo"] = True

    if args.tz and not args.follow_geo:
        fp["isIpCreateTimeZone"] = False
        fp["isIpCreatePosition"] = False
        fp["position"] = ""
        fp["timeZone"] = args.tz
        fp["timeZoneOffset"] = int(args.tz_offset or 0)
        changed["timeZone"] = args.tz
    if args.langs and not args.follow_geo:
        langs = [x.strip() for x in args.langs.split(",") if x.strip()]
        if langs:
            fp["isIpCreateLanguage"] = False
            fp["isIpCreateDisplayLanguage"] = False
            fp["languages"] = ",".join(langs)
            fp["displayLanguages"] = ",".join(langs)
            changed["languages"] = fp["languages"]
    if args.ua:
        fp["userAgent"] = args.ua
        changed["userAgent"] = args.ua

    if changed:
        client.update_env(env_id, browserFingerPrint=fp)
    return changed


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--env-id", default=secrets.get("bitbrowser.env_id", ""),
                    help="复用已有环境 id（默认取配置）")
    ap.add_argument("--name", default="", help="新建环境名（一号一环境）")
    ap.add_argument("--proxy", default="", help="http://user:pass@host:port，空=直连")
    ap.add_argument("--proxy-type", default="http", choices=["http", "https", "socks5"])
    ap.add_argument("--dynamic-url", default="", help="动态代理提取链接（proxyMethod=4）")
    ap.add_argument("--dynamic-type", default="noproxy")
    ap.add_argument("--dynamic-change-ip", action="store_true")
    ap.add_argument("--tz", default=secrets.get("bitbrowser.timezone", ""),
                    help="显式时区（默认取配置，关掉跟随 IP）")
    ap.add_argument("--tz-offset", default="", help="时区偏移分钟，如 -480")
    ap.add_argument("--langs", default=secrets.get("bitbrowser.languages", ""),
                    help="显式语言表（默认取配置）")
    ap.add_argument("--follow-geo", action="store_true",
                    default=bool(secrets.get("bitbrowser.follow_geo", False)),
                    help="让比特按出口 IP 自动派生时区/语言/地理（远端代理推荐）")
    ap.add_argument("--ua", default="", help="覆写 UA")
    ap.add_argument("--url", default="")
    ap.add_argument("--screenshot", default="")
    ap.add_argument("--keep-open", action="store_true", help="测完不关浏览器")
    ap.add_argument("--timeout", type=int, default=120)
    args = ap.parse_args()

    if not args.env_id and not args.name:
        print("要么 --env-id 复用，要么 --name 新建")
        return 2

    client = BitBrowserClient()
    if not client.health():
        print("FAIL 本地 API 不可用（比特浏览器没开或端口不是 54345）")
        return 2

    # ---------------------------------------------------------- 环境
    try:
        if args.env_id:
            env_id = args.env_id
            client.get_env(env_id)
            print(f"PASS 复用环境 {env_id}")
        else:
            fp = build_fingerprint(ua=UA, timezone="America/New_York",
                                   languages=["en-US", "en"], core_version="131")
            env = client.create_env(name=args.name, fingerprint=fp,
                                    remark="qoder_acct", proxy_raw=None)
            env_id = env.id
            print(f"PASS 新建环境 {env_id} name={args.name}")
        fields = configure_proxy(client, env_id, args, keep_existing=bool(args.env_id))
        shown = {k: (v if k != "proxyPassword" else "***") for k, v in fields.items()}
        if "_kept" in shown:
            print(f"PASS 保留现有代理（未改动）: {shown['_kept']}")
        else:
            print(f"PASS 代理已写入环境: {json.dumps(shown, ensure_ascii=False)}")
        fp_changed = align_fingerprint(client, env_id, args)
        if fp_changed:
            print(f"PASS 指纹已对齐: {json.dumps(fp_changed, ensure_ascii=False)}")
    except BitBrowserError as exc:
        print(f"FAIL 环境配置失败: {exc} code={exc.code}")
        return 2

    # ---------------------------------------------------------- 启动
    launch = None
    try:
        launch = client.open_env(env_id)
    except LaunchRestrictedError as exc:
        print(f"FAIL 启动被限：code={exc.code}（RPA 并发额度）")
        return 1
    except BitBrowserError as exc:
        print(f"FAIL 启动失败: {exc} code={exc.code} raw={exc.raw}")
        return 1

    print(f"PASS 已启动 CDP={launch.cdp_http} pid={launch.pid} core={launch.core_version}")

    ver = client.probe_cdp(launch.cdp_http)
    if not ver:
        print("FAIL CDP /json/version 不可达")
        return 1
    print(f"PASS CDP /json/version: {ver.get('Browser')}")

    # ---------------------------------------------------------- CDP 接管（纯 Python，无 node 驱动）
    from app.register import cdp_raw

    report: dict = {"env_id": env_id, "cdp": launch.cdp_http,
                    "browser": ver.get("Browser"), "core": launch.core_version}
    failures: list[str] = []

    with cdp_raw.CDP(launch.cdp_http) as page:
        print(f"PASS 纯 Python CDP 已连接 target={page.target.get('url') or '(page)'}")
        page.enable()

        if args.url:
            url_now = page.navigate(args.url, timeout=60000)
            print(f"PASS 打开 {args.url} -> {url_now}")

        probe = page.evaluate(cdp_raw.PROBE_JS)
        report["fingerprint"] = probe
        print("---- 实测 ----")
        for k in ("webdriver", "ua", "platform", "languages", "timezone",
                  "offsetMin", "screen", "hardwareConcurrency", "deviceMemory",
                  "webglVendor", "webglRender", "canvasHash",
                  "ip", "ipCountry", "ipTz"):
            print(f"  {k:22s} {probe.get(k)}")
        if probe.get("ipError"):
            print(f"  ipError               {probe['ipError']}")

        # ---- 判定 ----
        if probe.get("webdriver") is not False:
            failures.append("navigator.webdriver 不是 false")
        if probe.get("ip"):
            if not probe.get("ipCountry"):
                failures.append("拿不到出口国家")
            else:
                report["proxy_ok"] = True
        else:
            failures.append("页面内取不到出口 IP（代理可能不通）")
        if probe.get("ipCountry") and probe.get("timezone"):
            ip_tz = probe.get("ipTz") or ""
            if ip_tz:
                report["tz_matches_ip"] = (probe["timezone"] == ip_tz)
                if not report["tz_matches_ip"]:
                    failures.append(
                        f"时区 {probe['timezone']} 与出口 IP 时区 {ip_tz} 不一致")
            else:
                tz_country = {
                    "Asia/Singapore": "SG", "Asia/Shanghai": "CN",
                    "Asia/Hong_Kong": "HK", "America/New_York": "US",
                    "America/Chicago": "US", "America/Denver": "US",
                    "America/Los_Angeles": "US", "Europe/London": "GB",
                    "Asia/Tokyo": "JP", "Europe/Berlin": "DE",
                }.get(probe["timezone"])
                report["tz_matches_ip"] = (tz_country == probe["ipCountry"])
                if tz_country and not report["tz_matches_ip"]:
                    failures.append(
                        f"时区 {probe['timezone']}({tz_country}) 与出口 "
                        f"{probe['ipCountry']} 不一致")
        if probe.get("languages"):
            primary = (probe["languages"] or [""])[0]
            if args.langs and primary not in args.langs:
                failures.append(f"语言 {probe['languages']} 没生效（期望 {args.langs}）")
            elif not args.langs and primary.lower().startswith("zh") \
                    and probe.get("ipCountry") not in ("CN", "HK", "TW", "SG"):
                failures.append(f"语言 {primary} 与出口 {probe.get('ipCountry')} 不搭")

        env = client.get_env(env_id)
        report["env_last_ip"] = env.raw.get("lastIp", "")
        report["env_last_country"] = env.raw.get("lastCountry", "")
        print(f"  env.lastIp             {report['env_last_ip']} "
              f"({report['env_last_country']})")

        if args.screenshot:
            out = Path(args.screenshot)
            out.parent.mkdir(parents=True, exist_ok=True)
            try:
                page.screenshot(str(out))
                report["screenshot"] = str(out)
                print(f"PASS 截图 {out}")
            except Exception as exc:
                print(f"截图失败: {type(exc).__name__}: {exc}")

    report["checks_failed"] = failures
    if failures:
        print("\n---- 未通过 ----")
        for f in failures:
            print(f"  ! {f}")
    else:
        print("\n全部通过")

    print(json.dumps(report, ensure_ascii=False, indent=2))

    if not args.keep_open:
        try:
            client.close_env(env_id)
            print(f"已关闭环境 {env_id}")
        except Exception:
            pass
    return 0 if not failures else 1


if __name__ == "__main__":
    raise SystemExit(main())
