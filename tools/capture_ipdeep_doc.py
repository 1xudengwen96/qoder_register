"""抓取 IPDEEP 文档页真实调用的 API 地址（走本机代理）。

本机 Clash Verge 用 fake-ip，必须让 Playwright 也走 127.0.0.1:7897，
否则 DNS 解析到 198.18.x.x 会连不上。

用法：
    python tools/capture_ipdeep_doc.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from playwright.sync_api import sync_playwright  # noqa: E402

PROXY = {"server": "http://127.0.0.1:7897"}
DOC_URL = "https://user.ipdeep.cn/api-docv2"


def main() -> int:
    apis: dict[str, dict] = {}
    all_hosts: set[str] = set()

    with sync_playwright() as pw:
        browser = pw.chromium.launch(
            headless=True,
            proxy=PROXY,
            args=["--ignore-certificate-errors"],
        )
        ctx = browser.new_context(
            locale="zh-CN",
            ignore_https_errors=True,
            user_agent=("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) "
                        "Chrome/131.0.0.0 Safari/537.36"),
        )
        page = ctx.new_page()

        def on_request(req):
            u = req.url
            try:
                from urllib.parse import urlparse
                all_hosts.add(urlparse(u).netloc)
            except Exception:
                pass
            if "/api/" in u or "/openapi" in u.lower() or "openapi." in u:
                key = f"{req.method} {u.split('?')[0]}"
                if key not in apis:
                    apis[key] = {
                        "method": req.method,
                        "url": u.split("?")[0],
                        "post": (req.post_data or "")[:400],
                    }

        page.on("request", on_request)

        print(f"打开 {DOC_URL} ...")
        try:
            page.goto(DOC_URL, wait_until="domcontentloaded", timeout=60000)
        except Exception as exc:
            print(f"goto 异常：{exc}")

        page.wait_for_timeout(8000)

        # 抓页面文本里的 endpoint
        print("\n=== 页面文本里的地址与路径 ===")
        try:
            text = page.inner_text("body")
            import re
            urls = set(re.findall(r"https?://[A-Za-z0-9\.\-]+(?:/[A-Za-z0-9_\-/\.]*)?", text))
            for u in sorted(urls)[:20]:
                print(f"  URL  {u}")
            paths = set(re.findall(r"/[A-Za-z][A-Za-z0-9_\-]*(?:/[A-Za-z][A-Za-z0-9_\-]*){1,4}", text))
            if paths:
                print()
                for p in sorted(paths)[:25]:
                    print(f"  PATH {p}")
        except Exception as exc:
            print(f"  读文本失败：{exc}")

        # 尝试点开左侧菜单，触发更多请求
        print("\n=== 展开菜单 ===")
        for label in ("用户账户", "动态代理", "账户余额", "生成代理信息", "获取可用国家"):
            try:
                loc = page.get_by_text(label, exact=False).first
                if loc.count():
                    loc.click(timeout=2500)
                    page.wait_for_timeout(900)
                    print(f"  点击 {label}")
            except Exception:
                pass

        page.wait_for_timeout(3000)
        shot = ROOT / "data" / "ipdeep_doc.png"
        try:
            shot.parent.mkdir(parents=True, exist_ok=True)
            page.screenshot(path=str(shot), full_page=True)
            print(f"\n截图：{shot}")
        except Exception:
            pass

        # 把渲染后的完整文本存下来，供离线分析
        try:
            body = page.inner_text("body")
            (ROOT / "data" / "ipdeep_doc.txt").write_text(body, encoding="utf-8")
            print(f"页面文本：data/ipdeep_doc.txt（{len(body)} 字）")
        except Exception:
            pass

        ctx.close()
        browser.close()

    print("\n=== 访问过的域名 ===")
    for h in sorted(all_hosts):
        print(f"  {h}")

    print("\n=== 捕获到的 API 端点 ===")
    for k in sorted(apis):
        v = apis[k]
        print(f"[{v['method']}] {v['url']}")
        if v["post"]:
            print(f"       body: {v['post'][:250]}")

    print(f"\n共 {len(apis)} 个 API 端点")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
