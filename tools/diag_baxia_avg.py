"""验证实验：Baxia 题干噪点条的多帧平均（现场 acct19 的 punish iframe）。

假设：题干 canvas（"airplane" 之类的描述文字）被逐帧随机噪点覆盖，
单帧截图（screenshot_instructions 的现状）读不出来，打码员只能瞎猜
（本轮 2Captcha 答了 0、1，正确应 2、4）。
做法：连拍 N 帧 iframe 顶部截图 → Python 侧逐像素平均 → 存单帧 vs 平均图对比。
只读实验，不点任何格子、不关现场。
"""
from __future__ import annotations

import base64
import json
import time
import urllib.request

import numpy as np
from PIL import Image

from app.register.cdp_page import CDPPage
from app.register.baxia_cdp import JS_IFRAME_RECT

ENDPOINT = "http://127.0.0.1:55012"

with urllib.request.urlopen(ENDPOINT + "/json", timeout=10) as r:
    targets = json.loads(r.read().decode("utf-8", "replace"))
pages = [t for t in targets if t.get("type") == "page"
         and "qoder.com" in (t.get("url") or "")]
p = CDPPage(ENDPOINT, ws_url=pages[-1]["webSocketDebuggerUrl"], timeout=20)

rect = p.eval(JS_IFRAME_RECT) or {}
print("iframe rect:", rect)
if rect.get("error"):
    print("没有 punish iframe")
    raise SystemExit(2)

N = 10
frames = []
for i in range(N):
    res = p.cdp.call("Page.captureScreenshot", {
        "format": "png",
        "clip": {"x": rect["x"], "y": max(0.0, rect["y"]),
                 "width": rect["w"], "height": 130, "scale": 2},
    })
    frames.append(Image.open(__import__("io").BytesIO(
        base64.b64decode(res["data"]))))
    time.sleep(0.12)

# 平均
arr = np.stack([np.asarray(f.convert("RGB"), dtype=np.float64)
                for f in frames])
avg = Image.fromarray(arr.mean(axis=0).round().astype("uint8"))

frames[0].save("data/captcha_diag/baxia_single.png")
avg.save("data/captcha_diag/baxia_avg10.png")
# 3 倍放大平均图，肉眼验证
avg.resize((avg.width * 2, avg.height * 2)).save(
    "data/captcha_diag/baxia_avg10_2x.png")
print(f"已存 {N} 帧平均 → data/captcha_diag/baxia_avg10.png（单帧对照 "
      f"baxia_single.png）")
print("（现场保留：不点格子、不关标签页）")
