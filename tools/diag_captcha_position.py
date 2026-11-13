"""诊断阿里云 FeiLin 缺口定位：抓真实图做像素分析。

目的：搞清楚
1. 截图尺寸 vs 显示尺寸的实际比例
2. 拼图块的真实形态（是不是全高竖条、有没有透明边）
3. 背景图的列亮度曲线长什么样，真缺口在哪
4. 现有算法定位到了哪里、偏了多少

产出：
  data/captcha_diag/bg.png      背景图
  data/captcha_diag/piece.png   拼图块
  data/captcha_diag/analysis.txt 数值分析
  data/captcha_diag/column_profile.csv 列曲线（可用 Excel 画图）

用法：
    python tools/diag_captcha_position.py
"""

from __future__ import annotations

import asyncio
import io
import json
import sys
from pathlib import Path

import numpy as np
from PIL import Image

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.register import captcha_vision  # noqa: E402

SIGNUP_URL = "https://qoder.com/users/sign-up"
PROXY = "http://127.0.0.1:7897"
OUT = ROOT / "data" / "captcha_diag"


async def grab() -> tuple[Image.Image, Image.Image, dict]:
    from playwright.async_api import async_playwright

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
        for ch in "diag_cap_01@outlook.com":
            await eb.type(ch, delay=35)
        cb = page.locator("input[type=checkbox]").first
        if not await cb.is_checked():
            await cb.check(force=True)
        await asyncio.sleep(0.8)
        await page.get_by_role("button", name="Continue").click()
        await asyncio.sleep(4)

        pw_el = page.locator("input#basic_password, input[autocomplete=new-password]").first
        if await pw_el.count() > 0:
            await pw_el.click()
            for ch in "DiagPass123!x":
                await pw_el.type(ch, delay=30)
            await asyncio.sleep(0.8)
            await page.get_by_role("button", name="Continue").click()
            await asyncio.sleep(5)

        # 关键：验证码不会自动展开，必须先点 "Click to verify"，
        # 否则 #aliyunCaptcha-img 不可见，screenshot 会超时报 "element is not visible"
        print("[diag] 展开验证码...")
        for attempt in range(4):
            st = await page.evaluate(
                """() => {
                  const win = document.querySelector('#aliyunCaptcha-window-float');
                  const i = document.querySelector('#aliyunCaptcha-img');
                  return {
                    winShown: !!(win && win.classList.contains('window-show') &&
                                 win.offsetParent !== null),
                    imgVisible: !!(i && i.getBoundingClientRect().width > 50),
                  };
                }""")
            if st.get("winShown") and st.get("imgVisible"):
                break
            clicked = False
            for sel in ["#captcha-element", "text=Click to verify",
                        "#aliyunCaptcha-captcha-left"]:
                try:
                    loc = page.locator(sel).first
                    if await loc.count() > 0 and await loc.is_visible():
                        await loc.click(timeout=5000)
                        clicked = True
                        print(f"[diag] 点击 {sel}")
                        break
                except Exception:
                    continue
            if not clicked:
                await page.mouse.click(720, 450)
            await asyncio.sleep(2)

        # 等滑块图就绪
        for _ in range(20):
            ok = await page.evaluate(
                """() => {
                  const i = document.querySelector('#aliyunCaptcha-img');
                  const p = document.querySelector('#aliyunCaptcha-puzzle');
                  return !!(i && p && i.complete && i.naturalWidth > 0 &&
                            p.complete && p.naturalWidth > 0 &&
                            i.getBoundingClientRect().width > 50);
                }""")
            if ok:
                break
            await asyncio.sleep(0.5)

        geo = await page.evaluate(
            """() => {
              const g = (s) => document.querySelector(s);
              const box = (e) => {
                if (!e) return null;
                const r = e.getBoundingClientRect();
                return {x: r.x, y: r.y, w: r.width, h: r.height};
              };
              const i = g('#aliyunCaptcha-img');
              const p = g('#aliyunCaptcha-puzzle');
              return {
                bgBox: box(i), pzBox: box(p),
                bgNatural: i ? {w: i.naturalWidth, h: i.naturalHeight} : null,
                pzNatural: p ? {w: p.naturalWidth, h: p.naturalHeight} : null,
                dpr: window.devicePixelRatio,
              };
            }""")

        bg_png = await page.locator("#aliyunCaptcha-img").screenshot(type="png")
        pz_png = await page.locator("#aliyunCaptcha-puzzle").screenshot(type="png")

        await browser.close()

    OUT.mkdir(parents=True, exist_ok=True)
    bg = Image.open(io.BytesIO(bg_png))
    pz = Image.open(io.BytesIO(pz_png))
    bg.save(OUT / "bg.png")
    pz.save(OUT / "piece.png")
    return bg, pz, geo


def analyze(bg: Image.Image, pz: Image.Image, geo: dict) -> None:
    lines: list[str] = []

    def w(s: str = "") -> None:
        lines.append(s)
        print(s)

    w("=== 尺寸 ===")
    w(f"  背景截图   : {bg.size}  mode={bg.mode}")
    w(f"  拼图截图   : {pz.size}  mode={pz.mode}")
    w(f"  bgBox(显示): {geo.get('bgBox')}")
    w(f"  pzBox(显示): {geo.get('pzBox')}")
    w(f"  bgNatural  : {geo.get('bgNatural')}")
    w(f"  pzNatural  : {geo.get('pzNatural')}")
    w(f"  devicePixelRatio: {geo.get('dpr')}")

    bg_box_w = (geo.get("bgBox") or {}).get("w") or 300
    scale = bg.size[0] / bg_box_w
    w(f"  => 截图/显示 scale = {scale:.4f}")

    w("\n=== 拼图块形态 ===")
    arr = np.asarray(pz)
    w(f"  shape={arr.shape}")
    if arr.ndim == 3 and arr.shape[2] in (2, 4):
        alpha = arr[:, :, -1]
        opaque_cols = (alpha > 32).sum(axis=0)
        nonzero = int((opaque_cols > 0).sum())
        w(f"  有 alpha 通道，透明列数={arr.shape[1] - nonzero}，不透明列数={nonzero}")
        # 不透明区域的 x 范围
        xs = np.where(opaque_cols > 0)[0]
        if len(xs):
            w(f"  不透明 x 范围: {xs.min()}..{xs.max()}  宽={xs.max()-xs.min()+1}")
        # 每列不透明像素数分布（看是否全高）
        w(f"  每列不透明像素数: min={opaque_cols.min()} max={opaque_cols.max()} "
          f"median={int(np.median(opaque_cols))}")
        full_h = arr.shape[0]
        full_cols = int((opaque_cols >= full_h * 0.95).sum())
        w(f"  接近全高(>=95%)的列数: {full_cols} / {arr.shape[1]}")
    else:
        w("  无 alpha 通道（RGB），整块都是内容")

    w("\n=== 背景列亮度分析 ===")
    g = np.asarray(bg.convert("L"), dtype=np.float64)
    h, wd = g.shape
    y0, y1 = int(h * 0.08), int(h * 0.92)
    band = g[y0:y1, :]
    col = band.mean(axis=0)
    col_s = captcha_vision._box(col, 1)

    med = float(np.median(col_s))
    w(f"  列均值中位数: {med:.2f}")
    w(f"  列均值范围: {col_s.min():.2f} .. {col_s.max():.2f}")

    dev = col_s - med
    pos = np.clip(dev, 0, None)
    # 找所有显著峰
    thr = pos.max() * 0.45
    mask = pos >= thr
    runs = []
    i = 0
    while i < wd:
        if not mask[i]:
            i += 1
            continue
        j = i
        while j < wd and mask[j]:
            j += 1
        if j - i >= 4:
            runs.append((i, j - 1, float(pos[i:j].sum())))
        i = j
    runs.sort(key=lambda r: r[2], reverse=True)
    w(f"  显著区间（按强度）: {[(a, b, round(s,1)) for a,b,s in runs[:6]]}")

    # 导列曲线
    csv = OUT / "column_profile.csv"
    with csv.open("w", encoding="utf-8") as fh:
        fh.write("x,col_mean,dev\n")
        for x in range(wd):
            fh.write(f"{x},{col_s[x]:.3f},{dev[x]:.3f}\n")
    w(f"  列曲线已导出: {csv}")

    w("\n=== 现有算法结果 ===")
    r = captcha_vision.solve(bg, pz, display_w=bg_box_w)
    w(f"  x(显示坐标) = {r['x']}")
    w(f"  x_px(截图)  = {r.get('x_px')}")
    w(f"  method      = {r['method']}")
    w(f"  conf        = {r['conf']}")
    w(f"  run_w       = {r.get('run_w')}  piece_w={r.get('piece_w')}")
    w(f"  width_ok    = {r.get('width_ok')}  agree={r.get('agree')}")
    w(f"  scale       = {r.get('scale')}")

    w("\n=== 第二证据（列方差）===")
    sup = captcha_vision.find_gap_solid(bg)
    w(f"  {sup}")

    # 在背景图上标出候选位置，保存可视化
    vis = bg.convert("RGB").copy()
    from PIL import ImageDraw
    d = ImageDraw.Draw(vis)
    for idx, (a, b, s) in enumerate(runs[:4]):
        color = [(255, 0, 0), (0, 160, 255), (0, 200, 0), (255, 180, 0)][idx]
        d.rectangle([a, 0, b, vis.size[1] - 1], outline=color, width=2)
        d.text((a + 2, 4), str(idx), fill=color)
    if r["x"] >= 0:
        x_px = int(r["x"] * scale)
        d.line([x_px, 0, x_px, vis.size[1]], fill=(255, 0, 255), width=2)
    vis.save(OUT / "bg_marked.png")
    w(f"\n已标注候选区间和算法结果: {OUT / 'bg_marked.png'}")

    (OUT / "analysis.txt").write_text("\n".join(lines), encoding="utf-8")
    print(f"\n分析已写入 {OUT / 'analysis.txt'}")


def main() -> int:
    bg, pz, geo = asyncio.run(grab())
    analyze(bg, pz, geo)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
