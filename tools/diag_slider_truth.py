"""滑块取证脚本：在留存环境里复现滑块，拿真值判定三件事。

背景（2026-11-17 run:one 十连败的现场证据）：
- 每轮 final = target + 7.5~8px（ratio=1.21 系统性过冲 ~3.3%）
- 2Captcha x>236 的答案全被钳到 248.3（limit = disp_w - pw*scale）
- 若把 x 当「左沿」，x=270/273/264 超过自然图宽(296)里左沿上限 245 → 不可能；
  若当「中心」，x=270 → 左沿 248.0，恰好落在钳制值上 → 高度可疑

本脚本做的事（全只读 + 一次受控拖拽实验，不改生产代码）：
1. 在留存的比特环境开新标签 → 注册页 → 复用已烧掉账号的邮箱走到滑块
2. 抓同一对 bg/pz 图：
   - 本地模板匹配（numpy masked SSD）算缺口真实左沿/顶沿
   - 同一对图发给 2Captcha，对比 x/y 与真值的对应关系（左沿 or 中心）
3. 受控拖拽实验：压住滑块走 +100/+200px，量 puzzle style.left 实际位移
   → 实测真实 ratio（对照代码里算出来的 1.21）
4. 打印 geometry（bgBox/pzBox/track/滑块/初始 left）
5. 结束不关标签页、不关环境（保持现场）
"""

from __future__ import annotations

import argparse
import time
from pathlib import Path

SIGNUP_URL = "https://qoder.com/users/sign-up"

_JS_CLICK_CONTINUE = """(() => {
  const vis = (e) => !!(e.offsetWidth || e.offsetHeight ||
                        e.getClientRects().length);
  const nodes = Array.from(document.querySelectorAll(
      'button, [role=button]')).filter(vis).filter(e => !e.disabled);
  const hit = nodes.find(e =>
      ((e.innerText || '').trim().toLowerCase() === 'continue'));
  if (!hit) return null;
  hit.scrollIntoView({block: 'center'});
  hit.click();
  return (hit.innerText || '').trim();
})()"""

_JS_RECT = """(sel) => {
  const e = document.querySelector(sel);
  if (!e) return null;
  const r = e.getBoundingClientRect();
  return {x: r.x, y: r.y, w: r.width, h: r.height, cx: r.x + r.width / 2,
          cy: r.y + r.height / 2};
}"""


def match_gap(bg, pz_c, mask_img):
    """masked SSD 模板匹配：缺口真值 (x, y, score)。返回自然像素。"""
    import numpy as np

    bgg = np.asarray(bg.convert("L"), dtype=np.float64)
    pzg = np.asarray(pz_c.convert("L"), dtype=np.float64)
    m = np.asarray(mask_img.convert("L"), dtype=np.float64) / 255.0
    H, W = bgg.shape
    ph, pw = pzg.shape
    best = (None, None, float("inf"))
    for y in range(0, H - ph + 1):
        for x in range(0, W - pw + 1):
            patch = bgg[y:y + ph, x:x + pw]
            diff = patch - pzg
            score = float((diff * diff * m).sum())
            if score < best[2]:
                best = (x, y, score)
    return best


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--endpoint", default="http://127.0.0.1:50272",
                    help="留存环境的 CDP 入口")
    ap.add_argument("--email", default="0_paw.stardom@icloud.com",
                    help="已烧掉的那个号（服务端没建成，可复用）")
    ap.add_argument("--password", default="Diag!x7kQ2pz")
    ap.add_argument("--out", default="data/captcha_diag")
    args = ap.parse_args()

    from app import secrets as appsecrets
    from app.net.twocaptcha_dragdrop import TwoCaptchaDragDrop
    from app.register import captcha_vision as cv
    from app.register.aliyun_slider_cdp import JS_PZ_LEFT, JS_SRCS, AliyunSliderCDP
    from app.register.cdp_page import new_page

    print(f"=== 连环境 {args.endpoint} ===")
    page = new_page(args.endpoint, SIGNUP_URL)
    page.goto(SIGNUP_URL, wait_ms=3000)

    # ---- 走到滑块（复用生产 step 方法：真事件链；复用死号邮箱不烧新资源）
    from app.register.qoder_signup_cdp import QoderSignupCDP

    signup = QoderSignupCDP(page, human=False)
    if not signup.step_identity(args.email, "David", "Jackson"):
        print("step1 没过（看日志），现场保留")
        return 2
    if not signup.step_password(args.password):
        print("step2 没过（看日志），现场保留")
        return 2

    solver = AliyunSliderCDP(page, paid_client=None, proxy=None)
    if not solver.expand():
        print("滑块面板没展开")
        return 2
    got = solver.grab_images()
    if got is None:
        print("取图失败")
        return 2
    bg, pz, data = got

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    ts = time.strftime("%H%M%S")
    bg.save(out / f"truth_{ts}_bg.png")
    pz.save(out / f"truth_{ts}_pz.png")
    print(f"图已存 {out}\\truth_{ts}_bg/pz.png")

    # ---- geometry（静止状态）
    bg_box = data.get("bgBox") or {}
    pz_box = data.get("pzBox") or {}
    track = page.eval(f"({_JS_RECT})('#aliyunCaptcha-sliding-body')")
    slider = page.eval(f"({_JS_RECT})('#aliyunCaptcha-sliding-slider')")
    left0 = float(page.eval(JS_PZ_LEFT) or 0)
    print("\n=== geometry（自然/显示）===")
    print(f"bg natural {bg.size[0]}x{bg.size[1]}  bgBox {bg_box}")
    print(f"pzBox {pz_box}  track {track}  slider {slider}")
    print(f"pz 初始 style.left = {left0}")

    # ---- ① 本地真值
    pz_c, mask_img, bbox = cv.crop_to_alpha(pz)
    gx, gy, gscore = match_gap(bg, pz_c, mask_img)
    pw, ph = pz_c.size
    print(f"\n=== ① 本地模板匹配（SSD={gscore:.0f}）===")
    print(f"缺口真值: 左沿 x={gx} 顶沿 y={gy}（自然像素, 拼图 {pw}x{ph}, "
          f"pz alpha bbox={bbox}）")

    # ---- ② 同一对图发 2Captcha
    key = str(appsecrets.get("captcha.twocaptcha_key", "") or "")
    paid = TwoCaptchaDragDrop(key, proxy="http://127.0.0.1:7897")
    print("\n=== ② 2Captcha（同一对图）===")
    sol = paid.solve(bg, pz_c)
    if sol.ok:
        print(f"2Captcha: x={sol.x:.1f} y={sol.y:.1f} cost=${sol.cost:.4f} "
              f"耗时={sol.elapsed:.1f}s")
        as_left = sol.x - gx
        as_center = sol.x - pw / 2 - gx
        print(f"  若 x=左沿 → 与真值差 {as_left:+.1f}px")
        print(f"  若 x=中心 → 与真值差 {as_center:+.1f}px")
        print(f"  y 与真值顶沿差 {sol.y - gy:+.1f}px；"
              f"与 pz alpha bbox_y 差 {sol.y - bbox[1]:+.1f}px")
    else:
        print("2Captcha 失败:", sol.error)

    # ---- ③ 受控拖拽实验（+100 → +200）
    print("\n=== ③ 受控拖拽（滑块走 100/200px，量 puzzle 实际位移）===")
    # 关键：先把图注回 <img>（heal_images），组件才会 arm、才可拖。
    # 上一轮就是少了这步，拖了 puzzle 纹丝不动。
    if not solver.heal_images(bg, pz):
        print("heal_images 后 8 秒仍未 arm（看 aliyun 日志），现场保留")
        return 2
    slider = page.eval(f"({_JS_RECT})('#aliyunCaptcha-sliding-slider')")
    sx, sy = slider["cx"], slider["cy"]
    page.mouse("mousePressed", sx, sy, buttons=1)
    time.sleep(0.3)

    def _probe(tag: str) -> dict:
        left = float(page.eval(JS_PZ_LEFT) or 0)
        box = (page.eval(JS_SRCS) or {}).get("pzBox") or {}
        print(f"  {tag}: style.left={left:.1f} pzBox.x={box.get('x')}")
        return {"left": left, "x": float(box.get("x") or 0)}

    p0 = _probe("按下不动")
    page.move(sx + 100, sy, buttons=1)
    time.sleep(0.5)
    p1 = _probe("走 +100px")
    page.move(sx + 200, sy, buttons=1)
    time.sleep(0.5)
    p2 = _probe("走 +200px")
    page.mouse("mouseReleased", sx + 200, sy, buttons=0)
    time.sleep(1.0)
    p3 = _probe("松手后")

    d_left_100 = p1["left"] - p0["left"]
    d_left_200 = p2["left"] - p0["left"]
    d_x_100 = p1["x"] - p0["x"]
    print(f"  puzzle(style.left) 位移: +100px → {d_left_100:.1f}  "
          f"+200px → {d_left_200:.1f}")
    print(f"  pzBox.x 位移: +100px → {d_x_100:.1f}")
    print(f"  实测 ratio（滑块px/拼图px）: 100/{d_left_100:.1f}="
          f"{100 / max(d_left_100, 0.1):.3f}   200/{d_left_200:.1f}="
          f"{200 / max(d_left_200, 0.1):.3f}   （代码当前用 1.21）")

    print("\n（现场保留：标签页与环境都不关）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
