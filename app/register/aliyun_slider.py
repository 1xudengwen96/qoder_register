"""阿里云滑块求解器（付费优先版）。

策略
----
**主用 2Captcha DragAndDropTask**（$0.0012/次，15~20 秒）
    - 只提交背景图 + 拼图块，返回缺口坐标
    - 与拖拽逻辑完全解耦，替换成本极低
    - 用户明确要求「不自研」，这是主路径

**本地 OpenCV 作为可选降级**（免费但稳定性不足）
    - 实测会退化（多峰等高）、且换题后 src 变化导致失败
    - 仅在 ``prefer_paid=False`` 时使用

踩坑记录
--------
1. **必须解码 img.src，不能 screenshot**
   Playwright 截图会把透明处与背景合成，alpha 丢失。

2. **拼图块必须裁到 alpha bbox**
   52×200 画布有 81% 透明，真实内容约 52×54。

3. **refresh 后 src 可能不是 data URL**
   点 #aliyunCaptcha-btn-refresh 后，src 要等一会儿才变成新的 data URL。
   直接读会拿到空串或旧的，必须轮询等待。

4. **2Captcha 返回的 x 语义**
   实测 x=260/263（图宽 296，拼图宽 52，理论上限 244）。
   说明返回的是**拼图块目标位置的左沿**在更大坐标系里，
   或按整图宽计算。使用时要 clamp 到 [0, bg_w - piece_w]。
"""

from __future__ import annotations

import asyncio
import random
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

JS_STATE = """() => {
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
}"""

JS_SRCS = """() => {
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
}"""

JS_PZ_LEFT = """() => {
  const p = document.querySelector('#aliyunCaptcha-puzzle');
  return p ? parseFloat(p.style.left || '0') : 0;
}"""

JS_SOLVED = """() => {
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
}"""


@dataclass(slots=True)
class SolveResult:
    ok: bool
    attempts: int = 0
    target_x: float = 0.0
    final_x: float = 0.0
    score: float = 0.0
    method: str = ""
    detail: str = ""
    trail: list = field(default_factory=list)
    cost: float = 0.0


class AliyunSliderSolver:
    """求解 FeiLin 拼图滑块。付费优先，本地兜底。"""

    def __init__(self, page, *, human: bool = True, logger=None,
                 paid_client=None, prefer_paid: bool = True,
                 proxy: str | None = "http://127.0.0.1:7897") -> None:
        self.page = page
        self.human = human
        self.log = logger
        self.paid = paid_client          # TwoCaptchaDragDrop 实例
        self.prefer_paid = prefer_paid and paid_client is not None
        self.proxy = proxy               # 下载远程图片用
        self._cdp_session = None
        self.total_cost = 0.0

    def _d(self, msg: str) -> None:
        if self.log:
            self.log.info("[slider] %s", msg)

    async def _cdp(self):
        if self._cdp_session is None:
            self._cdp_session = await self.page.context.new_cdp_session(self.page)
        return self._cdp_session

    async def _mouse(self, kind: str, x: float, y: float, buttons: int = 0) -> None:
        cdp = await self._cdp()
        params = {
            "type": kind, "x": float(x), "y": float(y),
            "modifiers": 0, "pointerType": "mouse",
        }
        if kind in ("mousePressed", "mouseReleased", "mouseMoved"):
            params["button"] = "left"
            params["buttons"] = buttons
        if kind == "mousePressed":
            params["clickCount"] = 1
        await cdp.send("Input.dispatchMouseEvent", params)

    # ---------------------------------------------------------- 展开
    async def expand(self, max_try: int = 3) -> bool:
        for _ in range(max_try):
            st = await self.page.evaluate(JS_STATE)
            if st.get("expanded"):
                break
            try:
                btn = self.page.locator(SEL["captcha_btn"])
                if await btn.count() > 0 and await btn.is_visible():
                    await btn.click(timeout=5000)
                else:
                    left = self.page.locator(SEL["collapse"])
                    if await left.count() > 0 and await left.is_visible():
                        await left.click(timeout=5000, force=True)
            except Exception:
                pass
            await asyncio.sleep(1.5)

        try:
            await self.page.wait_for_function(
                """() => {
                  const i = document.querySelector('#aliyunCaptcha-img');
                  const p = document.querySelector('#aliyunCaptcha-puzzle');
                  const s = document.querySelector('#aliyunCaptcha-sliding-slider');
                  if (!i || !p || !s) return false;
                  return i.complete && i.naturalWidth > 0 &&
                         p.complete && p.naturalWidth > 0 &&
                         i.getBoundingClientRect().width > 100 &&
                         s.getBoundingClientRect().width > 0;
                }""", timeout=15000)
            return True
        except Exception:
            return False

    # ---------------------------------------------------------- 取图
    async def grab_images(self, wait_src: float = 15.0
                          ) -> tuple[Image.Image, Image.Image, dict] | None:
        """取背景图与拼图块。

        src 有两种形态，都要支持：
          1. ``data:image/png;base64,...``         初次加载
          2. ``https://static-captcha-*.aliyuncs.com/qst/PUZZLE/...``
             **点 refresh 换题后**会变成远程 URL

        踩坑：换题后如果只认 data URL，会一直「解码失败」。
        """
        import time
        deadline = time.time() + wait_src
        last = None
        while time.time() < deadline:
            data = await self.page.evaluate(JS_SRCS)
            last = data
            bg_src = (data.get("bgSrc") or "").strip()
            pz_src = (data.get("pzSrc") or "").strip()
            ok_bg = bg_src.startswith("data:") or bg_src.startswith("http")
            ok_pz = pz_src.startswith("data:") or pz_src.startswith("http")
            if ok_bg and ok_pz:
                bg = captcha_vision.load_image_src(bg_src, proxy=self.proxy)
                pz = captcha_vision.load_image_src(pz_src, proxy=self.proxy)
                if bg is not None and pz is not None:
                    return bg, pz, data
                self._d(f"加载图片失败 bg={bg_src[:60]} pz={pz_src[:60]}")
            await asyncio.sleep(0.6)

        if last:
            self._d(f"等待 src 超时: bg={(last.get('bgSrc') or '')[:60]} "
                    f"pz={(last.get('pzSrc') or '')[:60]}")
        return None

    # ---------------------------------------------------------- 识别
    async def identify(self) -> tuple[float, str, float] | None:
        """返回 (target_left_css_px, method, cost)。失败返回 None。

        **y 交叉校验**（提高付费识别准确率的关键）
        --------------------------------------------------
        拼图块是从原图缺口处裁出来的，所以拼图块 bbox 的 y **就是**缺口的 y。
        2Captcha 返回的 y 应该与之接近。实测误差大时（>35px），
        它给的位置基本都是错的 —— 用这个做过滤，能把误判挡掉。
        """
        got = await self.grab_images()
        if got is None:
            return None
        bg, pz, data = got
        bg_w = bg.size[0]
        pz_c, _, bbox = captcha_vision.crop_to_alpha(pz)
        pw = pz_c.size[0]
        bbox_y = bbox[1]          # 缺口在该画布里的 y

        # ---- 付费优先 ----
        if self.prefer_paid:
            try:
                sol = self.paid.solve(bg, pz_c)
                if sol.ok:
                    self.total_cost += sol.cost
                    # 2Captcha 返回的是**拼图块中心**的目标 x，减半宽得左沿。
                    # 实测对照（拼图宽 52）：
                    #   271 → 245 拖到 248 通过；274 → 248 拖到 247 通过
                    #   196 → 170.5 通过；181 → 155.5 通过
                    #   179 → 153 失败；242 → 216.5 失败
                    half = pw / 2.0
                    x_left = sol.x - half
                    x_left = min(max(0.0, x_left), float(bg_w - pw))

                    dy = abs(sol.y - bbox_y)
                    if dy > 35:
                        # y 偏差过大 → 判定识别不可信，换题重试
                        self._d(f"2Captcha y={sol.y} 与 bbox_y={bbox_y} "
                                f"偏差 {dy:.0f}px 过大，判为不可信")
                        return None

                    self._d(f"2Captcha 中心 x={sol.x} y={sol.y}(bbox_y={bbox_y}) "
                            f"→ 左沿 {x_left} 耗时={sol.elapsed:.1f}s "
                            f"cost=${sol.cost:.4f}")
                    return x_left, f"2captcha(c={sol.x:.0f})", sol.cost
                self._d(f"2Captcha 失败: {sol.error}")
            except Exception as exc:
                self._d(f"2Captcha 异常: {type(exc).__name__}: {exc}")

        # ---- 本地兜底 ----
        gap = captcha_vision.find_gap(bg, pz, display_w=None)
        if gap.ok:
            return gap.x_display, f"local-{gap.method}", 0.0
        self._d(f"本地识别失败: {gap.detail}")
        return None

    # ---------------------------------------------------------- 拖拽
    async def drag_to(self, target_left: float, data: dict) -> tuple[bool, float]:
        track = await self.page.evaluate(
            "() => { const e = document.querySelector('#aliyunCaptcha-sliding-body');"
            " if (!e) return null; const r = e.getBoundingClientRect();"
            " return {x: r.x, y: r.y, w: r.width, h: r.height}; }")
        slider = await self.page.evaluate(
            "() => { const e = document.querySelector('#aliyunCaptcha-sliding-slider');"
            " if (!e) return null; const r = e.getBoundingClientRect();"
            " return {x: r.x, y: r.y, w: r.width, h: r.height}; }")
        if not track or not slider:
            return False, 0.0

        bg_box = data.get("bgBox") or {"w": 300}
        pz_box = data.get("pzBox") or {"w": 52}

        sx = slider["x"] + slider["w"] / 2
        sy = slider["y"] + slider["h"] / 2
        track_w = max(track["w"], 1)
        max_travel = max(bg_box["w"] - pz_box.get("w", 52), 1)
        ratio = track_w / max_travel

        # 人类化靠近
        if self.human:
            await self._mouse("mouseMoved", sx - random.uniform(20, 45),
                              sy + random.uniform(12, 28))
            await asyncio.sleep(random.uniform(0.18, 0.32))
            for i in range(1, 5):
                await self._mouse("mouseMoved",
                                  sx + (i / 5) * random.uniform(-4, 2),
                                  sy + random.uniform(-3, 3))
                await asyncio.sleep(random.uniform(0.02, 0.05))

        await self._mouse("mouseMoved", sx, sy)
        await asyncio.sleep(random.uniform(0.08, 0.18))
        await self._mouse("mousePressed", sx, sy, buttons=1)
        await asyncio.sleep(random.uniform(0.06, 0.14))

        if self.human:
            for _ in range(random.randint(1, 3)):
                await self._mouse("mouseMoved", sx + random.uniform(-1.5, 1.5),
                                  sy + random.uniform(-1.5, 1.5), buttons=1)
                await asyncio.sleep(random.uniform(0.02, 0.05))

        # 主拖动：实时残差修正
        cur_x = sx
        for _ in range(140):
            cur_left = await self.page.evaluate(JS_PZ_LEFT)
            remaining = target_left - cur_left
            if abs(remaining) <= 1.0:
                break
            dx = remaining * ratio
            dist = abs(remaining)
            if dist > 60:
                cap = 22
            elif dist > 20:
                cap = 12
            elif dist > 6:
                cap = 5
            else:
                cap = 2
            dx = max(-cap, min(cap, dx))
            if self.human:
                dx += random.uniform(-0.4, 0.4)
            cur_x = max(track["x"], min(track["x"] + track_w, cur_x + dx))
            await self._mouse("mouseMoved", cur_x, sy + random.uniform(-0.6, 0.6),
                              buttons=1)
            base = 0.010 if dist > 40 else (0.018 if dist > 12 else 0.030)
            await asyncio.sleep(base * random.uniform(0.8, 1.4))

        final = await self.page.evaluate(JS_PZ_LEFT)
        self._d(f"拖动完成 left={final:.1f} target={target_left:.1f}")

        if self.human:
            await asyncio.sleep(random.uniform(0.12, 0.28))
        await self._mouse("mouseReleased", cur_x, sy, buttons=0)
        await asyncio.sleep(random.uniform(3.5, 5.0))
        return abs(final - target_left) <= 3.0, final

    # ---------------------------------------------------------- 检查
    async def check(self) -> dict:
        return await self.page.evaluate(JS_SOLVED)

    # ---------------------------------------------------------- 换题
    async def refresh(self) -> bool:
        """点刷新换题，并等 src 变成新的 data URL。"""
        try:
            rf = self.page.locator(SEL["refresh"])
            if await rf.count() > 0 and await rf.is_visible():
                await rf.click()
                self._d("已点 refresh 换题")
            else:
                await self.expand()
        except Exception:
            await self.expand()

        # 等新图就绪（grab_images 内部会等 src 变成 data URL 或 http URL）
        got = await self.grab_images(wait_src=15.0)
        return got is not None

    # ---------------------------------------------------------- 主流程
    async def solve(self, max_attempts: int = 6) -> SolveResult:
        res = SolveResult(ok=False)
        for attempt in range(1, max_attempts + 1):
            res.attempts = attempt
            if not await self.expand():
                await asyncio.sleep(2)
                continue

            ident = await self.identify()
            if ident is None:
                res.detail = "识别失败"
                self._d(f"第 {attempt} 次识别失败，换题")
                await self.refresh()
                continue

            target, method, cost = ident
            res.target_x = target
            res.method = method
            res.cost += cost

            got = await self.grab_images()
            if got is None:
                res.detail = "取图失败"
                await self.refresh()
                continue
            data = got[2]

            aligned, final_left = await self.drag_to(target, data)
            res.final_x = final_left
            res.trail.append({"attempt": attempt, "target": round(target, 1),
                              "final": round(final_left, 1),
                              "aligned": aligned, "method": method})

            st = await self.check()
            if st["ok"] and "weak" not in st["why"]:
                res.ok = True
                res.detail = st["why"]
                self._d(f"滑块通过（第 {attempt} 次 target={target:.1f} "
                        f"method={method}）")
                return res

            res.detail = st["why"]
            self._d(f"第 {attempt} 次未通过: {st['why']} "
                    f"(target={target:.1f} final={final_left:.1f})")
            await self.refresh()

        return res
