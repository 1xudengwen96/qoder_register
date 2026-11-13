"""抓取验证码提交过程的网络请求。

目的：填完 6 位后，到底有没有发出验证请求？
- 有 → 看请求内容和响应，判断是码错了还是别的问题
- 没有 → 说明 React 没触发提交，要找触发方式

用法：
    python tools/diag_otp_network.py --index 14
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
from app.register.qoder_signup import wait_code_from_mailbox  # noqa: E402

MAIL_FILE = "D:/Backup/Downloads/outlook.txt"
PROXY = "http://127.0.0.1:7897"
SIGNUP_URL = "https://qoder.com/users/sign-up"
OUT = ROOT / "data" / "otp_net"


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--index", type=int, default=14)
    args = ap.parse_args()

    accs = load_accounts(MAIL_FILE)
    acc = accs[args.index]
    print(f"邮箱: {acc.email}\n")

    box = OutlookMailbox(acc, proxy=PROXY)
    box.refresh_access_token()
    box.connect()
    box.select("INBOX")
    baseline = {u.decode() for u in box.list_uids()}
    print(f"基线 {len(baseline)} 封\n")

    from playwright.async_api import async_playwright

    OUT.mkdir(parents=True, exist_ok=True)
    reqs: list[dict] = []

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True, proxy={"server": PROXY},
                                           args=["--ignore-certificate-errors"])
        ctx = await browser.new_context(locale="en-US", ignore_https_errors=True,
                                        viewport={"width": 1440, "height": 900})
        page = await ctx.new_page()
        page.set_default_timeout(30000)

        # 监听所有非静态资源请求
        def on_req(req):
            u = req.url
            if any(k in u for k in (".js", ".css", ".png", ".jpg", ".woff",
                                    ".svg", ".ico", ".gif")):
                return
            if "aliyuncs" in u or "alicdn" in u or "bing" in u or "google" in u:
                return
            reqs.append({
                "phase": "req", "method": req.method, "url": u,
                "post": (req.post_data or "")[:800],
            })

        async def on_resp(resp):
            u = resp.url
            if any(k in u for k in (".js", ".css", ".png", ".jpg", ".woff",
                                    ".svg", ".ico", ".gif")):
                return
            if "aliyuncs" in u or "alicdn" in u or "bing" in u or "google" in u:
                return
            rec = {"phase": "resp", "status": resp.status, "url": u}
            try:
                body = await resp.text()
                rec["body"] = body[:600]
            except Exception:
                pass
            reqs.append(rec)

        page.on("request", on_req)
        page.on("response", lambda r: asyncio.create_task(on_resp(r)))

        # 走到滑块
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
            for ch in "NetDiag123!x":
                await pw_el.type(ch, delay=25)
            await asyncio.sleep(0.5)
            await page.get_by_role("button", name="Continue").click()
            await asyncio.sleep(5)

        paid = TwoCaptchaDragDrop(os.environ.get("TWOCAPTCHA_KEY", ""), proxy=PROXY)
        solver = AliyunSliderSolver(page, human=True, paid_client=paid,
                                    prefer_paid=True, proxy=PROXY)
        r = await solver.solve(max_attempts=6)
        print(f"滑块: ok={r.ok} attempts={r.attempts} target={r.target_x}\n")
        if not r.ok:
            await browser.close()
            box.close()
            return 2

        await asyncio.sleep(3)
        reqs.clear()   # 只看填码之后的
        print("=== 等验证码 ===")
        code = await wait_code_from_mailbox(box, timeout=180, poll=4,
                                           baseline_uids=baseline)
        print(f"验证码: {code}\n")
        if not code:
            await browser.close()
            box.close()
            return 2

        # 填码
        loc = page.locator("input[autocomplete='one-time-code']").first
        await loc.click()
        await loc.fill("")
        print("=== 用 CDP 键盘输入 ===")
        cdp = await ctx.new_cdp_session(page)
        for ch in code:
            await cdp.send("Input.dispatchKeyEvent", {
                "type": "keyDown", "text": ch, "key": ch,
                "code": f"Digit{ch}",
                "windowsVirtualKeyCode": ord(ch),
                "nativeVirtualKeyCode": ord(ch)})
            await cdp.send("Input.dispatchKeyEvent", {
                "type": "keyUp", "key": ch, "code": f"Digit{ch}",
                "windowsVirtualKeyCode": ord(ch),
                "nativeVirtualKeyCode": ord(ch)})
            await asyncio.sleep(0.1)

        print(f"填后 value = {await loc.input_value()}")
        await asyncio.sleep(5)

        print("\n=== 填码后的网络请求 ===")
        if not reqs:
            print("  （没有任何请求）→ React 没触发提交")
        for r in reqs:
            if r["phase"] == "req":
                print(f"[REQ ] {r['method']} {r['url'][:110]}")
                if r.get("post"):
                    print(f"       body: {r['post'][:300]}")
            else:
                print(f"[RESP] {r['status']} {r['url'][:110]}")
                if r.get("body"):
                    print(f"       body: {r['body'][:300]}")

        # 再试按 Enter
        print("\n=== 试按 Enter ===")
        reqs.clear()
        try:
            await page.keyboard.press("Enter")
            await asyncio.sleep(4)
        except Exception as exc:
            print(f"  Enter 失败: {exc}")
        if reqs:
            for r in reqs:
                if r["phase"] == "req":
                    print(f"[REQ ] {r['method']} {r['url'][:110]}")
        else:
            print("  （按 Enter 后也没请求）")

        # 试点页面上的可疑元素
        print("\n=== 页面可点元素 ===")
        clickables = await page.evaluate(
            """() => {
              const vis = (e) => !!(e.offsetWidth || e.offsetHeight);
              return Array.from(document.querySelectorAll(
                  'button, [role=button], [class*=btn], [class*=submit], a'))
                .filter(vis).map(e => ({
                  tag: e.tagName, text: (e.innerText||'').trim().slice(0,30),
                  cls: (typeof e.className==='string'?e.className:'').slice(0,50),
                  id: e.id,
                })).slice(0, 20);
            }""")
        for c in clickables:
            print(f"  {json.dumps(c, ensure_ascii=False)}")

        # 保存现场
        (OUT / f"reqs_{code}.json").write_text(
            json.dumps(reqs, ensure_ascii=False, indent=2), encoding="utf-8")
        await page.screenshot(path=str(OUT / f"otp_{code}.png"), full_page=True)
        print(f"\n已保存: {OUT}/reqs_{code}.json, otp_{code}.png")

        await browser.close()
    box.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
