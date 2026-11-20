"""Baxia 拼图滑块（punish iframe 内的 nocaptcha puzzle）CDP 求解器。

与 FeiLin 滑块（aliyun_slider_cdp）的区别
----------------------------------------
FeiLin 是主文档里的 #aliyunCaptcha-* 组件；本组件在**同源 punish iframe**
（#baxia-dialog-content，src 含 _____tmd_____/punish）里，DOM 特征：
    #nocaptcha / #puzzle-captcha-question-container
    #puzzle-captcha-question-img   背景图（带缺口）
    #puzzle-captcha-btn(+icon)     拖动按钮
2026-11-19 全自动轮首次遇到（puzzleCaptchaGet 接口），旧链路把它误判成
九宫格（no-widget）白烧两轮。

策略：诊断优先——每次尝试先把 iframe 内完整 DOM 结构 + 截图落盘，
DOM 细节未知处用启发式探测，失败也能靠产物离线修正。
识别复用滑块打码通道（超级鹰 9900 / 2Captcha DragAndDrop，solve(bg, pz)
返回缺口中心自然像素坐标）。
"""

from __future__ import annotations

import json
import logging
import random
import time
from dataclasses import dataclass
from pathlib import Path

from app.register import captcha_vision

log = logging.getLogger("baxia-puzzle")

JS_IFRAME_DOC = """(() => {
  const fr = document.getElementById('baxia-dialog-content') ||
    Array.from(document.querySelectorAll('iframe'))
      .find(f => (f.src || '').includes('tmd') || (f.src || '').includes('punish'));
  if (!fr) return null;
  let d = null;
  try { d = fr.contentDocument; } catch (e) { return null; }
  if (!d) return null;
  return true;
})()"""

# 探测 iframe 内结构：所有带 id 元素 + img/canvas 的 src 与几何
JS_DIAG = r"""(() => {
  const fr = document.getElementById('baxia-dialog-content') ||
    Array.from(document.querySelectorAll('iframe'))
      .find(f => (f.src || '').includes('tmd') || (f.src || '').includes('punish'));
  if (!fr) return {error: 'no-iframe'};
  let d = null;
  try { d = fr.contentDocument; } catch (e) { return {error: 'cross-origin'}; }
  if (!d) return {error: 'no-doc'};
  const box = (e) => {
    const r = e.getBoundingClientRect();
    return {x: Math.round(r.x), y: Math.round(r.y),
            w: Math.round(r.width), h: Math.round(r.height)};
  };
  const els = Array.from(d.querySelectorAll('[id]')).map(e => ({
    id: e.id, tag: e.tagName, cls: String(e.className || '').slice(0, 60),
    box: box(e),
    src: e.tagName === 'IMG' ? String(e.src || '').slice(0, 120) : '',
    style: String(e.getAttribute('style') || '').slice(0, 120),
  }));
  const imgs = Array.from(d.querySelectorAll('img')).map(e => ({
    id: e.id, cls: String(e.className || '').slice(0, 60),
    src: String(e.src || '').slice(0, 200),
    nat: [e.naturalWidth, e.naturalHeight], box: box(e),
    display: getComputedStyle(e).display,
  }));
  const canvases = Array.from(d.querySelectorAll('canvas')).map(e => ({
    id: e.id, cls: String(e.className || '').slice(0, 60),
    wh: [e.width, e.height], box: box(e),
  }));
  return {url: fr.src.slice(0, 160), els: els.slice(0, 40),
          imgs: imgs, canvases: canvases,
          bodyText: (d.body ? d.body.innerText : '').slice(0, 200)};
})()"""

# 取背景/拼图块 dataURL + 拖动按钮/拼图块几何（启发式：
# 背景=最大的 img；拼图块=另一张较小的 img 或独立 canvas）
JS_GRAB = r"""(async () => {
  const fr = document.getElementById('baxia-dialog-content') ||
    Array.from(document.querySelectorAll('iframe'))
      .find(f => (f.src || '').includes('tmd') || (f.src || '').includes('punish'));
  if (!fr) return {error: 'no-iframe'};
  const d = fr.contentDocument;
  if (!d) return {error: 'no-doc'};
  // iframe 内 getBoundingClientRect 是 iframe 局部坐标，
  // 拖拽用主页面视口坐标——必须加 iframe 自身偏移（2026-11-19 实测 bug）
  const frBox = fr.getBoundingClientRect();
  const box = (e) => {
    const r = e.getBoundingClientRect();
    return {x: r.x + frBox.x, y: r.y + frBox.y, w: r.width, h: r.height};
  };
  const toData = async (src) => {
    if (!src) return '';
    if (src.startsWith('data:')) return src;
    try {
      const r = await fetch(src, {credentials: 'include'});
      const b = await r.blob();
      return await new Promise((res) => {
        const f = new FileReader();
        f.onload = () => res(String(f.result || ''));
        f.onerror = () => res('');
        f.readAsDataURL(b);
      });
    } catch (e) { return ''; }
  };
  const q = d.getElementById('puzzle-captcha-question-img');
  const imgs = Array.from(d.querySelectorAll('img'))
    .filter(e => e.getBoundingClientRect().width > 5);
  // 背景：question-img 优先，否则面积最大的 img
  let bgEl = q && q.tagName === 'IMG' ? q : null;
  if (!bgEl && imgs.length) {
    bgEl = imgs.slice().sort((a, b) =>
      (b.getBoundingClientRect().width * b.getBoundingClientRect().height) -
      (a.getBoundingClientRect().width * a.getBoundingClientRect().height))[0];
  }
  // 拼图块：除背景外另一张 img（通常更小、带透明）
  let pzEl = imgs.find(e => e !== bgEl) || null;
  const btn = d.getElementById('puzzle-captcha-btn') ||
    d.querySelector('[class*=slider-btn],[class*=slide-btn],[class*=btn]');
  const grab = {
    bgSrc: bgEl ? String(bgEl.src || '') : '',
    bgData: bgEl ? await toData(bgEl.src) : '',
    bgBox: bgEl ? box(bgEl) : null,
    bgNat: bgEl ? [bgEl.naturalWidth, bgEl.naturalHeight] : null,
    pzSrc: pzEl ? String(pzEl.src || '') : '',
    pzData: pzEl ? await toData(pzEl.src) : '',
    pzBox: pzEl ? box(pzEl) : null,
    pzStyle: pzEl ? String(pzEl.getAttribute('style') || '') : '',
    btnBox: btn ? box(btn) : null,
    btnId: btn ? (btn.id || String(btn.className || '').slice(0, 40)) : '',
  };
  return grab;
})()"""

# 成功判定：iframe 消失 / 出现成功 class / 主页面已前进
JS_SOLVED = """(() => {
  const fr = document.getElementById('baxia-dialog-content') ||
    Array.from(document.querySelectorAll('iframe'))
      .find(f => (f.src || '').includes('tmd') || (f.src || '').includes('punish'));
  if (!fr) return {ok: true, why: 'iframe-gone'};
  let d = null;
  try { d = fr.contentDocument; } catch (e) {}
  if (d) {
    const txt = (d.body ? d.body.innerText : '').toLowerCase();
    if (txt.includes('success') || txt.includes('验证通过') ||
        txt.includes('通過')) return {ok: true, why: 'text-success'};
    const suc = d.querySelector('[class*=success],[class*=pass]');
    if (suc && suc.getBoundingClientRect().width > 0)
      return {ok: true, why: 'cls-success'};
  }
  if (fr.getBoundingClientRect().width === 0)
    return {ok: true, why: 'iframe-hidden'};
  return {ok: false, why: 'still-there'};
})()"""


@dataclass(slots=True)
class PuzzleResult:
    ok: bool
    detail: str = ""
    attempts: int = 0
    cost: float = 0.0


class BaxiaPuzzleSlider:
    """punish iframe 内拼图滑块的探测与求解。"""

    def __init__(self, page, paid_client=None, proxy: str | None = None,
                 artifacts_dir: str = "data/artifacts",
                 logger: logging.Logger | None = None):
        self.page = page
        self.paid = paid_client
        self.proxy = proxy
        self.artifacts = Path(artifacts_dir)
        self.log = logger or log

    def _d(self, msg: str) -> None:
        self.log.info("[baxia-puzzle] %s", msg)

    def present(self) -> bool:
        try:
            diag = self.page.eval(JS_DIAG) or {}
        except Exception:  # noqa: BLE001
            return False
        if diag.get("error"):
            return False
        ids = {e.get("id", "") for e in diag.get("els", [])}
        return bool({"puzzle-captcha-question-img",
                     "puzzle-captcha-question-container",
                     "nocaptcha"} & ids)

    def _dump_diag(self, tag: str) -> dict:
        try:
            diag = self.page.eval(JS_DIAG) or {}
        except Exception as exc:  # noqa: BLE001
            diag = {"error": f"{type(exc).__name__}: {exc}"}
        p = self.artifacts / f"baxia_puzzle_{tag}.json"
        try:
            p.write_text(json.dumps(diag, ensure_ascii=False, indent=1),
                         encoding="utf-8")
        except Exception:  # noqa: BLE001
            pass
        try:
            self.page.shot(str(self.artifacts / f"baxia_puzzle_{tag}.png"))
        except Exception:  # noqa: BLE001
            pass
        return diag

    def _grab(self):
        got = self.page.eval(JS_GRAB, await_promise=True) or {}
        if got.get("error"):
            self._d(f"取图失败: {got}")
            return None
        bg = captcha_vision.decode_data_url(got.get("bgData") or "")
        if bg is None and got.get("bgSrc"):
            bg = captcha_vision.load_image_src(got["bgSrc"], proxy=self.proxy)
        pz = captcha_vision.decode_data_url(got.get("pzData") or "")
        if pz is None and got.get("pzSrc"):
            pz = captcha_vision.load_image_src(got["pzSrc"], proxy=self.proxy)
        if bg is None:
            self._d("背景图解码失败")
            return None
        return bg, pz, got

    def _drag(self, btn_box: dict, dx: float) -> None:
        """从按钮中心向右拖 dx（显示像素），拟人轨迹。"""
        sx = btn_box["x"] + btn_box["w"] / 2
        sy = btn_box["y"] + btn_box["h"] / 2
        self.page.move(sx - random.uniform(15, 30),
                       sy + random.uniform(8, 16))
        time.sleep(random.uniform(0.15, 0.3))
        self.page.move(sx, sy)
        time.sleep(random.uniform(0.08, 0.18))
        self.page.mouse("mousePressed", sx, sy, buttons=1)
        time.sleep(random.uniform(0.08, 0.16))
        steps = random.randint(26, 38)
        pause_at = random.randint(int(steps * 0.5), int(steps * 0.8))
        for i in range(1, steps + 1):
            t = i / steps
            eased = 1 - (1 - t) ** 2.0
            x = sx + dx * eased + random.uniform(-0.6, 0.6)
            self.page.move(x, sy + random.uniform(-0.8, 0.8), buttons=1)
            time.sleep(random.uniform(0.014, 0.034))
            if i == pause_at:
                time.sleep(random.uniform(0.08, 0.2))
        time.sleep(random.uniform(0.08, 0.18))
        self.page.mouse("mouseReleased", sx + dx, sy, buttons=0)

    def solve(self, max_attempts: int = 3) -> PuzzleResult:
        res = PuzzleResult(ok=False)
        if self.paid is None:
            res.detail = "无打码客户端"
            return res
        for attempt in range(1, max_attempts + 1):
            res.attempts = attempt
            diag = self._dump_diag(f"attempt{attempt}")
            self._d(f"第 {attempt} 轮 DOM: "
                    f"{[e.get('id') for e in diag.get('els', [])][:12]}")
            got = self._grab()
            if got is None:
                res.detail = "取图失败"
                continue
            bg, pz, grab = got
            if pz is None:
                # 没找到独立拼图块：整图给打码（缺口定位模式）
                self._d("未找到独立拼图块，用背景整图识别缺口")
                pz = bg
            try:
                pz_c, _mask, _bbox = captcha_vision.crop_to_alpha(pz)
            except Exception:  # noqa: BLE001
                pz_c = pz
            bg_w = bg.size[0]
            disp_w = float((grab.get("bgBox") or {}).get("w") or bg_w)
            scale = disp_w / bg_w if bg_w else 1.0
            try:
                sol = self.paid.solve(bg, pz_c)
            except Exception as exc:  # noqa: BLE001
                self._d(f"打码异常: {type(exc).__name__}: {exc}")
                res.detail = f"打码异常 {exc}"
                continue
            if not sol.ok:
                self._d(f"打码失败: {sol.error}")
                res.detail = f"打码失败 {sol.error}"
                continue
            res.cost += sol.cost
            pw = pz_c.size[0]
            # 缺口中心 → 左沿（显示像素）
            x_left = (sol.x - pw / 2) * scale
            # 拼图块初始位置（style.left 或相对背景盒的 x 偏移）
            pz_box = grab.get("pzBox") or {}
            bg_box = grab.get("bgBox") or {}
            init_left = 0.0
            st = grab.get("pzStyle") or ""
            import re
            m = re.search(r"left:\s*([\d.]+)px", st)
            if m:
                init_left = float(m.group(1))
            elif pz_box and bg_box:
                init_left = float(pz_box.get("x", 0)) - float(bg_box.get("x", 0))
            dx = x_left - init_left
            btn = grab.get("btnBox")
            if not btn or btn.get("w", 0) <= 0:
                res.detail = "找不到拖动按钮"
                self._d(f"btn 缺失 grab={ {k: v for k, v in grab.items() if k.endswith('Box')} }")
                continue
            limit = max(0.0, disp_w - pw * scale)
            dx = min(max(0.0, dx), limit + btn["w"])
            self._d(f"缺口中心 x={sol.x:.0f} scale={scale:.3f} "
                    f"左沿={x_left:.1f} init={init_left:.1f} dx={dx:.1f} "
                    f"btn={grab.get('btnId')} cost=${sol.cost:.4f}")
            self._drag(btn, dx)
            # 判定
            for _ in range(6):
                time.sleep(random.uniform(0.7, 1.2))
                try:
                    st2 = self.page.eval(JS_SOLVED) or {}
                except Exception:  # noqa: BLE001
                    st2 = {}
                if st2.get("ok"):
                    res.ok = True
                    res.detail = f"{st2.get('why')} dx={dx:.1f}"
                    self._d(f"拼图滑块通过: {res.detail}")
                    return res
            self._d(f"第 {attempt} 轮拖完未通过，换题重试")
            time.sleep(random.uniform(1.0, 2.0))
        res.detail = res.detail or f"{max_attempts} 轮未通过"
        # 兜底：punish 弹窗除拖动外还有「跳过」入口（用户提示），
        # 拖动全失败时点跳过再看是否放行
        try:
            skipped = self.page.eval("""(() => {
              const fr = document.getElementById('baxia-dialog-content') ||
                Array.from(document.querySelectorAll('iframe'))
                  .find(f => (f.src || '').includes('tmd') || (f.src || '').includes('punish'));
              if (!fr) return false;
              const d = fr.contentDocument;
              if (!d) return false;
              const els = Array.from(d.querySelectorAll('button,a,[role=button],div,span'));
              const hit = els.find(e => ['跳过', 'skip']
                .includes((e.innerText || '').trim().toLowerCase())
                && e.getBoundingClientRect().width > 0);
              if (!hit) return false;
              hit.click();
              return true;
            })()""")
            if skipped:
                self._d("拖动未过，点了 punish 弹窗「跳过」兜底")
                for _ in range(4):
                    time.sleep(1.5)
                    st = self.page.eval(JS_SOLVED) or {}
                    if st.get("ok"):
                        res.ok = True
                        res.detail = f"skip-fallback {st.get('why')}"
                        return res
        except Exception as exc:  # noqa: BLE001
            self._d(f"跳过兜底异常: {exc}")
        self._dump_diag("fail")
        return res