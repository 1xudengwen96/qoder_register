"""分辨实验：非线性是「真实位移映射」还是「平滑动画滞后」？

A. 瞬间跳变 +100 后按住不动，0.2/0.6/1.2/2.5s 各读一次 left：
   - 若 left 持续增长 → 时间平滑（滞后），生产连续拖拽的响应更接近线性
   - 若 left 原地不动 → 空间映射（真曲线），任何匀速比都错
B. 慢速连续拖拽到 +100（50 小步 × 20ms），读 left，对照跳变结果。
结束不关标签页（保持现场）。
"""
from __future__ import annotations

import json
import sys
import time
import urllib.request

from app.register.aliyun_slider_cdp import JS_PZ_LEFT
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

page = CDPPage(endpoint, ws_url=pages[-1]["webSocketDebuggerUrl"], timeout=15)
slider = page.eval("""(() => {
  const e = document.querySelector('#aliyunCaptcha-sliding-slider');
  const r = e.getBoundingClientRect();
  return {cx: r.x + r.width / 2, cy: r.y + r.height / 2}; })()""")
sx, sy = slider["cx"], slider["cy"]


def left() -> float:
    return float(page.eval(JS_PZ_LEFT) or 0)


print("=== A. 瞬间跳变 +100px 后按住不动 ===")
page.mouse("mousePressed", sx, sy, buttons=1)
time.sleep(0.3)
print(f"  按下: left={left():.2f}")
page.move(sx + 100, sy, buttons=1)
for i, w in enumerate((0.2, 0.5, 1.0, 1.5, 2.5)):
    time.sleep(w)
    print(f"  跳变后第 {i + 1} 次读（再等 {w}s）: left={left():.2f}")
page.mouse("mouseReleased", sx + 100, sy, buttons=0)
time.sleep(2.0)
print(f"  松手后: left={left():.2f}  (回弹)")

print("\n=== B. 慢速连续拖 50 步到 +100px ===")
page.mouse("mousePressed", sx, sy, buttons=1)
time.sleep(0.3)
for i in range(1, 51):
    page.move(sx + i * 2, sy, buttons=1)
    time.sleep(0.02)
time.sleep(0.5)
print(f"  到 +100 后 0.5s: left={left():.2f}")
time.sleep(2.0)
print(f"  再等 2s: left={left():.2f}")
page.mouse("mouseReleased", sx + 100, sy, buttons=0)
time.sleep(1.5)
print(f"  松手后: left={left():.2f}")
print("\n（现场保留）")
