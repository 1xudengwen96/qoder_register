"""深度抓取 IPDEEP 文档页：点击每个菜单项，抓取出每个接口的完整调用地址。

文档是 SPA，接口地址在点击菜单后才渲染。必须逐个点击。

用法：
    python tools/capture_ipdeep_doc2.py
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from playwright.sync_api import sync_playwright  # noqa: E402

PROXY = {"server": "http://127.0.0.1:7897"}
DOC_URL = "https://user.ipdeep.cn/api-docv2"

MENUS = ["用户账户", "动态代理", "静态代理", "订单", "白名单"]
SUBS = ["账户余额", "剩余流量", "获取可用国家", "获取可用省份", "获取可用城市",
        "生成代理信息", "生成代理链接", "获取IP列表", "添加白名单"]


def main() -> int:
    captured: dict[str, set[str]] = {}
    texts: dict[str, str] = {}

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True, proxy=PROXY,
                                     args=["--ignore-certificate-errors"])
        ctx = browser.new_context(locale="zh-CN", ignore_https_errors=True)
        page = ctx.new_page()

        def on_request(req):
            u = req.url
            if "openApi" in u or "/api/" in u:
                host_path = u.split("?")[0]
                captured.setdefault("requests", set()).add(
                    f"{req.method} {host_path}")

        page.on("request", on_request)
        page.goto(DOC_URL, wait_until="domcontentloaded", timeout=60000)
        page.wait_for_timeout(6000)

        print("=== 逐个点击菜单 ===")
        for m in MENUS:
            try:
                loc = page.get_by_text(m, exact=True).first
                if loc.count():
                    loc.click(timeout=3000)
                    page.wait_for_timeout(800)
                    print(f"  菜单：{m}")
            except Exception:
                pass

        for s in SUBS:
            try:
                loc = page.get_by_text(s, exact=False).first
                if loc.count():
                    loc.click(timeout=2500)
                    page.wait_for_timeout(1000)
                    print(f"    子项：{s}")
                    # 抓右侧面板文本
                    try:
                        body = page.inner_text("body")
                        texts[s] = body
                        # 找地址
                        for m2 in re.finditer(
                                r"https?://[A-Za-z0-9\.\-]+/[A-Za-z0-9_\-/{}\.]+", body):
                            captured.setdefault("urls", set()).add(m2.group(0))
                    except Exception:
                        pass
            except Exception:
                pass

        # 最后再抓一次全页
        page.wait_for_timeout(2000)
        try:
            full = page.inner_text("body")
            (ROOT / "data" / "ipdeep_full.txt").write_text(full, encoding="utf-8")
            print(f"\n全文已存：data/ipdeep_full.txt（{len(full)} 字）")
        except Exception:
            full = ""

        # dump HTML 里所有 openApi 相关片段
        try:
            html = page.content()
            (ROOT / "data" / "ipdeep_full.html").write_text(html, encoding="utf-8")
            print(f"HTML 已存：data/ipdeep_full.html（{len(html)} 字）")
        except Exception:
            html = ""

        ctx.close()
        browser.close()

    print("\n=== 捕获到的 URL ===")
    for u in sorted(captured.get("urls", set())):
        print(f"  {u}")

    print("\n=== 捕获到的 API 请求 ===")
    for r in sorted(captured.get("requests", set())):
        print(f"  {r}")

    # 从全文里找 Controller/Action 形式的片段
    print("\n=== 全文中形如 Xxx/Yyy 的接口名 ===")
    if full:
        cand = set(re.findall(r"\b([A-Z][A-Za-z]+)/([A-Z][A-Za-z]+)\b", full))
        for c, a in sorted(cand):
            if c not in ("IPDEEP", "OpenAPI"):
                print(f"  {c}/{a}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
