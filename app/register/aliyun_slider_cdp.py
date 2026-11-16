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
  const box = (e) => {
    if (!e) return null;
    const r = e.getBoundingClientRect();
    return {x: r.x, y: r.y, w: r.width, h: r.height};
  };
  // 真正的显示区域是 .puzzle 容器（img 被设成 display:none）
  const cont = p ? p.parentElement : null;
  return {
    pzSrc: p ? (p.src || '') : '',
    bgSrc: i ? (i.src || '') : '',
    bgNatural: i ? [i.naturalWidth, i.naturalHeight] : null,
    bgBox: box(cont) || box(i),
    pzBox: box(p) || {w: 52, h: 52},
    contCls: cont ? String(cont.className || '') : '',
    imgDisplay: i ? getComputedStyle(i).display : '',
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
  const w = document.querySelector('#aliyunCaptcha-window-float');
  if (!i || !p || !s) return false;
  const shown = !!(w && w.classList.contains('window-show'));
  // 不检查 img 的尺寸：比特浏览器把 <img> 设成 display:none，
  // 真实显示区是 .puzzle 容器；这里只要求「有 src + 窗口已展开 + 滑块可见」
  return shown && !!i.src && !!p.src &&
         s.getBoundingClientRect().width > 0;
})()"""

JS_GRAB_SRCS = r"""(async () => {
  const toDataUrl = async (sel) => {
    const e = document.querySelector(sel);
    if (!e || !e.src) return '';
    const ctl = new AbortController();
    const timer = setTimeout(() => ctl.abort(), 6000);   // 别让代理卡住整轮
    try {
      const r = await fetch(e.src, {cache: 'no-store', signal: ctl.signal});
      const b = await r.blob();
      return await new Promise(res => {
        const fr = new FileReader();
        fr.onload = () => res(String(fr.result || ''));
        fr.onerror = () => res('');
        fr.readAsDataURL(b);
      });
    } catch (err) { return ''; }
    finally { clearTimeout(timer); }
  };
  return {
    bg: await toDataUrl('#aliyunCaptcha-img'),
    pz: await toDataUrl('#aliyunCaptcha-puzzle'),
  };
})()"""


JS_INJECT_IMAGES = r"""(args) => {
  const set = (sel, url) => {
    const e = document.querySelector(sel);
    if (!e) return 'no-el';
    e.src = url;
    return 'set';
  };
  return {img: set('#aliyunCaptcha-img', args.bg),
          puzzle: set('#aliyunCaptcha-puzzle', args.pz)};
}"""

JS_ARMED = """(() => {
  const s = document.querySelector('#aliyunCaptcha-sliding-slider');
  if (!s) return false;
  const cls = String(s.className || '');
  const pe = getComputedStyle(s).pointerEvents;
  return !/initial/.test(cls) && pe !== 'none';
})()"""


def _img_to_data_url(img) -> str:
    import base64
    import io

    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode()


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
    def expand(self, max_try: int = 4) -> bool:
        """确保滑块面板打开且图片就绪。

        实测：`#captcha-element` 是外层容器，点它不一定能展开；
        真正可点的是 `#aliyunCaptcha-captcha-left`（"Click to verify" 药丸）
        和 `#aliyunCaptcha-captcha-body`。所以按顺序试，每次点完等图加载，
        不要连点（连点会把它点开又点关）。
        """
        if self.page.wait_js(JS_IMAGES_READY, timeout=3):
            return True

        candidates = (SEL["collapse"], "#aliyunCaptcha-captcha-body",
                      SEL["captcha_btn"])
        for attempt in range(1, max_try + 1):
            for sel in candidates:
                if not self.page.exists(sel):
                    continue
                r = self.page.rect(sel)
                if not r or r.w < 5 or r.h < 5:
                    continue
                self._d(f"第 {attempt} 轮点 {sel} ({r.w:.0f}x{r.h:.0f})")
                self.page.click_xy(r.cx, r.cy, human=self.human)
                if self.page.wait_js(JS_IMAGES_READY, timeout=10):
                    return True
            # 面板可能被上一轮点关了，给一次纯等待
            if self.page.wait_js(JS_IMAGES_READY, timeout=4):
                return True

        st = self.page.eval(JS_STATE) or {}
        img_ok = self.page.eval(
            "(() => { const i=document.querySelector('#aliyunCaptcha-img');"
            " return i ? [i.complete, i.naturalWidth, "
            "i.getBoundingClientRect().width] : null; })()")
        self._d(f"滑块窗口没起来 state={st} img={img_ok}")
        return False

    def heal_images(self, bg, pz) -> bool:
        """把图注入回 ``<img>``，逼组件进入可拖状态。

        比特浏览器会拦 ``<img>`` 的加载（naturalWidth 恒 0），组件因此一直
        停在 ``pointer-events:none`` / ``slider-move initial``，怎么拖都没反应。
        我们已经在 Python 侧拿到了图，直接以 dataURL 注回去。
        """
        import json as _json

        try:
            args = {"bg": _img_to_data_url(bg), "pz": _img_to_data_url(pz)}
            out = self.page.eval(f"({JS_INJECT_IMAGES})({_json.dumps(args)})")
            self._d(f"注入图片: {out}")
        except Exception as exc:
            self._d(f"注入失败: {type(exc).__name__}: {exc}")
            return False
        ok = self.page.wait_js(JS_ARMED, timeout=8)
        self._d(f"滑块已 arm: {ok}")
        return ok

    # ---------------------------------------------------------- 取图
    def grab_images(self, wait_src: float = 15.0):
        """取背景图与拼图块。

        两条路：
        1. 页面内 fetch 成 dataURL（首选 —— 比特浏览器会拦 <img> 加载，
           naturalWidth 恒 0，但 fetch 是通的）
        2. 退回到 Python 侧按 URL 下载（带代理）
        """
        deadline = time.time() + wait_src
        last = None
        while time.time() < deadline:
            data = self.page.eval(JS_SRCS) or {}
            last = data
            bg_src = (data.get("bgSrc") or "").strip()
            pz_src = (data.get("pzSrc") or "").strip()
            if bg_src and pz_src:
                grabbed = self.page.eval(JS_GRAB_SRCS, await_promise=True) or {}
                bg_d = (grabbed.get("bg") or "")
                pz_d = (grabbed.get("pz") or "")
                if bg_d.startswith("data:") and pz_d.startswith("data:"):
                    bg = captcha_vision.decode_data_url(bg_d)
                    pz = captcha_vision.decode_data_url(pz_d)
                    if bg is not None and pz is not None:
                        self._d("取图: 页面内 fetch 成功")
                        return bg, pz, data
                bg = captcha_vision.load_image_src(bg_src, proxy=self.proxy)
                pz = captcha_vision.load_image_src(pz_src, proxy=self.proxy)
                if bg is not None and pz is not None:
                    self._d("取图: Python 侧下载成功")
                    return bg, pz, data
                self._d(f"图片解码失败 bg={bg_src[:50]} pz={pz_src[:50]}")
            time.sleep(0.8)
        if last:
            self._d(f"等 src 超时 bg={(last.get('bgSrc') or '')[:50]}")
        return None

    # ---------------------------------------------------------- 识别
    def identify(self, got=None, prefer_local: bool = False):
        if got is None:
            got = self.grab_images()
        if got is None:
            return None
        bg, pz, _data = got
        bg_w = bg.size[0]
        pz_c, _mask, bbox = captcha_vision.crop_to_alpha(pz)
        pw = pz_c.size[0]
        bbox_y = bbox[1]

        # 自然像素 → 显示像素的缩放（.puzzle 容器宽 / 图宽）
        disp_w = float((_data.get("bgBox") or {}).get("w") or bg_w or 1)
        scale = (disp_w / bg_w) if bg_w else 1.0
        self._d(f"图宽 {bg_w} 显示宽 {disp_w:.0f} scale={scale:.3f} "
                f"拼图 {pw}x{pz_c.size[1]}")

        def _paid():
            if not self.prefer_paid:
                return None
            try:
                sol = self.paid.solve(bg, pz_c)
                if sol.ok:
                    self.total_cost += sol.cost
                    # 实测（2026-11-16 20:18 通过那次）：2Captcha 的 x 是
                    # **目标左沿**，不是中心。减半宽会偏 25px 左右。
                    x_left = sol.x * scale
                    limit = max(0.0, disp_w - pw * scale)
                    x_left = min(max(0.0, x_left), limit)
                    dy = abs(sol.y - bbox_y)
                    self._d(f"2Captcha x={sol.x:.0f} y={sol.y:.0f} "
                            f"(bbox_y={bbox_y} dy={dy:.0f}) → 左沿 {x_left:.1f}"
                            f" 耗时={sol.elapsed:.1f}s cost=${sol.cost:.4f}")
                    return x_left, f"2captcha(c={sol.x:.0f})", sol.cost
                self._d(f"2Captcha 失败: {sol.error}")
            except Exception as exc:
                self._d(f"2Captcha 异常: {type(exc).__name__}: {exc}")
            return None

        def _local():
            gap = captcha_vision.find_gap(bg, pz, display_w=disp_w)
            if gap.ok:
                return gap.x_display, f"local-{gap.method}", 0.0
            self._d(f"本地识别失败: {gap.detail}")
            return None

        # 两个来源实测会差 40px 左右，交替优先，命中率更高
        order = (_local, _paid) if prefer_local else (_paid, _local)
        for fn in order:
            ident = fn()
            if ident:
                return ident
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
        pz_box = dict(data.get("pzBox") or {"w": 52})
        if float(pz_box.get("w") or 0) < 5:      # 拼图元素被隐藏时给个估计值
            pz_box["w"] = 52
        sx = slider["x"] + slider["w"] / 2
        sy = slider["y"] + slider["h"] / 2
        track_w = max(track["w"], 1)
        max_travel = max(bg_box["w"] - pz_box.get("w", 52), 1)
        ratio = track_w / max_travel
        if not (0.2 <= ratio <= 5.0):        # 布局没量到时兜底
            self._d(f"ratio 异常 {ratio:.2f}（track={track_w} "
                    f"bg={bg_box.get('w')}），按 1.0 处理")
            ratio = 1.0

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
        # ---- 开环拖动（2026-11-16 18:46 实测通过的那版）----
        total = max(-track_w, min(track_w, target_left * ratio))
        duration = random.uniform(1.1, 1.9)
        steps = max(40, int(abs(total) / 2))
        pause_at = random.randint(int(steps * 0.55), int(steps * 0.8))
        self._d(f"开环拖动 total={total:.1f}px steps={steps} "
                f"dur={duration:.2f}s ratio={ratio:.2f}")
        for i in range(1, steps + 1):
            t = i / steps
            eased = t * t * (3 - 2 * t)
            x = sx + total * eased
            if self.human:
                x += random.uniform(-0.35, 0.35)
            cur_x = x
            self.page.move(cur_x, sy + random.uniform(-0.4, 0.4), buttons=1)
            time.sleep(duration / steps * random.uniform(0.75, 1.25))
            if i == pause_at:
                time.sleep(random.uniform(0.08, 0.16))

        # ---- 闭环修正：只有 style.left 真的在变时才用（新版本可能不写它）----
        for _ in range(24):
            cur_left = float(self.page.eval(JS_PZ_LEFT) or 0.0)
            if cur_left <= 0.5:
                break                      # 反馈不可用，跳过
            remaining = target_left - cur_left
            if abs(remaining) <= 1.5:
                break
            dx = max(-8.0, min(8.0, remaining * ratio))
            cur_x = max(track["x"], min(track["x"] + track_w, cur_x + dx))
            self.page.move(cur_x, sy + random.uniform(-0.5, 0.5), buttons=1)
            time.sleep(random.uniform(0.02, 0.04))

        final = float(self.page.eval(JS_PZ_LEFT) or 0.0)
        moved = final if final > 0.5 else (cur_x - sx) / max(ratio, 1e-6)
        self._d(f"拖动完成 left={final:.1f} 估计位移={moved:.1f} "
                f"target={target_left:.1f}")
        if self.human:
            time.sleep(random.uniform(0.35, 0.6))     # 松手前停一下
        self.page.mouse("mouseReleased", cur_x, sy, buttons=0)
        time.sleep(random.uniform(3.0, 4.5))
        return abs(moved - target_left) <= 6.0, moved

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

            got = self.grab_images()
            if got is None:
                res.detail = "取图失败"
                self.refresh()
                continue
            bg0, pz0, _d0 = got
            # 组件没 arm（pointer-events:none / slider-move initial）时，把图注回去
            if not self.page.wait_js(JS_ARMED, timeout=1):
                self.heal_images(bg0, pz0)

            ident = self.identify(got)   # 付费优先，本地仅兜底
            if ident is None:
                res.detail = "识别失败"
                self.refresh()
                continue

            target, method, cost = ident
            res.target_x, res.method = target, method
            res.cost += cost

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
