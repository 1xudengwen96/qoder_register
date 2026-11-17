"""在留存标签页上做受控拖拽：量「滑块位移 vs puzzle 位移」的真实 ratio。

前提（diag_slider_truth.py 第二轮跑后的现场）：图已注入 dataURL
（img.naturalWidth=296、pz=52、pointerEvents=all），slider class 仍是
'slider-move initial'（JS_ARMED 判 not-armed 只是启发式，不影响拖）。

做法：压住滑块 → +100px → +200px → 松手，每步读 puzzle style.left
与 pzBox.x，算真实 ratio（对照生产代码的 1.21）。
结束不关标签页、不关环境（保持现场）。
"""
from __future__ import annotations

import json
import sys
import time
import urllib.request

from app.register.aliyun_slider_cdp import JS_PZ_LEFT, JS_SRCS
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


def probe(tag: str) -> dict:
    left = float(page.eval(JS_PZ_LEFT) or 0)
    box = (page.eval(JS_SRCS) or {}).get("pzBox") or {}
    cls = page.eval("""(() => {
      const s = document.querySelector('#aliyunCaptcha-sliding-slider');
      return s ? String(s.className || '') : 'no-slider'; })()""")
    print(f"  {tag}: style.left={left:.2f}  pzBox.x={box.get('x')}  cls={cls!r}")
    return {"left": left, "x": float(box.get("x") or 0)}


slider = page.eval("""(() => {
  const e = document.querySelector('#aliyunCaptcha-sliding-slider');
  if (!e) return null;
  const r = e.getBoundingClientRect();
  return {x: r.x, y: r.y, w: r.width, h: r.height,
          cx: r.x + r.width / 2, cy: r.y + r.height / 2}; })()""")
track = page.eval("""(() => {
  const e = document.querySelector('#aliyunCaptcha-sliding-body');
  if (!e) return null;
  const r = e.getBoundingClientRect();
  return {x: r.x, y: r.y, w: r.width}; })()""")
print(f"slider={slider}  track={track}")

sx, sy = slider["cx"], slider["cy"]
print("\n=== 受控拖拽（+100 → +200 → 回 0）===")
p0 = probe("按下前")
page.mouse("mousePressed", sx, sy, buttons=1)
time.sleep(0.4)
p1 = probe("按下不动")
page.move(sx + 100, sy, buttons=1)
time.sleep(0.6)
p2 = probe("走 +100px")
page.move(sx + 200, sy, buttons=1)
time.sleep(0.6)
p3 = probe("走 +200px")
page.move(sx + 150, sy, buttons=1)
time.sleep(0.6)
p4 = probe("回走到 +150px")
page.move(sx, sy, buttons=1)
time.sleep(0.6)
p5 = probe("拖回 0")
page.mouse("mouseReleased", sx, sy, buttons=0)
time.sleep(2.0)
p6 = probe("松手 2 秒后")

d = [p["left"] - p1["left"] for p in (p2, p3, p4, p5)]
s = (100, 200, 150, 0)
print("\n=== ratio（滑块位移 / puzzle style.left 位移）===")
for travel, dl in zip(s, d):
    print(f"  滑块 {travel:+4d}px → puzzle left {dl:+8.2f}px"
          f"  ratio={travel / dl if abs(dl) > 0.5 else float('nan'):.4f}"
          if abs(dl) > 0.5 else
          f"  滑块 {travel:+4d}px → puzzle left {dl:+8.2f}px  （无反馈）")
print(f"\n生产代码当前用 ratio = track_w/(bgBox.w-pzBox.w) = "
      f"{track['w']}/{300 - 52} = {track['w'] / (300 - 52):.4f}")
print("（现场保留：标签页与环境都不关）")
