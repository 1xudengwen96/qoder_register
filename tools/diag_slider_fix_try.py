"""端到端验证（第二版）：修正算法真实过一次滑块。

上一版失败原因：refresh 后组件处于失败锁定态（img/puzzle display:none、
slider pointer-events:none），少了生产 solve() 每轮开头的 expand()。

本版严格按生产顺序：expand → grab → heal → 2Captcha → 修正换算 → 松弛拖拽
→ check()。拖拽前显式检查 slider pointerEvents，未 armed 先诊断不烧打码费。
结束不关标签页（保持现场）。
"""
from __future__ import annotations

import json
import math
import sys
import time
import urllib.request

from app import secrets as appsecrets
from app.net.twocaptcha_dragdrop import TwoCaptchaDragDrop
from app.register import captcha_vision as cv
from app.register.aliyun_slider_cdp import JS_PZ_LEFT, JS_SOLVED, AliyunSliderCDP
from app.register.cdp_page import CDPPage

endpoint = "http://127.0.0.1:50272"
if len(sys.argv) > 1:
    endpoint = sys.argv[1]

with urllib.request.urlopen(endpoint + "/json", timeout=10) as r:
    targets = json.loads(r.read().decode("utf-8", "replace"))
pages = [t for t in targets if t.get("type") == "page"
         and "qoder.com" in (t.get("url") or "")]
if not pages:
    print("没有 qoder 标签页")
    raise SystemExit(2)

page = CDPPage(endpoint, ws_url=pages[-1]["webSocketDebuggerUrl"], timeout=20)
key = str(appsecrets.get("captcha.twocaptcha_key", "") or "")
paid = TwoCaptchaDragDrop(key, proxy="http://127.0.0.1:7897")
solver = AliyunSliderCDP(page, paid_client=paid, proxy=None)


def arm_state() -> dict:
    return page.eval("""(() => {
      const s = document.querySelector('#aliyunCaptcha-sliding-slider');
      const i = document.querySelector('#aliyunCaptcha-img');
      const p = document.querySelector('#aliyunCaptcha-puzzle');
      return {cls: s ? String(s.className) : 'no-slider',
              pe: s ? getComputedStyle(s).pointerEvents : null,
              imgDisp: i ? getComputedStyle(i).display : null,
              pzDisp: p ? getComputedStyle(p).display : null,
              pzLeft: p ? p.style.left : null}; })()""")


print("=== ① expand（生产每轮开头都做）===")
if not solver.expand():
    print("expand 失败")
    raise SystemExit(2)
print(f"arm_state: {arm_state()}")

print("=== ② grab + heal ===")
got = solver.grab_images()
if got is None:
    print("取图失败")
    raise SystemExit(2)
bg, pz, data = got
bg_box = data.get("bgBox") or {}
track = page.eval("""(() => {
  const e = document.querySelector('#aliyunCaptcha-sliding-body');
  const r = e.getBoundingClientRect();
  return {x: r.x, w: r.width}; })()""")
slider = page.eval("""(() => {
  const e = document.querySelector('#aliyunCaptcha-sliding-slider');
  const r = e.getBoundingClientRect();
  return {cx: r.x + r.width / 2, cy: r.y + r.height / 2}; })()""")
print(f"bg {bg.size[0]}x{bg.size[1]}  bgBox {bg_box}  track w={track['w']}")
solver.heal_images(bg, pz)
time.sleep(1.0)
st = arm_state()
print(f"arm_state after heal: {st}")
if st.get("pe") != "all":
    print("slider 仍不可拖（pe != all），现场保留，不再烧打码费")
    raise SystemExit(2)

print("=== ③ 2Captcha 识别 + 修正换算 ===")
pz_c, _mask, bbox = cv.crop_to_alpha(pz)
pw, ph = pz_c.size
scale = float(bg_box.get("w") or bg.size[0]) / bg.size[0]
sol = paid.solve(bg, pz_c)
if not sol.ok:
    print("2Captcha 失败:", sol.error)
    raise SystemExit(2)
x_left = (sol.x - pw / 2) * scale          # 修正 1：中心 → 左沿
limit = max(0.0, float(bg_box.get("w")) - pw * scale)
x_left = min(max(0.0, x_left), limit)
print(f"2Captcha x={sol.x:.1f} y={sol.y:.1f}（y-bbox_y={sol.y - bbox[1]:+.1f}"
      f" 应≈半高 {ph / 2:.0f}）")
print(f"pw={pw} scale={scale:.4f} → 目标左沿 {x_left:.1f}（上限 {limit:.1f}）")

print("=== ④ 松弛拖拽（曲线逆变换初值 + 低增益闭环）===")
sx, sy = slider["cx"], slider["cy"]
track_x, track_w = track["x"], track["w"]
max_travel = track_w - 40

a, b = 0.077, 0.00355                      # 同几何实测的加速曲线
t0 = (-a + math.sqrt(a * a + 4 * b * x_left)) / (2 * b) if x_left > 0 else 0
t0 = max(0.0, min(t0, max_travel))
print(f"g 逆变换初值 t0={t0:.1f}（max_travel={max_travel:.0f}）")

page.move(sx, sy)
time.sleep(0.15)
page.mouse("mousePressed", sx, sy, buttons=1)
time.sleep(0.3)

cur_x = sx + t0 * 0.7
page.move(cur_x, sy, buttons=1)
time.sleep(0.5)

for i in range(40):
    left = float(page.eval(JS_PZ_LEFT) or 0)
    if left <= 0.5:
        print(f"  iter{i}: 反馈不可用 left={left}")
        break
    remaining = x_left - left
    if abs(remaining) <= 1.5:
        print(f"  iter{i}: 收敛 left={left:.2f}（误差 {remaining:+.2f}）")
        break
    dx = max(-12.0, min(12.0, remaining * 0.6))
    cur_x = max(track_x, min(track_x + track_w, cur_x + dx))
    page.move(cur_x, sy, buttons=1)
    time.sleep(0.05)
else:
    left = float(page.eval(JS_PZ_LEFT) or 0)
    print(f"  40 轮后 left={left:.2f} 目标 {x_left:.1f}")

final = float(page.eval(JS_PZ_LEFT) or 0)
print(f"最终 left={final:.2f} 目标={x_left:.1f} 误差={final - x_left:+.2f}")

time.sleep(0.5)
page.mouse("mouseReleased", cur_x, sy, buttons=0)
print("\n=== ⑤ check() ===")
time.sleep(4.0)
st = page.eval(JS_SOLVED) or {"ok": False, "why": "no-state"}
print(f"check: {st}")
print("\n（现场保留：标签页与环境都不关）")
