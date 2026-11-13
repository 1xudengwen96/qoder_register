"""Qoder 注册端到端测试（真实邮箱 + 真实页面）。

跑法：
    # 用 Playwright 直连（走本机 Clash 代理）
    python tools/run_signup.py --index 0

    # 只走到滑块就停（不消耗邮箱）
    python tools/run_signup.py --index 0 --stop-at-captcha

    # 用比特浏览器（需先配好静态代理）
    python tools/run_signup.py --index 0 --bitbrowser --proxy "http://user:pass@host:port"
"""

from __future__ import annotations

import os

import argparse
import asyncio
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.mail.outlook_oauth import (  # noqa: E402
    OutlookMailbox,
    load_accounts,
)
from app.register.qoder_signup import (  # noqa: E402
    QoderSignup,
    gen_password,
    pick_name,
    wait_code_from_mailbox,
)

MAIL_FILE = "D:/Backup/Downloads/outlook.txt"
PROXY = "http://127.0.0.1:7897"

import logging  # noqa: E402

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-5s %(message)s",
    datefmt="%H:%M:%S",
)
LOG = logging.getLogger("signup")


async def run(index: int, headless: bool, stop_at_captcha: bool,
              use_bb: bool, bb_proxy: str,
              captcha_key: str, no_paid: bool) -> int:
    from playwright.async_api import async_playwright

    accs = load_accounts(MAIL_FILE)
    if not accs:
        print(f"没读到邮箱：{MAIL_FILE}")
        return 2
    if index >= len(accs):
        print(f"index {index} 超出范围（共 {len(accs)} 个）")
        return 2

    acc = accs[index]
    print(f"=== 目标邮箱: {acc.email} ===\n")

    # 先用 OAuth2 确认邮箱可用，并记基线
    box = OutlookMailbox(acc, proxy=PROXY)
    try:
        box.refresh_access_token()
        box.connect()
        box.select("INBOX")
        baseline = {u.decode() if isinstance(u, bytes) else str(u)
                    for u in box.list_uids()}
        print(f"邮箱 OK，当前 INBOX 基线 {len(baseline)} 封\n")
    except Exception as exc:
        print(f"邮箱不可用：{exc}")
        return 2

    first, last = pick_name(acc.email)
    password = gen_password()
    print(f"姓名: {first} {last}")
    print(f"密码: {password}\n")

    # 收码回调
    # 关键：baseline 必须在提交注册表单**之前**拍，
    # 否则会漏掉「提交后立刻到达」的邮件（实测有邮件早于等待开始 4 秒到）。
    async def code_provider():
        print("等待 Qoder 验证码邮件（最多 180s）...")
        code = await wait_code_from_mailbox(box, timeout=180, poll=4,
                                            baseline_uids=baseline)
        print(f"取到验证码: {code or '(超时)'}")
        return code

    bb_client = None
    launch = None
    try:
        async with async_playwright() as pw:
            if use_bb:
                # 比特浏览器：静态代理模式
                from app.register.bitbrowser import (
                    BitBrowserClient,
                    build_fingerprint,
                    build_proxy_fields,
                )

                bb_client = BitBrowserClient()
                if not bb_client.is_port_open():
                    print("比特浏览器未运行")
                    return 2
                fp = build_fingerprint(
                    ua=("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) "
                        "Chrome/131.0.0.0 Safari/537.36"),
                )
                env = bb_client.create_env(
                    name=f"signup_{acc.email.split('@')[0][:12]}",
                    fingerprint=fp,
                    proxy_raw=bb_proxy or None,
                    remark="qoder_signup",
                )
                print(f"比特浏览器环境: {env.id}")
                launch = bb_client.open_env(env.id)
                print(f"CDP: {launch.cdp_http}")
                browser = await pw.chromium.connect_over_cdp(launch.cdp_http)
                ctx = browser.contexts[0] if browser.contexts else \
                    await browser.new_context()
                page = ctx.pages[0] if ctx.pages else await ctx.new_page()
            else:
                browser = await pw.chromium.launch(
                    headless=headless,
                    proxy={"server": PROXY},
                    args=["--ignore-certificate-errors",
                          "--disable-blink-features=AutomationControlled"],
                )
                ctx = await browser.new_context(
                    locale="en-US", ignore_https_errors=True,
                    viewport={"width": 1440, "height": 900},
                    user_agent=("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                                "AppleWebKit/537.36 (KHTML, like Gecko) "
                                "Chrome/131.0.0.0 Safari/537.36"),
                )
                page = await ctx.new_page()

            # 2Captcha 客户端（付费识别）
            paid = None
            if not no_paid:
                from app.net.twocaptcha_dragdrop import TwoCaptchaDragDrop

                paid = TwoCaptchaDragDrop(captcha_key, proxy=PROXY)
                try:
                    print(f"2Captcha 余额: ${paid.balance():.4f}")
                except Exception as exc:
                    print(f"2Captcha 余额查询失败: {exc}，将只用本地识别")
                    paid = None

            signup = QoderSignup(
                page,
                code_provider=code_provider,
                artifacts_dir="data/artifacts",
                humanize=True,
                logger=LOG,
                captcha_max_attempts=6,
                paid_client=paid,
                prefer_paid=paid is not None,
            )

            if stop_at_captcha:
                # 只走到滑块：把 code_provider 换成直接报错
                async def _noop():
                    return ""
                signup.code_provider = _noop

            res = await signup.run(acc.email, password)

            print("\n=== 结果 ===")
            print(json.dumps({
                "ok": res.ok, "stage": res.stage, "detail": res.detail[:200],
                "email": res.email, "password": res.password,
                "name": f"{res.first_name} {res.last_name}",
                "url": res.url, "captcha_attempts": res.captcha_attempts,
                "code": res.code, "screenshot": res.screenshot,
            }, ensure_ascii=False, indent=2))

            if res.ok:
                out = ROOT / "data" / "registered.jsonl"
                out.parent.mkdir(parents=True, exist_ok=True)
                with out.open("a", encoding="utf-8") as fh:
                    fh.write(json.dumps({
                        "email": res.email, "password": res.password,
                        "first": res.first_name, "last": res.last_name,
                    }, ensure_ascii=False) + "\n")
                print(f"\n已记录到 {out}")

            await browser.close()
            return 0 if res.ok else 1
    finally:
        if bb_client and launch:
            try:
                bb_client.close_env(launch.env_id)
            except Exception:
                pass
        box.close()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--index", type=int, default=0, help="用第几个邮箱")
    ap.add_argument("--headless", action="store_true")
    ap.add_argument("--stop-at-captcha", action="store_true")
    ap.add_argument("--bitbrowser", action="store_true")
    ap.add_argument("--proxy", default="", help="比特浏览器的静态代理")
    ap.add_argument("--captcha-key", default=os.environ.get("TWOCAPTCHA_KEY", ""),
                    help="2Captcha API Key")
    ap.add_argument("--no-paid", action="store_true",
                    help="禁用付费识别，只用本地 OpenCV")
    args = ap.parse_args()
    return asyncio.run(run(args.index, args.headless, args.stop_at_captcha,
                           args.bitbrowser, args.proxy,
                           args.captcha_key, args.no_paid))


if __name__ == "__main__":
    raise SystemExit(main())
