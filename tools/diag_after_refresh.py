"""确认：refresh 之后 puzzle/bg 的 src 变成什么。

上一轮实测：点 refresh 换题后，src 解码失败。
推测 src 从 data:base64 变成了远程 http URL（带签名/时效）。

用法：
    python tools/diag_after_refresh.py
"""

from __future__ import annotations

import asyncio
import base64
import io
import sys
from pathlib import Path

from PIL import Image

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

SIGNUP_URL = "https://qoder.com/users/sign-up"
PROXY = "http://127.0.0.1:7897"
OUT = ROOT / "data" / "refresh_diag"

GET_SRC = """() => {
  const p = document.querySelector('#aliyunCaptcha-puzzle');
  const i = document.querySelector('#aliyunCaptcha-img');
  const desc = (e) => {
    if (!e) return null;
    const s = e.src || '';
    return {
      src: s,
      isData: s.startsWith('data:'),
      len: s.length,
      prefix: s.slice(0, 120),
      natural: [e.naturalWidth, e.naturalHeight],
      complete: e.complete,
    };
  };
  return {puzzle: desc(p), bg: desc(i)};
}"""


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

        # 走到滑块
        await page.goto(SIGNUP_URL, wait_until="domcontentloaded", timeout=60000)
        await asyncio.sleep(3)
        await page.locator("#basic_firstName").fill("James")
        await page.locator("#basic_lastName").fill("Smith")
        eb = page.locator("#basic_email")
        await eb.click()
        for ch in "refresh_diag_01@outlook.com":
            await eb.type(ch, delay=28)
        cb = page.locator("input[type=checkbox]").first
        if not await cb.is_checked():
            await cb.check(force=True)
        await asyncio.sleep(0.6)
        await page.get_by_role("button", name="Continue").click()
        await asyncio.sleep(4)
        pw_el = page.locator("input#basic_password, input[autocomplete=new-password]").first
        if await pw_el.count() > 0:
            await pw_el.click()
            for ch in "RefDiag123!x":
                await pw_el.type(ch, delimiter := None, delay=28) if False else \
                    await pw_el.type(ch, delay=28)
            await asyncio.sleep(0.6)
            await page.get_by_role("button", name="Continue").click()
            await asyncio.sleep(5)

        # 展开
        for _ in range(4):
            ok = await page.evaluate(
                """() => {
                  const w = document.querySelector('#aliyunCaptcha-window-float');
                  const i = document.querySelector('#aliyunCaptcha-img');
                  return !!(w && w.classList.contains('window-show') &&
                            i && i.getBoundingClientRect().width > 50);
                }""")
            if ok:
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

        await asyncio.sleep(1)

        print("=== 初始状态 ===")
        s0 = await page.evaluate(GET_SRC)
        for k in ("puzzle", "bg"):
            d = s0[k] or {}
            print(f"  {k}: isData={d.get('isData')} len={d.get('len')} "
                  f"natural={d.get('natural')}")
            print(f"        prefix={str(d.get('prefix'))[:90]}")

        # 保存初始图
        for k in ("puzzle", "bg"):
            src = (s0[k] or {}).get("src", "")
            if src.startswith("data:"):
                payload = src.split(",", 1)[1]
                raw = base64.b64decode(payload + "=" * ((-len(payload)) % 4))
                Image.open(io.BytesIO(raw)).save(OUT / f"{k}_initial.png")

        # ---- 点 refresh ----
        print("\n=== 点击 refresh 换题 ===")
        try:
            rf = page.locator("#aliyunCaptcha-btn-refresh")
            if await rf.count() > 0:
                await rf.click()
                print("  已点击 refresh")
            else:
                print("  找不到 refresh 按钮")
        except Exception as exc:
            print(f"  点击失败: {exc}")

        # 观察 src 变化
        for wait in (1, 2, 4, 7):
            await asyncio.sleep(wait)
            s1 = await page.evaluate(GET_SRC)
            print(f"\n  --- 等待 {wait}s 后 ---")
            for k in ("puzzle", "bg"):
                d = s1[k] or {}
                print(f"    {k}: isData={d.get('isData')} len={d.get('len')} "
                      f"natural={d.get('natural')} complete={d.get('complete')}")
                print(f"          prefix={str(d.get('prefix'))[:100]}")

        # 保存 refresh 后的图（尝试 data URL；否则用截图）
        s2 = await page.evaluate(GET_SRC)
        for k in ("puzzle", "bg"):
            src = (s2[k] or {}).get("src", "")
            if src.startswith("data:"):
                payload = src.split(",", 1)[1]
                raw = base64.b64decode(payload + "=" * ((-len(payload)) % 4))
                Image.open(io.BytesIO(raw)).save(OUT / f"{k}_after_refresh.png")
                print(f"\n{k} 换题后仍是 data URL，已保存")
            else:
                print(f"\n{k} 换题后 **不是** data URL：{src[:120]}")
                # 用截图兜底
                try:
                    png = await page.locator(f"#{('aliyunCaptcha-puzzle' if k=='puzzle' else 'aliyunCaptcha-img')}").screenshot(type="png", timeout=8000)
                    Image.open(io.BytesIO(png)).save(OUT / f"{k}_after_refresh_shot.png")
                    print(f"   已用截图保存（注意：截图会丢 alpha）")
                except Exception as exc:
                    print(f"   截图也失败: {exc}")

        # 检查是否有 canvas 承载图像
        canvas_info = await page.evaluate(
            """() => {
              const out = [];
              document.querySelectorAll('canvas').forEach(c => {
                if (c.width > 20) out.push({
                  id: c.id, cls: (c.className||'').toString().slice(0,40),
                  w: c.width, h: c.height,
                  visible: !!(c.offsetWidth || c.offsetHeight)
                });
              });
              return out;
            }""")
        print(f"\n=== 页面上的 canvas ===\n  {canvas_info}")

        await browser.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
