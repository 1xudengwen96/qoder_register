"""dump Qoder 验证码输入页的完整结构（6 格 OTP）。

目的：搞清楚填满 6 位后怎么提交 —— 是自动提交、还是要点某个按钮/按 Enter。

用法：
    python tools/diag_otp_page.py --index 7
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

from app.mail.outlook_oauth import OutlookMailbox, load_accounts  # noqa: E402

MAIL_FILE = "D:/Backup/Downloads/outlook.txt"
PROXY = "http://127.0.0.1:7897"
SIGNUP_URL = "https://qoder.com/users/sign-up"

DUMP = r"""
() => {
  const vis = (e) => !!(e.offsetWidth || e.offsetHeight || e.getClientRects().length);
  return {
    url: location.href,
    inputs: Array.from(document.querySelectorAll('input')).filter(vis).map(e => ({
      type: e.type, id: e.id, name: e.name, ph: e.placeholder,
      ac: e.autocomplete, maxlength: e.maxLength, inputmode: e.inputMode,
      valueLen: (e.value || '').length,
      cls: (typeof e.className === 'string' ? e.className : '').slice(0, 60),
    })),
    buttons: Array.from(document.querySelectorAll('button')).filter(vis).map(e => ({
      text: (e.innerText || '').trim().slice(0, 40),
      type: e.type, id: e.id,
      cls: (typeof e.className === 'string' ? e.className : '').slice(0, 60),
      disabled: e.disabled,
    })),
    links: Array.from(document.querySelectorAll('a')).filter(vis)
      .map(e => ({ text: (e.innerText || '').trim().slice(0, 30), href: (e.href || '').slice(0, 80) })),
    forms: Array.from(document.querySelectorAll('form')).map(f => ({
      id: f.id, action: (f.action || '').slice(0, 80), method: f.method,
    })),
    bodyText: (document.body.innerText || '').slice(0, 800),
  };
}
"""


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--index", type=int, default=7)
    args = ap.parse_args()

    accs = load_accounts(MAIL_FILE)
    acc = accs[args.index]
    print(f"邮箱: {acc.email}\n")

    if not acc.email:
        return 2

    from playwright.async_api import async_playwright

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True, proxy={"server": PROXY},
                                           args=["--ignore-certificate-errors"])
        ctx = await browser.new_context(locale="en-US", ignore_https_errors=True,
                                        viewport={"width": 1440, "height": 900})
        page = await ctx.new_page()
        page.set_default_timeout(30000)

        # 走到验证码页（滑块用 2Captcha）
        from app.net.twocaptcha_dragdrop import TwoCaptchaDragDrop
        from app.register.aliyun_slider import AliyunSliderSolver

        await page.goto(SIGNUP_URL, wait_until="domcontentloaded", timeout=60000)
        await asyncio.sleep(3)
        await page.locator("#basic_firstName").fill("James")
        await page.locator("#basic_lastName").fill("Smith")
        eb = page.locator("#basic_email")
        await eb.click()
        for ch in acc.email:
            await eb.type(ch, delay=25)
        cb = page.locator("input[type=checkbox]").first
        if not await cb.is_checked():
            await cb.check(force=True)
        await asyncio.sleep(0.5)
        await page.get_by_role("button", name="Continue").click()
        await asyncio.sleep(4)

        pw_el = page.locator("input#basic_password, input[autocomplete=new-password]").first
        if await pw_el.count() > 0:
            await pw_el.click()
            for ch in "OtpDiag123!x":
                await pw_el.type(ch, delay=25)
            await asyncio.sleep(0.5)
            await page.get_by_role("button", name="Continue").click()
            await asyncio.sleep(5)

        # 滑块
        paid = TwoCaptchaDragDrop(os.environ.get("TWOCAPTCHA_KEY", ""), proxy=PROXY)
        solver = AliyunSliderSolver(page, human=True, paid_client=paid,
                                    prefer_paid=True, proxy=PROXY)
        r = await solver.solve(max_attempts=5)
        print(f"滑块: ok={r.ok} attempts={r.attempts} target={r.target_x} "
              f"method={r.method}\n")
        if not r.ok:
            await browser.close()
            return 2

        await asyncio.sleep(3)
        d = await page.evaluate(DUMP)
        print("=== 验证码页结构 ===")
        print(f"URL: {d['url']}")
        print(f"\n输入框 ({len(d['inputs'])} 个):")
        for i in d["inputs"]:
            print(f"  {json.dumps(i, ensure_ascii=False)}")
        print(f"\n按钮 ({len(d['buttons'])} 个):")
        for b in d["buttons"]:
            print(f"  {json.dumps(b, ensure_ascii=False)}")
        print(f"\n链接 ({len(d['links'])} 个):")
        for l in d["links"]:
            print(f"  {json.dumps(l, ensure_ascii=False)}")
        print(f"\n表单: {json.dumps(d['forms'], ensure_ascii=False)}")
        print(f"\n正文:\n{d['bodyText']}")

        # 尝试填入一个错误码，看是否有自动提交
        print("\n=== 试填 6 位看行为 ===")
        try:
            first = page.locator("input").first
            await first.click()
            for ch in "111111":
                await page.keyboard.type(ch, delay=60)
            await asyncio.sleep(3)
            d2 = await page.evaluate(DUMP)
            print(f"填完后 URL: {d2['url']}")
            print(f"正文: {d2['bodyText'][:400]}")
            print(f"输入框值长度: "
                  f"{[i['valueLen'] for i in d2['inputs']]}")
        except Exception as exc:
            print(f"填码失败: {exc}")

        await browser.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
