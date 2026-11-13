"""阿里云 FeiLin 滑块 —— 纯 CDP 版（不依赖 Playwright）。

逻辑与 ``app/register/aliyun_slider.py`` 等价，只是把 ``page.evaluate``
换成 ``CDPPage.eval``，把 Playwright 的鼠标换成 ``Input.dispatchMouseEvent``。
识别仍用 ``captcha_vision.find_gap``（numpy 版），可选 2Captcha 付费兜底。

实测要点（沿用旧版结论，全部仍然成立）
------------------------------------
1. 取图必须解码 ``img.src``，不能截图（截图会把透明区合成掉）
2. 拼图块必须裁到 alpha bbox（52×200 画布 81% 是透明）
3. refresh 换题后 src 会变成 ``*.aliyuncs.com`` 远程 URL
4. 拖动必须实时读 ``#aliyunCaptcha-puzzle`` 的 ``style.left`` 做残差修正
5. 轨道宽度与图像可移动距离之间有比例，不能直接按像素搬
"""

from __future__ import annotations

import random
import time
from dataclasses import dataclass, field

from PIL import Image

from app.register import captcha_vision

SEL = {
    "bg": "#aliyunCaptcha-img",
    "puzzle": "#aliyunCaptcha-puzzle",
    "track": "#aliyunCaptcha-sliding-body",
    "slider": "#aliyunCaptcha-sliding-slider",
    "window": "#aliyunCaptcha-window-float",
    "collapse": "#aliyunCaptcha-captcha-left",
    "captcha_btn": "#captcha-element",
    "refresh": "#aliyunCaptcha-btn-refresh",
}

JS_STATE = """(() => {
  const win = document.querySelector('#aliyunCaptcha-window-float');
  const track = document.querySelector('#aliyunCaptcha-sliding-body');
  const slider = document.querySelector('#aliyunCaptcha-sliding-slider');
  const img = document.querySelector('#aliyunCaptcha-img');
  const winShown = !!(win && win.classList.contains('window-show') &&
                      win.offsetParent !== null);
  return {
    expanded: winShown && !!(track && track.getBoundingClientRect().width > 0),
    ready: winShown && !!(img && img.complete && img.naturalWidth > 0),
    winShown,
  };
})()"""

JS_SRCS = """(() => {
  const p = document.querySelector('#aliyunCaptcha-puzzle');
  const i = document.querySelector('#aliyunCaptcha-img');
  const b = (e) => {
    if (!e) return null;
    const r = e.getBoundingClientRect();
    return {x: r.x, y: r.y, w: r.width, h: r.height};
  };
  return {
    pzSrc: p ? (p.src || '') : '',
    bgSrc: i ? (i.src || '') : '',
    bgNatural: i ? [i.naturalWidth, i.naturalHeight] : null,
    bgBox: b(i),
    pzBox: b(p),
  };
})()"""

JS_PZ_LEFT = """(() => {
  const p = document.querySelector('#aliyunCaptcha-puzzle');
  return p ? parseFloat(p.style.left || '0') : 0;
})()"""

JS_SOLVED = """(() => {
  const codeInput = document.querySelector(
    'input#basic_code, input[autocomplete=one-time-code]');
  if (codeInput && codeInput.getBoundingClientRect().width > 0) {
    return {ok: true, why: 'code-input-appeared'};
  }
  const txt = (document.body ? document.body.innerText : '').toLowerCase();
  if (txt.includes('verified') && !txt.includes('unable to verify')) {
    return {ok: true, why: 'text-verified'};
  }
  const err = document.querySelector('[class*=error],[class*=tips-error]');
  if (err && err.innerText) {
    return {ok: false, why: 'error:' + err.innerText.slice(0, 60)};
  }
  if (txt.includes('please complete the captcha') ||
      txt.includes('unable to verify')) {
    return {ok: false, why: 'still-need-captcha'};
  }
  const win = document.querySelector('#aliyunCaptcha-window-float');
  const shown = !!(win && win.classList.contains('window-show') &&
                   win.offsetParent !== null);
  const sd = document.querySelector('#aliyunCaptcha-sliding-slider');
  const cls = (sd && sd.className) || '';
  if (/success|pass/i.test(cls)) return {ok: true, why: 'class-success'};
  if (shown) return {ok: false, why: 'window-still-open'};
  return {ok: true, why: 'window-gone-weak'};
})()"""

JS_IMAGES_READY = """(() => {
  const i = document.querySelector('#aliyunCaptcha-img');
  const p = document.querySelector('#aliyunCaptcha-puzzle');
  const s = document.querySelector('#aliyunCaptcha-sliding-slider');
  if (!i || !p || !s) return false;
  return i.complete && i.naturalWidth > 0 &&
         p.complete && p.naturalWidth > 0 &&
         i.getBoundingClientRect().width > 100 &&
         s.getBoundingClientRect().width > 0;
})()"""


@dataclass(slots=True)
class SolveResult:
    ok: bool
    attempts: int = 0
    target_x: float = 0.0
    final_x: float = 0.0
    cost: float = 0.0
    method: str = ""
    detail: str = ""
    trail: list = field(default_factory=list)


class AliyunSliderCDP:
    def __init__(self, page, *, human: bool = True, logger=None,
                 paid_client=None, prefer_paid: bool = False,
                 proxy: str | None = None,
                 local_fallback: bool = True) -> None:
        self.page = page
        self.human = human
        self.log = logger
        self.paid = paid_client
        self.prefer_paid = prefer_paid and paid_client is not None
        # 付费可用时默认不回落本地识别（用户要求只走 2Captcha）
        self.local_fallback = local_fallback and not self.prefer_paid
        self.proxy = proxy
        self.total_cost = 0.0

    def _d(self, msg: str) -> None:
        if self.log:
            self.log.info("[slider-cdp] %s", msg)

    # ---------------------------------------------------------- 展开
    def expand(self, max_try: int = 3) -> bool:
        """确保滑块面板打开且图片就绪。

        踩坑：``#captcha-element`` 是**开关**，连着点会把面板点开又点关，
        最后一直卡在"窗口没起来"。所以先判就绪，没就绪才点，且每次点完
        老老实实等图片加载。
        """
        if self.page.wait_js(JS_IMAGES_READY, timeout=3):
            return True

        for attempt in range(1, max_try + 1):
            if self.page.click(SEL["captcha_btn"], human=self.human, timeout=3):
                self._d(f"第 {attempt} 次点展开键")
            else:
                self.page.click(SEL["collapse"], human=self.human, timeout=2)
            if self.page.wait_js(JS_IMAGES_READY, timeout=8):
                return True
            # 面板可能是被上一次点击关上的，再补一次
            self.page.click(SEL["collapse"], human=self.human, timeout=2)
            if self.page.wait_js(JS_IMAGES_READY, timeout=6):
                return True

        st = self.page.eval(JS_STATE) or {}
        img_ok = self.page.eval(
            "(() => { const i=document.querySelector('#aliyunCaptcha-img');"
            " return i ? [i.complete, i.naturalWidth, "
            "i.getBoundingClientRect().width] : null; })()")
        self._d(f"滑块窗口没起来 state={st} img={img_ok}")
        return False

    # ---------------------------------------------------------- 取图
    def grab_images(self, wait_src: float = 15.0):
        deadline = time.time() + wait_src
        last = None
        while time.time() < deadline:
            data = self.page.eval(JS_SRCS) or {}
            last = data
            bg_src = (data.get("bgSrc") or "").strip()
            pz_src = (data.get("pzSrc") or "").strip()
            if ((bg_src.startswith("data:") or bg_src.startswith("http")) and
                    (pz_src.startswith("data:") or pz_src.startswith("http"))):
                bg = captcha_vision.load_image_src(bg_src, proxy=self.proxy)
                pz = captcha_vision.load_image_src(pz_src, proxy=self.proxy)
                if bg is not None and pz is not None:
                    return bg, pz, data
                self._d(f"图片解码失败 bg={bg_src[:50]} pz={pz_src[:50]}")
            time.sleep(0.6)
        if last:
            self._d(f"等 src 超时 bg={(last.get('bgSrc') or '')[:50]}")
        return None

    # ---------------------------------------------------------- 识别
    def identify(self):
        got = self.grab_images()
        if got is None:
            return None
        bg, pz, _data = got
        bg_w = bg.size[0]
        pz_c, _mask, bbox = captcha_vision.crop_to_alpha(pz)
        pw = pz_c.size[0]
        bbox_y = bbox[1]

        if self.prefer_paid:
            try:
                sol = self.paid.solve(bg, pz_c)
                if sol.ok:
                    self.total_cost += sol.cost
                    # 2Captcha 给的是拼图块目标中心 x，减半宽得左沿
                    x_left = sol.x - pw / 2.0
                    x_left = min(max(0.0, x_left), float(bg_w - pw))
                    dy = abs(sol.y - bbox_y)
                    self._d(f"2Captcha 中心 x={sol.x:.0f} y={sol.y:.0f} "
                            f"(bbox_y={bbox_y} dy={dy:.0f}) → 左沿 {x_left:.1f} "
                            f"耗时={sol.elapsed:.1f}s cost=${sol.cost:.4f}")
                    return x_left, f"2captcha(c={sol.x:.0f})", sol.cost
                self._d(f"2Captcha 失败: {sol.error}")
            except Exception as exc:
                self._d(f"2Captcha 异常: {type(exc).__name__}: {exc}")
            if not self.local_fallback:
                self._d("付费识别未拿到坐标，按配置不回落本地")
                return None

        gap = captcha_vision.find_gap(bg, pz, display_w=None)
        if gap.ok:
            return gap.x_display, f"local-{gap.method}", 0.0
        self._d(f"本地识别失败: {gap.detail}")
        return None

    # ---------------------------------------------------------- 拖拽
    def drag_to(self, target_left: float, data: dict) -> tuple[bool, float]:
        track = self.page.eval(
            "(() => { const e = document.querySelector('#aliyunCaptcha-sliding-body');"
            " if (!e) return null; const r = e.getBoundingClientRect();"
            " return {x: r.x, y: r.y, w: r.width, h: r.height}; })()")
        slider = self.page.eval(
            "(() => { const e = document.querySelector('#aliyunCaptcha-sliding-slider');"
            " if (!e) return null; const r = e.getBoundingClientRect();"
            " return {x: r.x, y: r.y, w: r.width, h: r.height}; })()")
        if not track or not slider:
            return False, 0.0

        bg_box = data.get("bgBox") or {"w": 300}
        pz_box = data.get("pzBox") or {"w": 52}
        sx = slider["x"] + slider["w"] / 2
        sy = slider["y"] + slider["h"] / 2
        track_w = max(track["w"], 1)
        max_travel = max(bg_box["w"] - pz_box.get("w", 52), 1)
        ratio = track_w / max_travel

        if self.human:
            self.page.move(sx - random.uniform(20, 45), sy + random.uniform(12, 28))
            time.sleep(random.uniform(0.18, 0.32))
            for i in range(1, 5):
                self.page.move(sx + (i / 5) * random.uniform(-4, 2),
                               sy + random.uniform(-3, 3))
                time.sleep(random.uniform(0.02, 0.05))

        self.page.move(sx, sy)
        time.sleep(random.uniform(0.08, 0.18))
        self.page.mouse("mousePressed", sx, sy, buttons=1)
        time.sleep(random.uniform(0.06, 0.14))

        if self.human:
            for _ in range(random.randint(1, 3)):
                self.page.move(sx + random.uniform(-1.5, 1.5),
                               sy + random.uniform(-1.5, 1.5), buttons=1)
                time.sleep(random.uniform(0.02, 0.05))

        cur_x = sx
        for _ in range(140):
            cur_left = float(self.page.eval(JS_PZ_LEFT) or 0.0)
            remaining = target_left - cur_left
            if abs(remaining) <= 1.0:
                break
            dist = abs(remaining)
            cap = 22 if dist > 60 else 12 if dist > 20 else 5 if dist > 6 else 2
            dx = max(-cap, min(cap, remaining * ratio))
            if self.human:
                dx += random.uniform(-0.4, 0.4)
            cur_x = max(track["x"], min(track["x"] + track_w, cur_x + dx))
            self.page.move(cur_x, sy + random.uniform(-0.6, 0.6), buttons=1)
            base = 0.010 if dist > 40 else (0.018 if dist > 12 else 0.030)
            time.sleep(base * random.uniform(0.8, 1.4))

        final = float(self.page.eval(JS_PZ_LEFT) or 0.0)
        self._d(f"拖动完成 left={final:.1f} target={target_left:.1f}")
        if self.human:
            time.sleep(random.uniform(0.12, 0.28))
        self.page.mouse("mouseReleased", cur_x, sy, buttons=0)
        time.sleep(random.uniform(3.0, 4.5))
        return abs(final - target_left) <= 3.0, final

    # ---------------------------------------------------------- 检查/换题
    def check(self) -> dict:
        return self.page.eval(JS_SOLVED) or {"ok": False, "why": "no-state"}

    def refresh(self) -> bool:
        if not self.page.click(SEL["refresh"], human=self.human, timeout=3):
            self.expand()
        else:
            self._d("已点 refresh 换题")
        return self.grab_images(wait_src=15.0) is not None

    # ---------------------------------------------------------- 主流程
    def solve(self, max_attempts: int = 6) -> SolveResult:
        res = SolveResult(ok=False)
        for attempt in range(1, max_attempts + 1):
            res.attempts = attempt
            if not self.expand():
                time.sleep(2)
                continue

            ident = self.identify()
            if ident is None:
                res.detail = "识别失败"
                self.refresh()
                continue

            target, method, cost = ident
            res.target_x, res.method = target, method
            res.cost += cost

            got = self.grab_images()
            if got is None:
                res.detail = "取图失败"
                self.refresh()
                continue
            data = got[2]

            aligned, final_left = self.drag_to(target, data)
            res.final_x = final_left
            res.trail.append({"attempt": attempt, "target": round(target, 1),
                              "final": round(final_left, 1),
                              "aligned": aligned, "method": method})

            st = self.check()
            if st.get("ok") and "weak" not in str(st.get("why")):
                res.ok = True
                res.detail = str(st.get("why"))
                self._d(f"滑块通过（第 {attempt} 次 target={target:.1f} "
                        f"method={method}）")
                return res

            res.detail = str(st.get("why"))
            self._d(f"第 {attempt} 次未通过: {res.detail} "
                    f"(target={target:.1f} final={final_left:.1f})")
            self.refresh()
        return res
