"""阿里云 FeiLin 滑块 —— 纯 CDP 版（不依赖 Playwright）。

逻辑与 ``app/register/aliyun_slider.py`` 等价，只是把 ``page.evaluate``
换成 ``CDPPage.eval``，把 Playwright 的鼠标换成 ``Input.dispatchMouseEvent``。
识别只用 2Captcha（2026-11-17 起：自研本地识别已全部移除）。
``captcha_vision`` 仅用于取图/解码/裁剪（2Captcha 的输入预处理）。

实测要点（沿用旧版结论，全部仍然成立）
------------------------------------
1. 取图必须解码 ``img.src``，不能截图（截图会把透明区合成掉）
2. 拼图块必须裁到 alpha bbox（52×200 画布 81% 是透明）
3. refresh 换题后 src 会变成 ``*.aliyuncs.com`` 远程 URL
4. 拖动必须实时读 ``#aliyunCaptcha-puzzle`` 的 ``style.left`` 做残差修正
5. 拖拽映射不是常数比例：拼图位移 = g(滑块位移) 是加速曲线（见
   ``_DRAG_A/_DRAG_B``，2026-11-17 受控实验标定），开环必须按曲线逆变换
   估算行程，闭环必须低增益小步长（无瞬移）收敛
6. 2Captcha 返回的 x/y 是拼图缺口的**中心**坐标（不是左沿/顶沿），
   2026-11-17 七组独立采样证实（详见 identify 的 docstring）
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
  // 组件 class 是代码控制、与页面语言无关（2026-11-17 acct22 实测：
  // 中文页“验证通过”英文文本匹配漏检 → 空转 7 分钟；class=verified 则稳定）
  const cb = document.getElementById('aliyunCaptcha-captcha-body');
  if (cb && /(^|\s)verified(\s|$)/.test(String(cb.className))) {
    return {ok: true, why: 'cls-verified'};
  }
  const txt = (document.body ? document.body.innerText : '').toLowerCase();
  if ((txt.includes('verified') || txt.includes('验证通过') ||
       txt.includes('驗證通過')) &&
      !txt.includes('unable to verify')) {
    return {ok: true, why: 'text-verified'};
  }
  const err = document.querySelector('[class*=error],[class*=tips-error]');
  if (err && err.innerText) {
    return {ok: false, why: 'error:' + err.innerText.slice(0, 60)};
  }
  if (txt.includes('please complete the captcha') ||
      txt.includes('unable to verify') ||
      txt.includes('请完成人机验证') || txt.includes('請完成人機驗證')) {
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
  // initial 是组件空闲态标记（首次 mousedown 后才移除），不是锁死：
  // 空闲态拖动同样生效（2026-11-18 日志反证 arm=False 首轮拖全生效）。
  // 旧版把 initial 当锁死，导致每轮误触发 heal 篡改 DOM，只查 pointerEvents。
  return getComputedStyle(s).pointerEvents !== 'none';
})()"""

JS_IMG_LOADED = """(() => {
  const i = document.querySelector('#aliyunCaptcha-img');
  const p = document.querySelector('#aliyunCaptcha-puzzle');
  return {img: i ? i.naturalWidth : 0, pz: p ? p.naturalWidth : 0};
})()"""

JS_IMG_LOADED_OK = """(() => {
  const i = document.querySelector('#aliyunCaptcha-img');
  const p = document.querySelector('#aliyunCaptcha-puzzle');
  return !!i && !!p && i.naturalWidth > 0 && p.naturalWidth > 0;
})()"""

JS_RELOAD_IMAGES = r"""(() => {
  const reload = (sel) => {
    const e = document.querySelector(sel);
    if (!e) return 'no-el';
    if (e.naturalWidth > 0) return 'already';
    const u = e.src || '';
    if (!u) return 'no-src';
    e.removeAttribute('src');
    e.src = u;                 // 同 URL 重载：不改 src 来源，无篡改信号
    return 'reload';
  };
  return {img: reload('#aliyunCaptcha-img'),
          puzzle: reload('#aliyunCaptcha-puzzle')};
})()"""


def _img_to_data_url(img) -> str:
    import base64
    import io

    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode()


# ---- 拖拽行程映射（2026-11-17 受控实验标定，qoder.com 注册页几何）----
# 滑块位移 t → 拼图 style.left = g(t) = a·t + b·t²，纯空间映射：
# 跳变与慢拖同值、按住零漂移（与速度/时间无关）。g(260)=260 恰与轨道
# 端点对齐。旧代码按常数 ratio = track/(bg-pz) = 1.21 开环，远端过冲
# 可达 +40px；闭环 dx=remaining×1.21 在末端实际斜率 ≈1.9 处是超增益，
# 振荡后停在 target+6~8px（与生产日志逐位吻合）。
_DRAG_A, _DRAG_B = 0.077, 0.00355


def travel_for_target(target_left: float) -> float:
    """g(t) = a·t + b·t² 的逆变换：目标 left → 滑块行程 t（显示像素）。"""
    if target_left <= 0:
        return 0.0
    disc = _DRAG_A * _DRAG_A + 4 * _DRAG_B * target_left
    if disc <= 0:
        return 0.0
    return (-_DRAG_A + disc ** 0.5) / (2 * _DRAG_B)


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
                 proxy: str | None = None) -> None:
        self.page = page
        self.human = human
        self.log = logger
        self.paid = paid_client
        self.paid_ready = paid_client is not None
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
        """图真没加载（naturalWidth==0）才修，且优先无篡改手段。

        旧版只要 JS_ARMED 为 False 就把 src 改写 dataURL——但 initial 只是
        空闲标记（空闲态可拖），于是每轮都不必要地篡改组件 DOM，src 来源
        异常成为风控可检测信号（2026-11-18 取证：注入轮 100% 吃九宫格）。
        现顺序：① 图已加载直接返回；② 同 URL 重载；③ 仍失败才 dataURL 兜底。
        """
        import json as _json

        loaded = self.page.eval(JS_IMG_LOADED) or {}
        if int(loaded.get("img") or 0) > 0 and int(loaded.get("pz") or 0) > 0:
            return True
        reloaded = self.page.eval(JS_RELOAD_IMAGES) or {}
        self._d(f"同 URL 重载: {reloaded}")
        if self.page.wait_js(JS_IMG_LOADED_OK, timeout=5):
            return True
        try:
            args = {"bg": _img_to_data_url(bg), "pz": _img_to_data_url(pz)}
            out = self.page.eval(f"({JS_INJECT_IMAGES})({_json.dumps(args)})")
            self._d(f"重载无效，兜底注入图片: {out}")
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
    def identify(self, got=None):
        """只走 2Captcha。返回 (target_left, method, cost) 或 None。

        2026-11-17 七组独立采样证实：2Captcha 的 x/y 是拼图缺口的**中心**
        坐标（自然像素），不是左沿/顶沿：
        - y 证据：每题 sol.y - pz alpha bbox_y ≈ 半高 26（25/26/27/32）；
        - x 证据：边缘梯度法测得一题缺口左沿 190/右沿 230，中心 = 210，
          与 2Captcha x=214 差 4px；另一题左沿 188.4、中心 213.9，
          与 x=214 一位不差。
        旧版把 x 当左沿直接换算，每轮恒偏 +25.5×scale px（2026-11-17
        run:one 十连败主因），左沿 = (x - 拼图半宽) × scale。
        """
        if not self.paid_ready:
            self._d("没有 2Captcha 客户端（识别只走 2Captcha，本地识别已移除）")
            return None
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

        # 识别请求偶尔出错（图太大/超时/服务波动），最多重试 3 次
        for try_i in range(1, 4):
            try:
                sol = self.paid.solve(bg, pz_c)
            except Exception as exc:
                self._d(f"2Captcha 异常（第 {try_i}/3）: "
                        f"{type(exc).__name__}: {exc}")
            else:
                if sol.ok:
                    self.total_cost += sol.cost
                    # x/y 是缺口中心（自然像素）：减拼图半宽得左沿，再乘 scale
                    x_left = (sol.x - pw / 2) * scale
                    limit = max(0.0, disp_w - pw * scale)
                    x_left = min(max(0.0, x_left), limit)
                    dy = sol.y - bbox_y
                    prov = getattr(self.paid, "name", "2captcha")
                    self._d(f"{prov} x={sol.x:.0f} y={sol.y:.0f} "
                            f"(bbox_y={bbox_y} dy={dy:.0f}≈半高"
                            f"{pz_c.size[1] // 2}→中心) → 左沿 {x_left:.1f}"
                            f" 耗时={sol.elapsed:.1f}s cost=${sol.cost:.4f}"
                            f"{' 重试' + str(try_i - 1) if try_i > 1 else ''}")
                    return x_left, f"{prov}(c={sol.x:.0f})", sol.cost
                self._d(f"打码失败（第 {try_i}/3）: {sol.error}")
            if try_i < 3:
                time.sleep(1.5)
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

        # ---- 行程初值：加速曲线逆变换（受控实验标定，见模块头 _DRAG_A/B）----
        travel_cap = max(track_w - slider["w"], 1)   # 滑块自身宽度不能出轨道
        t0 = max(0.0, min(travel_for_target(target_left), travel_cap))
        self._d(f"拖动：目标 {target_left:.1f} → 行程初值 {t0:.1f}"
                f"（曲线逆变换，max_travel={travel_cap:.0f}）")

        # ---- 单程缓动（拟人，无瞬移）：1.4~2.0s 滑到行程初值 ----
        cur_x = sx
        duration = random.uniform(1.4, 2.0)
        steps = max(60, int(t0))
        pause_at = random.randint(int(steps * 0.55), int(steps * 0.75))
        for i in range(1, steps + 1):
            t = i / steps
            eased = t * t * (3 - 2 * t)
            x = sx + t0 * eased
            if self.human:
                x += random.uniform(-0.45, 0.45)
            cur_x = x
            self.page.move(cur_x, sy + random.uniform(-0.5, 0.5), buttons=1)
            time.sleep(duration / steps * random.uniform(0.7, 1.3))
            if i == pause_at:
                time.sleep(random.uniform(0.06, 0.14))
        time.sleep(random.uniform(0.12, 0.2))

        # ---- 微步闭环（无瞬移）：低增益 + ≤3px 小步拆 0.4px 子步 ----
        # 旧版 dx=remaining×1.21 在末端实际斜率 ≈1.9 处超增益振荡，停在
        # target+6~8px；实测低增益小步可收敛到 ±1px（跳变/慢拖同值证明
        # 映射纯空间、无时间平滑，闭环每步立即生效，无需等稳定）。
        feedback = False
        for _ in range(30):
            cur_left = float(self.page.eval(JS_PZ_LEFT) or 0.0)
            if cur_left <= 0.5:
                break                      # 反馈不可用（新版本可能不写它）
            feedback = True
            remaining = target_left - cur_left
            if abs(remaining) <= 1.0:
                break
            dx = max(-3.0, min(3.0, remaining * 0.7))
            nsub = max(2, int(abs(dx) / 0.4))
            for j in range(1, nsub + 1):
                self.page.move(cur_x + dx * j / nsub,
                               sy + random.uniform(-0.5, 0.5), buttons=1)
                time.sleep(random.uniform(0.012, 0.02))
            cur_x = max(track["x"], min(track["x"] + track_w, cur_x + dx))
            time.sleep(random.uniform(0.03, 0.06))

        final = float(self.page.eval(JS_PZ_LEFT) or 0.0)
        moved = final if final > 0.5 else (cur_x - sx) / max(ratio, 1e-6)
        self._d(f"拖动完成 left={final:.1f} 估计位移={moved:.1f} "
                f"target={target_left:.1f} 反馈={feedback}")
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
        expand_fails = 0
        for attempt in range(1, max_attempts + 1):
            res.attempts = attempt
            # 先看是否已通过：refresh 换题/点展开按钮都可能触发阿里云的
            # **无感通过**（环境可信直接放行，组件收起、窗口永不展开），
            # 2026-11-17 acct20 实测：expand 空转 15 分钟而页面早已 Verified。
            st = self.check()
            if st.get("ok") and "weak" not in str(st.get("why")):
                res.ok = True
                res.detail = str(st.get("why"))
                self._d(f"滑块已通过（循环开头检测 {st.get('why')}）")
                return res
            if not self.expand():
                # 窗口起不来≠锁定：很可能就是“已通过收起”。再查一次
                st = self.check()
                if st.get("ok") and "weak" not in str(st.get("why")):
                    res.ok = True
                    res.detail = str(st.get("why"))
                    self._d(f"滑块通过（expand 失败后发现 {st.get('why')}）")
                    return res
                expand_fails += 1
                if expand_fails >= 2:
                    # 止损：明确拒绝（错误条）后 refresh 常把组件锁死
                    #（img display:none/滑块 pe:none，怎么点都起不来），
                    # 空转 6 轮纯烧时间。快退出，让上层 Go back 恢复重走。
                    # 2026-11-17 acct22 实测：锁定后空转 7 分钟无进展。
                    res.detail = "expand-lock"
                    self._d("滑块窗口连续 2 次起不来，止损退出")
                    break
                time.sleep(2)
                continue
            expand_fails = 0

            got = self.grab_images()
            if got is None:
                res.detail = "取图失败"
                self.refresh()
                continue
            bg0, pz0, _d0 = got
            # 图真没加载才修；空闲态 initial 不再触发注入（避免无谓 DOM 篡改）
            loaded = self.page.eval(JS_IMG_LOADED) or {}
            if int(loaded.get("img") or 0) <= 0 or int(loaded.get("pz") or 0) <= 0:
                self.heal_images(bg0, pz0)

            ident = self.identify(got)   # 只走 2Captcha
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

            # 判定延迟容忍：still-need-captcha 只是“还没判完”，不是判负。
            # 2026-11-17 acct21 实测：拖后 4s 内 still-need-captcha，实际通过
            # （判定 >4s 才回来）；过早点 refresh 会打断 success 回调，死在
            # 「Verified|Go back」假通过页（无 button 无 form）。所以先延长复查。
            if "still-need-captcha" in str(st.get("why")):
                for _ in range(4):           # 再等最多 12s
                    time.sleep(3)
                    st = self.check()
                    if st.get("ok") and "weak" not in str(st.get("why")):
                        res.ok = True
                        res.detail = str(st.get("why"))
                        self._d(f"滑块通过（延迟判定，第 {attempt} 次 "
                                f"target={target:.1f}）")
                        return res
                    if "still-need-captcha" not in str(st.get("why")):
                        break              # 出现明确错误条，走正常换题

            res.detail = str(st.get("why"))
            self._d(f"第 {attempt} 次未通过: {res.detail} "
                    f"(target={target:.1f} final={final_left:.1f})")
            self.refresh()
        return res
