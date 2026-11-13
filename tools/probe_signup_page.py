"""实测 Qoder 注册页结构（分步 dump）。

目的：确认 /users/sign-up 三步流程的选择器是否还有效。
不填真实邮箱，只 dump 结构。

用法：
    python tools/probe_signup_page.py
    python tools/probe_signup_page.py --step2    # 继续走到第二步
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

SIGNUP_URL = "https://qoder.com/users/sign-up"
PROXY = "http://127.0.0.1:7897"

DUMP_JS = r"""
() => {
  const vis = (e) => !!(e.offsetWidth || e.offsetHeight || e.getClientRects().length);
  return {
    url: location.href,
    title: document.title,
    inputs: Array.from(document.querySelectorAll('input')).filter(vis).map(e => ({
      type: e.type, id: e.id, name: e.name,
      ph: e.placeholder, ac: e.autocomplete,
      value: e.type === 'password' ? '***' : (e.value || '').slice(0, 40),
      checked: e.type === 'checkbox' ? e.checked : undefined,
    })),
    selects: Array.from(document.querySelectorAll('select')).filter(vis).map(e => e.id),
    textareas: Array.from(document.querySelectorAll('textarea')).filter(vis).map(e => e.id),
    buttons: Array.from(document.querySelectorAll('button')).filter(vis).map(e => ({
      text: (e.innerText || '').trim().slice(0, 40),
      type: e.type, id: e.id, disabled: e.disabled,
    })),
    iframes: Array.from(document.querySelectorAll('iframe')).map(e => e.src).slice(0, 5),
    hasCaptcha: !!document.querySelector('[class*="captcha" i], [id*="captcha" i], iframe[src*="captcha"]'),
    bodyText: (document.body.innerText || '').slice(0, 600),
  };
}
"""


async def run(step2: bool) -> int:
    from playwright.async_api import async_playwright

    proxy = {"server": PROXY} if PROXY else None
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True, proxy=proxy,
                                           args=["--ignore-certificate-errors"])
        ctx = await browser.new_context(
            locale="en-US", ignore_https_errors=True,
            user_agent=("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                        "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"),
        )
        page = await ctx.new_page()
        page.set_default_timeout(30000)

        print(f"=== 1. 打开 {SIGNUP_URL} ===")
        try:
            await page.goto(SIGNUP_URL, wait_until="domcontentloaded", timeout=60000)
        except Exception as exc:
            print(f"打开失败：{exc}")
            await browser.close()
            return 2
        await asyncio.sleep(4)

        d = await page.evaluate(DUMP_JS)
        print(f"URL   : {d['url']}")
        print(f"Title : {d['title']}")
        print(f"滑块  : {d['hasCaptcha']}")
        print(f"iframes: {d['iframes']}")
        print("\n输入框：")
        for i in d["inputs"]:
            print(f"  {json.dumps(i, ensure_ascii=False)}")
        print("\n按钮：")
        for b in d["buttons"]:
            print(f"  {json.dumps(b, ensure_ascii=False)}")
        print(f"\n正文（前 600）：\n{d['bodyText']}")

        if not step2:
            await browser.close()
            print("\n（加 --step2 会继续填表走到下一步）")
            return 0

        # ---- 填表走第一步 ----
        print("\n=== 2. 填姓名/邮箱/勾选 → Continue ===")
        try:
            await page.locator("#basic_firstName").fill("James")
            await page.locator("#basic_lastName").fill("Smith")
            eb = page.locator("#basic_email")
            await eb.click()
            test_mail = "probe_test_0001@outlook.com"
            for ch in test_mail:
                await eb.type(ch, delay=45)
            print(f"  邮箱填入: {await eb.input_value()}")

            cb = page.locator("input[type=checkbox]").first
            if await cb.count() > 0:
                if not await cb.is_checked():
                    await cb.check(force=True)
                print(f"  勾选状态: {await cb.is_checked()}")
        except Exception as exc:
            print(f"  填表失败：{exc}")
            await browser.close()
            return 2

        await asyncio.sleep(1)
        try:
            await page.get_by_role("button", name="Continue").click()
            print("  已点 Continue")
        except Exception as exc:
            print(f"  点 Continue 失败：{exc}")
        await asyncio.sleep(6)

        d2 = await page.evaluate(DUMP_JS)
        print(f"\n--- 第一步提交后 ---")
        print(f"URL   : {d2['url']}")
        print(f"滑块  : {d2['hasCaptcha']}")
        print("\n输入框：")
        for i in d2["inputs"]:
            print(f"  {json.dumps(i, ensure_ascii=False)}")
        print("\n按钮：")
        for b in d2["buttons"]:
            print(f"  {json.dumps(b, ensure_ascii=False)}")
        print(f"\n正文：\n{d2['bodyText']}")

        # ---- 第三步：填密码 ----
        print("\n=== 3. 填密码 → Continue ===")
        try:
            pw = page.locator("input#basic_password, input[autocomplete=new-password]").first
            if await pw.count() > 0:
                await pw.click()
                for ch in "ProbePass123!x":
                    await pw.type(ch, delay=40)
                print("  已填密码")
                await asyncio.sleep(1)
                await page.get_by_role("button", name="Continue").click()
                print("  已点 Continue")
                await asyncio.sleep(8)
        except Exception as exc:
            print(f"  填密码失败：{exc}")

        d3 = await page.evaluate(DUMP_JS)
        print("\n--- 第二步提交后 ---")
        print("URL   :", d3["url"])
        print("滑块  :", d3["hasCaptcha"])
        print("输入框：")
        for i in d3["inputs"]:
            print("  ", json.dumps(i, ensure_ascii=False))
        print("按钮：")
        for b in d3["buttons"]:
            print("  ", json.dumps(b, ensure_ascii=False))
        print("\n正文：\n", d3["bodyText"])

        await browser.close()
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--step2", action="store_true")
    args = ap.parse_args()
    return asyncio.run(run(args.step2))


if __name__ == "__main__":
    raise SystemExit(main())
