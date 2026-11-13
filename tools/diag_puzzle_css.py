"""dump 拼图块和背景图的完整 CSS，找出缺口线索。

重点看：
1. puzzle 是 <img> 还是 background-image？
2. 有没有 clip-path / mask，可能只显示中间一块
3. 背景图上缺口的视觉特征

用法：
    python tools/diag_puzzle_css.py
"""

from __future__ import annotations

import asyncio
import base64
import io
import json
import sys
from pathlib import Path

import numpy as np
from PIL import Image

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

SIGNUP_URL = "https://qoder.com/users/sign-up"
PROXY = "http://127.0.0.1:7897"
OUT = ROOT / "data" / "puzzle_css"


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

        await page.goto(SIGNUP_URL, wait_until="domcontentloaded", timeout=60000)
        await asyncio.sleep(3)
        await page.locator("#basic_firstName").fill("James")
        await page.locator("#basic_lastName").fill("Smith")
        eb = page.locator("#basic_email")
        await eb.click()
        for ch in "css_probe_01@outlook.com":
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
            for ch in "CssPass123!x":
                await pw_el.type(ch, delay=28)
            await asyncio.sleep(0.6)
            await page.get_by_role("button", name="Continue").click()
            await asyncio.sleep(5)

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
              return !!(i && p && i.complete && p.complete && i.naturalWidth > 0);
            }""", timeout=20000)

        info = await page.evaluate(
            """() => {
              const p = document.querySelector('#aliyunCaptcha-puzzle');
              const i = document.querySelector('#aliyunCaptcha-img');
              const ps = getComputedStyle(p);
              const is_ = getComputedStyle(i);
              const pick = (s) => ({
                tag: s ? s.tagName : null,
                backgroundImage: s ? s.backgroundImage.slice(0, 200) : null,
                backgroundSize: s ? s.backgroundSize : null,
                backgroundPosition: s ? s.backgroundPosition : null,
                backgroundRepeat: s ? s.backgroundRepeat : null,
                clipPath: s ? s.clipPath : null,
                maskImage: s ? (s.maskImage || s.webkitMaskImage || '').slice(0, 120) : null,
                width: s ? s.width : null, height: s ? s.height : null,
                transform: s ? s.transform : null,
                position: s ? s.position : null,
                left: s ? s.left : null, top: s ? s.top : null,
                zIndex: s ? s.zIndex : null,
                opacity: s ? s.opacity : null,
              });
              return {
                puzzle: pick(ps),
                bg: pick(is_),
                puzzleSrc: p ? (p.src || '') : null,
                bgSrc: i ? (i.src || '') : null,
                puzzleOuterHTML: p ? p.outerHTML.slice(0, 300) : null,
                bgOuterHTML: i ? i.outerHTML.slice(0, 300) : null,
              };
            }""")

        print("=== puzzle CSS ===")
        print(json.dumps(info["puzzle"], ensure_ascii=False, indent=2))
        print("\n=== bg CSS ===")
        print(json.dumps(info["bg"], ensure_ascii=False, indent=2))
        print(f"\npuzzle outerHTML: {info['puzzleOuterHTML']}")
        print(f"\nbg outerHTML: {info['bgOuterHTML']}")

        # 保存原始图片（不截图，直接从 data URL 解码，避免渲染干扰）
        for name in ("puzzle", "bg"):
            src = info[f"{name}Src"] or ""
            if src.startswith("data:") and "," in src:
                header, payload = src.split(",", 1)
                if "base64" in header:
                    raw = base64.b64decode(payload + "=" * ((-len(payload)) % 4))
                    im = Image.open(io.BytesIO(raw))
                    im.save(OUT / f"{name}_raw.png")
                    print(f"\n{name} 原始图: {im.size} {im.mode} → {OUT / f'{name}_raw.png'}")

        # 分析 puzzle 原始图
        pr = OUT / "puzzle_raw.png"
        if pr.exists():
            im = Image.open(pr)
            arr = np.asarray(im)
            print("\n=== puzzle 原始图分析 ===")
            print(f"  size={im.size} mode={im.mode} shape={arr.shape}")
            if arr.ndim == 3 and arr.shape[2] == 4:
                a = arr[:, :, 3]
                print(f"  alpha: min={a.min()} max={a.max()} 唯一值={len(np.unique(a))}")
                transparent = int((a < 32).sum())
                print(f"  透明像素={transparent} / {a.size} "
                      f"({100*transparent/a.size:.1f}%)")
                # 不透明区域的包围盒
                mask = a >= 32
                if mask.any():
                    ys, xs = np.where(mask)
                    print(f"  不透明区域 bbox: x={xs.min()}..{xs.max()} "
                          f"y={ys.min()}..{ys.max()}")
                    # 每列不透明像素数
                    per_col = mask.sum(axis=0)
                    nz = np.where(per_col > 0)[0]
                    print(f"  有内容的列: {nz.min()}..{nz.max()} 共 {len(nz)} 列")

        # 分析 bg 原始图
        br = OUT / "bg_raw.png"
        if br.exists():
            im = Image.open(br)
            g = np.asarray(im.convert("L"), dtype=np.float64)
            print(f"\n=== bg 原始图分析 ===")
            print(f"  size={im.size} shape={g.shape}")
            h, w = g.shape
            # 找"暗块"：缺口常为半透明暗色
            col_mean = g.mean(axis=0)
            print(f"  列均值: min={col_mean.min():.1f} max={col_mean.max():.1f}")
            # 找最暗的 52 宽窗口
            pw = 52
            if w > pw:
                scores = np.array([col_mean[x:x + pw].mean()
                                   for x in range(w - pw + 1)])
                order = np.argsort(scores)
                print(f"  最暗的 5 个窗口: "
                      f"{[(int(i), round(scores[i],1)) for i in order[:5]]}")
                order2 = np.argsort(scores)[::-1]
                print(f"  最亮的 5 个窗口: "
                      f"{[(int(i), round(scores[i],1)) for i in order2[:5]]}")

        await browser.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
