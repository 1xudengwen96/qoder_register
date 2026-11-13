"""Dump 阿里云滑块验证的 DOM 结构。

注册走到第三步会弹出「Click to verify」，需要先点一下才出现滑块。
本脚本自动走到第三步并 dump 滑块结构。

用法：
    python tools/probe_aliyun_slider.py
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

SIGNUP_URL = "https://qoder.com/users/sign-up"
PROXY = "http://127.0.0.1:7897"

DUMP_CAPTCHA = r"""
() => {
  const out = {aliyun: {}, nodes: [], images: [], canvas: [], scripts: []};
  // 所有 id/class 里带 aliyun 或 captcha 的元素
  document.querySelectorAll('*').forEach(e => {
    const id = e.id || '';
    const cls = (typeof e.className === 'string' ? e.className : '') || '';
    if (/aliyun/i.test(id) || /aliyun/i.test(cls) || /captcha/i.test(id)) {
      const r = e.getBoundingClientRect();
      out.nodes.push({
        tag: e.tagName, id, cls: cls.slice(0, 80),
        x: Math.round(r.x), y: Math.round(r.y),
        w: Math.round(r.width), h: Math.round(r.height),
        visible: !!(e.offsetWidth || e.offsetHeight)
      });
    }
    if (e.tagName === 'IMG') {
      out.images.push({id, src: (e.src || '').slice(0, 120), w: e.naturalWidth, h: e.naturalHeight});
    }
    if (e.tagName === 'CANVAS') {
      out.canvas.push({id, w: e.width, h: e.height});
    }
  });
  // iframe 里的脚本（阿里 SDK）
  document.querySelectorAll('script').forEach(s => {
    const src = s.src || '';
    if (/aliyun|captcha|feilin|saf/i.test(src)) out.scripts.push(src);
  });
  out.bodyText = (document.body.innerText || '').slice(0, 800);
  return out;
}
"""


async def run() -> int:
    from playwright.async_api import async_playwright

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True, proxy={"server": PROXY},
                                           args=["--ignore-certificate-errors"])
        ctx = await browser.new_context(locale="en-US", ignore_https_errors=True,
                                        viewport={"width": 1440, "height": 900})
        page = await ctx.new_page()
        page.set_default_timeout(30000)

        print(f"=== 1. 打开注册页 ===")
        await page.goto(SIGNUP_URL, wait_until="domcontentloaded", timeout=60000)
        await asyncio.sleep(4)

        print("=== 2. 第一步：姓名/邮箱/勾选 ===")
        await page.locator("#basic_firstName").fill("James")
        await page.locator("#basic_lastName").fill("Smith")
        eb = page.locator("#basic_email")
        await eb.click()
        for ch in "probe_slider_01@outlook.com":
            await eb.type(ch, delay=40)
        cb = page.locator("input[type=checkbox]").first
        if not await cb.is_checked():
            await cb.check(force=True)
        await asyncio.sleep(1)
        await page.get_by_role("button", name="Continue").click()
        await asyncio.sleep(5)

        print("=== 3. 第二步：密码 ===")
        pw_el = page.locator("input#basic_password, input[autocomplete=new-password]").first
        if await pw_el.count() > 0:
            await pw_el.click()
            for ch in "ProbePass123!x":
                await pw_el.type(ch, delay=35)
            await asyncio.sleep(1)
            await page.get_by_role("button", name="Continue").click()
            await asyncio.sleep(5)

        print("=== 4. 第三步：人机验证 ===")
        d1 = await page.evaluate(DUMP_CAPTCHA)
        print(f"  当前正文:\n{d1['bodyText']}")
        print(f"  匹配节点数: {len(d1['nodes'])}")

        # 点 "Click to verify"
        clicked = False
        for sel in ["text=Click to verify", "#captcha-element",
                    "div[class*=verify]", "button:has-text('verify')"]:
            try:
                loc = page.locator(sel).first
                if await loc.count() > 0 and await loc.is_visible():
                    await loc.click()
                    clicked = True
                    print(f"  已点击: {sel}")
                    break
            except Exception:
                continue
        if not clicked:
            print("  未找到 'Click to verify'，尝试点击页面中心")
            await page.mouse.click(720, 450)

        await asyncio.sleep(5)
        d2 = await page.evaluate(DUMP_CAPTCHA)

        print("\n=== 5. 滑块结构 dump ===")
        print(f"正文:\n{d2['bodyText']}\n")
        print("节点：")
        for n in d2["nodes"][:40]:
            print(f"  {json.dumps(n, ensure_ascii=False)}")
        print("\n图片：")
        for i in d2["images"]:
            print(f"  {json.dumps(i, ensure_ascii=False)}")
        print("\nCanvas：")
        for c in d2["canvas"]:
            print(f"  {json.dumps(c, ensure_ascii=False)}")
        print("\n相关脚本：")
        for s in d2["scripts"]:
            print(f"  {s}")

        # 截图留证
        shot = ROOT / "data" / "aliyun_slider.png"
        shot.parent.mkdir(parents=True, exist_ok=True)
        try:
            await page.screenshot(path=str(shot), full_page=True)
            print(f"\n截图：{shot}")
        except Exception:
            pass

        await browser.close()
    return 0


def main() -> int:
    return asyncio.run(run())


if __name__ == "__main__":
    raise SystemExit(main())
