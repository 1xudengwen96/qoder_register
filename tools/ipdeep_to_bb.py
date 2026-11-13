"""IPDEEP 提取代理 → 灌进比特浏览器环境 → 启动并验证出口。

两种接入方式，任选其一：

A) OpenAPI（要 API Key + Secret，写进 config/secrets.yaml）
    python tools/ipdeep_to_bb.py --country SG --num 1

B) 后台生成的「提取链接」（不需要 key/secret）
    python tools/ipdeep_to_bb.py --extract-url "https://..."

C) 直接把一条已知代理写进环境（跳提取）
    python tools/ipdeep_to_bb.py --proxy "http://user:pass@1.2.3.4:8080"

跑完会打印：环境里的代理字段、浏览器页面内看到的出口 IP/国家/时区、
以及比特浏览器自己记录的 lastIp/lastCountry（这两个以前是 CN，就是代理
没被它自己识别；用远端代理后应该跟出口一致）。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import secrets  # noqa: E402
from app.net.ipdeep import IpdeepClient, IpdeepError, ProxyInfo, parse_proxy_payload  # noqa: E402
from app.register.bitbrowser import (  # noqa: E402
    BitBrowserClient,
    BitBrowserError,
    build_proxy_config,
)

DEFAULT_LOCAL_PROXY = "http://127.0.0.1:7897"


def _rewrite_country(info: ProxyInfo, country: str) -> ProxyInfo:
    """IPDEEP 的用户名里带 ``country-XX``，改掉就能换落地国家，不用重生成链接。"""
    if not country or not info.username:
        return info
    import re

    if not re.search(r"country-[A-Za-z]{2}", info.username):
        return info
    new_user = re.sub(r"country-[A-Za-z]{2}", f"country-{country.upper()}",
                      info.username)
    if new_user == info.username:
        return info
    print(f"国家改写: {info.username} -> {new_user}")
    info.username = new_user
    return info


def pick_proxy(args) -> ProxyInfo:
    """按参数拿一条代理。"""
    if args.proxy:
        got = parse_proxy_payload(args.proxy)
        if not got:
            raise SystemExit(f"解析不了这条代理: {args.proxy}")
        return _rewrite_country(got[0], args.country if args.force_country else "")

    if args.extract_url:
        import requests

        url = args.extract_url
        proxies = None
        if args.via_local:
            proxies = {"http": DEFAULT_LOCAL_PROXY, "https": DEFAULT_LOCAL_PROXY}
        r = requests.get(url, timeout=30, proxies=proxies)
        r.raise_for_status()
        got = parse_proxy_payload(r.text)
        if not got:
            raise SystemExit(f"提取链接返回里没有代理：{r.text[:200]}")
        print(f"提取链接返回 {len(got)} 条，取第一条")
        return _rewrite_country(got[0], args.country if args.force_country else "")

    key = args.key or secrets.get("ipdeep.key", "")
    sec = args.secret or secrets.get("ipdeep.secret", "")
    if not key or not sec:
        raise SystemExit(
            "缺 IPDEEP 凭据：把 API Key / Secret 填进 config/secrets.yaml 的 "
            "ipdeep.key / ipdeep.secret，或用 --extract-url 传提取链接")

    client = IpdeepClient(key, sec, proxy=DEFAULT_LOCAL_PROXY)
    try:
        bal = client.balance()
        print(f"账户信息: {json.dumps(bal, ensure_ascii=False)[:200]}")
    except IpdeepError as exc:
        print(f"账户接口失败（继续试提取）: code={exc.code} {exc}")

    got = client.extract_proxies(
        proxy_type=args.proxy_type, country=args.country, state=args.state,
        city=args.city, num=args.num, protocol=args.protocol,
        duration=args.duration, rotation=args.rotation)
    print(f"提取到 {len(got)} 条代理，取第一条")
    return _rewrite_country(got[0], args.country if args.force_country else "")


def verify_via_cdp(endpoint: str) -> dict:
    """在浏览器里看真实出口。"""
    from app.register import cdp_raw

    try:
        with cdp_raw.CDP(endpoint) as page:
            page.enable()
            page.navigate("about:blank", timeout=20)
            return page.evaluate(cdp_raw.PROBE_JS) or {}
    except Exception as exc:
        return {"error": f"{type(exc).__name__}: {exc}"}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--env-id", default=secrets.get("bitbrowser.env_id", ""))
    ap.add_argument("--create-name", default="",
                    help="新建环境并写入该代理（一号一环境；指纹在创建时定死）")
    ap.add_argument("--key", default="")
    ap.add_argument("--secret", default="")
    ap.add_argument("--proxy", default="", help="直接指定代理 URL")
    ap.add_argument("--extract-url", default="", help="IPDEEP 提取链接")
    ap.add_argument("--via-local", action="store_true",
                    help="走本机 7897 访问提取链接")
    ap.add_argument("--proxy-type", default="dynamicResidential")
    ap.add_argument("--country", default="SG")
    ap.add_argument("--tz", default="", help="新建环境的时区，如 America/Chicago")
    ap.add_argument("--explicit-tz", action="store_true",
                    help="关闭「跟随 IP」，时区语言显式钉死（推荐，实测跟随会把创建瞬间的检测固化）")
    ap.add_argument("--force-country", action="store_true",
                    help="把用户名里的 country-XX 改成 --country（提取链接方式）")
    ap.add_argument("--state", default="")
    ap.add_argument("--city", default="")
    ap.add_argument("--num", type=int, default=1)
    ap.add_argument("--protocol", default="http", choices=["http", "socks5"])
    ap.add_argument("--duration", type=int, default=30)
    ap.add_argument("--rotation", default="stickiness")
    ap.add_argument("--no-launch", action="store_true", help="只写环境配置不启动")
    ap.add_argument("--keep-open", action="store_true")
    args = ap.parse_args()

    if not args.env_id and not args.create_name:
        raise SystemExit("缺环境 id：--env-id 或 config/secrets.yaml 的 bitbrowser.env_id")

    info = pick_proxy(args)
    raw = info.url
    print(f"使用代理: {raw[:40]}{'***' if info.password else ''} "
          f"({info.country or '?'})")

    client = BitBrowserClient()
    if not client.health():
        raise SystemExit("比特浏览器本地 API 不通（54345）")

    if args.create_name:
        # 一号一环境：指纹只在创建时生效，所以时区/语言必须此刻定死
        from app.register.bitbrowser import build_fingerprint

        tz = args.tz or secrets.get("bitbrowser.timezone", "America/New_York")
        langs = [x.strip() for x in
                 str(secrets.get("bitbrowser.languages", "en-US,en")).split(",")
                 if x.strip()]
        fp = build_fingerprint(
            ua=("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/155.0.8059.86 Safari/537.36"),
            timezone=tz, languages=langs, hardware_concurrency=8,
            device_memory=8)
        fp["isIpv6"] = False          # 禁用 IPv6：实测会让流量绕过代理走直连
        if args.explicit_tz:
            # 显式钉死：实测「跟随 IP」会把创建那一刻的检测结果固化进去
            fp["isIpCreateTimeZone"] = False
            fp["isIpCreatePosition"] = False
            fp["position"] = ""
            fp["isIpCreateLanguage"] = False
            fp["isIpCreateDisplayLanguage"] = False
            fp["languages"] = ",".join(langs)
            fp["displayLanguages"] = ",".join(langs)
        else:
            fp["isIpCreateTimeZone"] = True
            fp["isIpCreateLanguage"] = True
            fp["isIpCreatePosition"] = True
            fp["isIpCreateDisplayLanguage"] = True
            fp["position"] = "1"
        env = client.create_env(name=args.create_name, fingerprint=fp,
                                proxy_raw=raw, remark="qoder_acct")
        env_id = env.id
        print(f"PASS 已新建环境 {env_id} name={args.create_name} "
              f"tz={tz} follow_ip=True")
    else:
        env_id = args.env_id
        if client.get_env(env_id).status == 1:
            client.close_env(env_id)
        fields = build_proxy_config(raw,
                                    "socks5" if args.protocol == "socks5" else "http")
        client.update_env(env_id, **fields)
        shown = {k: ("***" if k == "proxyPassword" else v)
                 for k, v in fields.items()}
        print(f"PASS 已写入环境 {env_id}: "
              f"{json.dumps(shown, ensure_ascii=False)}")

    if args.no_launch:
        return 0

    try:
        launch = client.open_env(env_id)
    except BitBrowserError as exc:
        print(f"FAIL 启动失败: {exc} code={exc.code}")
        return 1
    print(f"PASS 已启动 CDP={launch.cdp_http}")

    probe = verify_via_cdp(launch.cdp_http)
    env = client.get_env(env_id)
    print("\n---- 浏览器内实测 ----")
    for k in ("ip", "ipCountry", "ipTz", "timezone", "languages", "webdriver"):
        print(f"  {k:12s} {probe.get(k)}")
    print(f"  lastIp       {env.raw.get('lastIp')} ({env.raw.get('lastCountry')})")

    ok = bool(probe.get("ip")) and probe.get("ipError") is None
    same = env.raw.get("lastIp") == probe.get("ip")
    print(f"\n出口可达: {'是' if ok else '否'}；"
          f"比特自检 lastIp {'与出口一致' if same else '仍与出口不一致'}")

    if not args.keep_open:
        client.close_env(env_id)
        print(f"已关闭环境 {env_id}")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
