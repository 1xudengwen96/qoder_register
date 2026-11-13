"""验证比特浏览器在「静态代理」模式下能否启动 + CDP 接管。

背景：套餐开通后，11018 已消失，现在报 "请填写提取URL: dynamicIpUrl"。
说明比特浏览器把环境当成了「动态IP」模式，要求提取链接。

本脚本对比几种 proxyMethod，找出不依赖 dynamicIpUrl 的配置方式：

    proxyMethod=1  本地/不使用
    proxyMethod=2  自定义代理（host/port/user/pass）
    proxyMethod=3  直连
    proxyMethod=4  动态IP（需要 dynamicIpUrl）

用法：
    python tools/verify_bb_proxy.py
    python tools/verify_bb_proxy.py --proxy-raw "http://127.0.0.1:7897"
"""

from __future__ import annotations

import argparse
import json
import sys
import time
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
from urllib.parse import urlparse  # noqa: E402

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")


def make_fp():
    return build_fingerprint(
        ua=UA, screen_w=1920, screen_h=1080,
        timezone="America/New_York", languages=["en-US", "en"],
        os_type="Win32", hardware_concurrency=8, device_memory=8,
    )


def try_config(c: BitBrowserClient, label: str, proxy_fields: dict,
               dry_only: bool = False) -> dict:
    """建环境 → 启动 → 记录结果 → 清理。"""
    fp = make_fp()
    body = {
        "name": f"pm_{label}_{int(time.time())}",
        "remark": "proxy-mode-test",
        "browserFingerPrint": fp,
        **proxy_fields,
    }
    try:
        d = c.call("/browser/update", body, timeout=90).get("data") or {}
        env_id = d.get("id", "")
    except BitBrowserError as exc:
        return {"label": label, "create": False, "error": f"建环境失败 {exc}"}

    if not env_id:
        return {"label": label, "create": False, "error": "未返回 id"}

    out = {"label": label, "create": True, "env_id": env_id,
           "launch": False, "cdp": "", "error": ""}

    if dry_only:
        c.delete_env(env_id)
        return out

    try:
        lr = c.open_env(env_id)
        out["launch"] = True
        out["cdp"] = lr.cdp_http
        out["pid"] = lr.pid
        ver = c.probe_cdp(lr.cdp_http)
        out["cdp_alive"] = ver is not None
        out["browser"] = (ver or {}).get("Browser", "")
        c.close_env(env_id)
    except LaunchRestrictedError as exc:
        out["error"] = f"11018 并发限制: {exc}"
    except BitBrowserError as exc:
        out["error"] = str(exc)[:120]
    finally:
        time.sleep(0.5)
        c.delete_env(env_id)
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--proxy-raw", default="http://127.0.0.1:7897",
                    help="静态代理，默认用本机 Clash")
    args = ap.parse_args()

    c = BitBrowserClient()
    if not c.is_port_open():
        print("比特浏览器未运行")
        return 2

    # 构造静态代理字段
    u = urlparse(args.proxy_raw if "://" in args.proxy_raw
                 else "http://" + args.proxy_raw)
    static_proxy = {
        "proxyMethod": 2,
        "proxyType": u.scheme or "http",
        "host": u.hostname or "127.0.0.1",
        "port": str(u.port or 7897),
        "proxyUserName": u.username or "",
        "proxyPassword": u.password or "",
    }

    configs = [
        ("直连 pm3", {"proxyMethod": 3, "proxyType": "noproxy"}),
        ("静态 pm2", static_proxy),
        ("静态 pm2 + 空dynamicIpUrl",
         {**static_proxy, "dynamicIpUrl": ""}),
        ("直连 pm3 + 空dynamicIpUrl",
         {"proxyMethod": 3, "proxyType": "noproxy", "dynamicIpUrl": ""}),
    ]

    print("=== 对比 proxyMethod 配置 ===\n")
    results = []
    for label, fields in configs:
        r = try_config(c, label.replace(" ", "_").replace("+", ""), fields)
        results.append(r)
        status = "启动OK" if r.get("launch") else "失败"
        print(f"[{label}] 建环境={r.get('create')} {status}")
        if r.get("error"):
            print(f"    {r['error']}")
        if r.get("cdp"):
            print(f"    CDP: {r['cdp']}  Browser: {r.get('browser','')}")
        time.sleep(1.5)

    print("\n=== 汇总 ===")
    ok = [r for r in results if r.get("launch")]
    for r in results:
        mark = "PASS" if r.get("launch") else "FAIL"
        detail = r.get("cdp") or r.get("error", "")
        print(f"[{mark}] {r['label']:<30} {detail[:70]}")

    print(f"\n{len(ok)}/{len(results)} 能启动")
    if ok:
        print("\n可用的配置：")
        for r in ok:
            print(f"  {r['label']}  →  CDP {r['cdp']}")
    return 0 if ok else 2


if __name__ == "__main__":
    raise SystemExit(main())
