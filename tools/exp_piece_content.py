"""关键实验：拼图块的图像内容是固定的，还是随位置动态切片？

这个结论决定整个识别策略：
  - 内容固定（= 缺口处的原图）→ 可以用模板匹配定位缺口
  - 内容随位置变化（= 背景图当前位置的切片）→ 模板匹配必然失败，
    必须改用「找背景图上最平坦/异常的区域」

做法：在同一个验证码上，把拼图块拖到几个不同位置，每次截图对比。

用法：
    python tools/exp_piece_content.py
"""

from __future__ import annotations

import asyncio
import hashlib
import io
import sys
from pathlib import Path

import numpy as np
from PIL import Image

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

SIGNUP_URL = "https://qoder.com/users/sign-up"
PROXY = "http://127.0.0.1:7897"
OUT = ROOT / "data" / "piece_exp"


async def main() -> int:
    from playwright.async_api import async_playwright

    OUT.mkdir(parents=True, exist_ok=True)

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True, proxy={"server": PROXY},
                                           args=["--ignore-certificate-errors"])
        ctx = await browser.new_context(locale="en-US", ignore_https_errors=True,
                                        viewport={"width": 1440, "height": 900})
        page = await ctx.new_page()
        page.set_default_timeout(30000)

        # ---- 走到滑块 ----
        await page.goto(SIGNUP_URL, wait_until="domcontentloaded", timeout=60000)
        await asyncio.sleep(3)
        await page.locator("#basic_firstName").fill("James")
        await page.locator("#basic_lastName").fill("Smith")
        eb = page.locator("#basic_email")
        await eb.click()
        for ch in "exp_piece_01@outlook.com":
            await eb.type(ch, delay=30)
        cb = page.locator("input[type=checkbox]").first
        if not await cb.is_checked():
            await cb.check(force=True)
        await asyncio.sleep(0.6)
        await page.get_by_role("button", name="Continue").click()
        await asyncio.sleep(4)
        pw_el = page.locator("input#basic_password, input[autocomplete=new-password]").first
        if await pw_el.count() > 0:
            await pw_el.click()
            for ch in "ExpPass123!x":
                await pw_el.type(ch, delay=28)
            await asyncio.sleep(0.6)
            await page.get_by_role("button", name="Continue").click()
            await asyncio.sleep(5)

        # 展开
        for _ in range(4):
            st = await page.evaluate(
                """() => {
                  const w = document.querySelector('#aliyunCaptcha-window-float');
                  const i = document.querySelector('#aliyunCaptcha-img');
                  return !!(w && w.classList.contains('window-show') &&
                            i && i.getBoundingClientRect().width > 50);
                }""")
            if st:
                break
            for sel in ["#captcha-element", "#aliyunCaptcha-captcha-left"]:
                try:
                    loc = page.locator(sel).first
                    if await loc.count() > 0 and await loc.is_visible():
                        await loc.click(timeout=4000)
                        break
                except Exception:
                    pass
            await asyncio.sleep(1.8)

        await page.wait_for_function(
            """() => {
              const i = document.querySelector('#aliyunCaptcha-img');
              const p = document.querySelector('#aliyunCaptcha-puzzle');
              return !!(i && p && i.complete && p.complete &&
                        i.naturalWidth > 0 && p.naturalWidth > 0);
            }""", timeout=20000)

        bg = Image.open(io.BytesIO(
            await page.locator("#aliyunCaptcha-img").screenshot(type="png")))
        bg.save(OUT / "bg.png")

        def h(img: Image.Image) -> str:
            return hashlib.md5(np.asarray(img.convert("RGB")).tobytes()).hexdigest()[:12]

        # ---- 逐步拖动，每步截图拼图块 ----
        cdp = await ctx.new_cdp_session(page)
        geo = await page.evaluate(
            """() => {
              const g = (s) => document.querySelector(s);
              const b = (e) => { const r = e.getBoundingClientRect();
                return {x: r.x, y: r.y, w: r.width, h: r.height}; };
              return {
                slider: b(g('#aliyunCaptcha-sliding-slider')),
                track: b(g('#aliyunCaptcha-sliding-body')),
                pz: b(g('#aliyunCaptcha-puzzle')),
                bg: b(g('#aliyunCaptcha-img')),
              };
            }""")

        sl = geo["slider"]
        tr = geo["track"]
        sx, sy = sl["x"] + sl["w"] / 2, sl["y"] + sl["h"] / 2
        print(f"滑块中心 ({sx:.0f},{sy:.0f})  轨道宽 {tr['w']:.0f}  "
              f"背景宽 {geo['bg']['w']:.0f}  拼图宽 {geo['pz']['w']:.0f}")

        async def mouse(kind, x, y, buttons=0):
            p = {"type": kind, "x": float(x), "y": float(y),
                 "modifiers": 0, "pointerType": "mouse"}
            if kind in ("mousePressed", "mouseReleased", "mouseMoved"):
                p["button"] = "left"
                p["buttons"] = buttons
            if kind == "mousePressed":
                p["clickCount"] = 1
            await cdp.send("Input.dispatchMouseEvent", p)

        # 按下
        await mouse("mouseMoved", sx, sy)
        await asyncio.sleep(0.1)
        await mouse("mousePressed", sx, sy, 1)
        await asyncio.sleep(0.15)

        results = []
        # 逐步向右移动，每次记录 puzzle 截图 + style.left
        for step in range(0, 201, 40):
            cur = sx + step
            await mouse("mouseMoved", cur, sy, 1)
            await asyncio.sleep(0.35)

            left = await page.evaluate(
                "() => parseFloat(document.querySelector('#aliyunCaptcha-puzzle')"
                ".style.left || '0')")
            png = await page.locator("#aliyunCaptcha-puzzle").screenshot(type="png")
            img = Image.open(io.BytesIO(png))
            img.save(OUT / f"pz_step{step:03d}.png")
            digest = h(img)
            arr = np.asarray(img.convert("L"), dtype=np.float64)
            results.append((step, left, digest, float(arr.mean()), float(arr.std())))
            print(f"  步进 {step:3d}px → style.left={left:6.1f}  "
                  f"md5={digest}  mean={arr.mean():6.1f} std={arr.std():5.1f}")

        await mouse("mouseReleased", sx + 200, sy, 0)
        await asyncio.sleep(1)

        # ---- 结论 ----
        print("\n=== 结论 ===")
        digests = {r[2] for r in results}
        lefts = {round(r[1]) for r in results}
        if len(digests) == 1:
            print("  拼图块图像 **完全不变**（同一个 md5）")
            print("  → 内容是固定的那块图，可以用模板匹配")
        else:
            print(f"  拼图块图像 **随位置变化**（{len(digests)} 种不同 md5）")
            print("  → 是背景图当前位置的动态切片，模板匹配必然失败")
            print("  → 必须改用「找背景图上最平坦/异常的区域」")
        print(f"  style.left 取值: {sorted(lefts)}")

        # 顺便看拼图块的初始内容是否等于背景 left=0 条带
        pz0 = Image.open(OUT / "pz_step000.png").convert("L")
        pw_ = pz0.size[0]
        bgL = bg.convert("L").crop((0, 0, pw_, bg.size[1]))
        if pz0.size[1] != bgL.size[1]:
            hh = min(pz0.size[1], bgL.size[1])
            pz0c = pz0.crop((0, 0, pw_, hh))
            bgLc = bgL.crop((0, 0, pw_, hh))
        else:
            pz0c, bgLc = pz0, bgL
        d = np.abs(np.asarray(pz0c, dtype=np.float64) -
                   np.asarray(bgLc, dtype=np.float64))
        print(f"\n  拼图块 vs 背景 left 条带：平均绝对差={d.mean():.2f} "
              f"max={d.max():.0f}")
        if d.mean() < 3:
            print("  → 两者几乎相同，证实拼图块就是背景该位置的切片")

        await browser.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
