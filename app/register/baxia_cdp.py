"""阿里 Baxia 点选验证码（captchaclick）的 CDP 侧处理。

页面特征
--------
注册提交后若被风控盯上，会出现同源 iframe：

    <iframe id="baxia-dialog-content"
            src="https://qoder.com//api/v1/users/_____tmd_____/punish?...&action=captchaclick">

里面是 9 宫格点选：
- ``#click-question-canvas``  题干（画在 canvas 上，需截图给打码员看）
- ``#click-grid-0..8``        3×3 的图块（120×120）
- ``#click-submit``           提交
- ``#click-refresh``          换题

流程
----
1. 把「题干 + 九宫格」在 iframe 内拼成一张 360×(48+360) 的图
2. 交给 2Captcha ``CoordinatesTask`` 拿点击点
3. 按点换算成格子索引，用真实鼠标事件点 DOM 中心（比点截图坐标稳）
4. 点 Submit，验证 iframe 消失 / 拿到 x5sec cookie
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field

from PIL import Image

from app.net.twocaptcha_click import TwoCaptchaClick, data_url_to_bytes

IFRAME_ID = "baxia-dialog-content"
CELL = 120

JS_CAPTURE = r"""(async () => {
  const CELL = 120;
  const GRID_SCALE = 1.6;          // 九宫格放大（坐标按这个算）
  const Q_SCALE = 3;               // 题干单独放大 3 倍给打工人看
  const AVG_FRAMES = 30;           // 目标图案 canvas 逐帧平均帧数
  const AVG_STEP = 35;             // 每帧间隔 ms
  const fr = document.getElementById('baxia-dialog-content') ||
    Array.from(document.querySelectorAll('iframe'))
      .find(f => (f.src || '').includes('tmd') || (f.src || '').includes('punish'));
  if (!fr) return {error: 'no-iframe'};
  let d = null;
  try { d = fr.contentDocument; } catch (e) { return {error: 'cross-origin'}; }
  if (!d) return {error: 'no-doc'};

  const q = d.getElementById('click-question-canvas');
  const grids = [];
  for (let i = 0; i < 9; i++) {
    const g = d.getElementById('click-grid-' + i);
    if (g) grids.push(g);
  }
  if (!q || grids.length === 0) return {error: 'no-widget',
    ids: Array.from(d.querySelectorAll('[id]')).map(e => e.id).slice(0, 30)};

  const GH = Math.round(CELL * GRID_SCALE);
  const W = GH * 3;
  const gc = d.createElement('canvas');
  gc.width = W; gc.height = GH * 3;
  const gctx = gc.getContext('2d');
  gctx.fillStyle = '#fff'; gctx.fillRect(0, 0, gc.width, gc.height);
  gctx.imageSmoothingEnabled = true;
  grids.forEach((g, i) => {
    const gx = (i % 3) * GH, gy = Math.floor(i / 3) * GH;
    try { gctx.drawImage(g, gx, gy, GH, GH); } catch (e) {}
  });

  // 「要匹配的图案」是另一个无 id 的方形 canvas（实测 80x80）。
  // 实测它被**逐帧随机噪点**覆盖（单帧截图 = 纯噪点，打工人无法解题），
  // 帧间平均后噪点抵消、恒定图案浮现（2026-11-17 实测重建出清晰 QR 码）。
  const descCanvas = Array.from(d.querySelectorAll('canvas')).find(
    c => !/^click-grid-/.test(c.id) && c.id !== 'click-question-canvas' &&
         c.width > 20);
  let descAvg = null, descW = 0, descH = 0;
  if (descCanvas) {
    const w = descCanvas.width, h = descCanvas.height;
    const acc = new Float64Array(w * h * 3);
    const tmp = d.createElement('canvas');
    tmp.width = w; tmp.height = h;
    const tctx = tmp.getContext('2d', {willReadFrequently: true});
    let n = 0;
    for (let i = 0; i < AVG_FRAMES; i++) {
      tctx.clearRect(0, 0, w, h);
      tctx.drawImage(descCanvas, 0, 0);
      const px = tctx.getImageData(0, 0, w, h).data;
      for (let p = 0, q = 0; p < px.length; p += 4, q += 3) {
        acc[q] += px[p]; acc[q + 1] += px[p + 1]; acc[q + 2] += px[p + 2];
      }
      n++;
      await new Promise(r => setTimeout(r, AVG_STEP));
    }
    const avg = d.createElement('canvas');
    avg.width = w; avg.height = h;
    const actx = avg.getContext('2d');
    const aimg = actx.createImageData(w, h);
    const ad = aimg.data;
    for (let p = 0, q = 0; p < ad.length; p += 4, q += 3) {
      ad[p] = acc[q] / n; ad[p + 1] = acc[q + 1] / n;
      ad[p + 2] = acc[q + 2] / n; ad[p + 3] = 255;
    }
    actx.putImageData(aimg, 0, 0);
    descAvg = avg; descW = w; descH = h;
  }

  const qw = (q.width || 355) * Q_SCALE;
  const qh = (q.height || 48) * Q_SCALE;
  const DS = 4;
  const dw = descAvg ? descW * DS : 0;
  const dh = descAvg ? descH * DS : 0;
  const W2 = Math.max(qw, dw);
  const qc = d.createElement('canvas');
  qc.width = W2; qc.height = qh + dh;
  const qctx = qc.getContext('2d');
  qctx.fillStyle = '#fff'; qctx.fillRect(0, 0, qc.width, qc.height);
  qctx.imageSmoothingEnabled = true;
  try { qctx.drawImage(q, 0, 0, qw, qh); } catch (e) {}
  if (descAvg) {
    try { qctx.drawImage(descAvg, 0, qh, dw, dh); } catch (e) {}
  }

  // 2Captcha 限制 100KB，按体积自适应压成 JPEG
  const compress = (canvas) => {
    let url = '';
    for (const quality of [0.9, 0.75, 0.6, 0.45, 0.35]) {
      url = canvas.toDataURL('image/jpeg', quality);
      if (url.length < 128000) break;
    }
    return url;
  };

  const frRect = fr.getBoundingClientRect();
  const center = (el) => {
    const r = el.getBoundingClientRect();
    return {x: frRect.left + r.left + r.width / 2,
            y: frRect.top + r.top + r.height / 2};
  };
  const submit = d.getElementById('click-submit');
  const refresh = d.getElementById('click-refresh');
  return {
    dataUrl: compress(gc),            // 九宫格（坐标对它算）
    questionUrl: compress(qc),        // 题干（放大 3 倍给打工人看）
    layout: {w: W, qh: 0, cell: GH},
    gridRects: grids.map((g, i) => Object.assign({i}, center(g))),
    submit: submit ? center(submit) : null,
    refresh: refresh ? center(refresh) : null,
    questionW: qw, questionH: qh,
  };
})()"""

JS_STATE = r"""(() => {
  const fr = document.getElementById('baxia-dialog-content') ||
    Array.from(document.querySelectorAll('iframe'))
      .find(f => (f.src || '').includes('tmd') || (f.src || '').includes('punish'));
  const cookies = document.cookie || '';
  return {
    iframe: !!fr,
    visible: !!(fr && fr.offsetWidth),
    x5sec: /x5sec=/.test(cookies),
    url: location.href,
    body: (document.body ? document.body.innerText : '').slice(0, 200),
  };
})()"""


JS_IFRAME_RECT = r"""(() => {
  const fr = document.getElementById('baxia-dialog-content') ||
    Array.from(document.querySelectorAll('iframe'))
      .find(f => (f.src || '').includes('tmd') || (f.src || '').includes('punish'));
  if (!fr) return {error: 'no-iframe'};
  const r = fr.getBoundingClientRect();
  return {x: r.left, y: r.top, w: r.width, h: r.height};
})()"""


JS_CENTERS = r"""(() => {
  const fr = document.getElementById('baxia-dialog-content') ||
    Array.from(document.querySelectorAll('iframe'))
      .find(f => (f.src || '').includes('tmd') || (f.src || '').includes('punish'));
  if (!fr) return {error: 'no-iframe'};
  let d = null;
  try { d = fr.contentDocument; } catch (e) { return {error: 'cross-origin'}; }
  if (!d) return {error: 'no-doc'};
  const frRect = fr.getBoundingClientRect();
  const center = (el) => {
    const r = el.getBoundingClientRect();
    return {x: frRect.left + r.left + r.width / 2,
            y: frRect.top + r.top + r.height / 2,
            w: r.width, h: r.height};
  };
  const grids = [];
  const selected = [];
  const win = d.defaultView;
  for (let i = 0; i < 9; i++) {
    const g = d.getElementById('click-grid-' + i);
    if (g) {
      const cls = (g.className || '') + '';
      const sop = g.style.opacity || '';
      let cop = '';
      try { cop = win.getComputedStyle(g).opacity; } catch (e) {}
      grids.push(Object.assign({i, cls, sop, cop}, center(g)));
      // 选中态从宽判定：内联 opacity / computed opacity / class 任一命中。
      // 实测（2026-11-17 夜 23 连败）：只用内联 style.opacity 时，组件在
      // 点击后的重渲染/transition 中会把内联值清掉，selected 读数丢失，
      // 补点逻辑按假读数乱点，实际选择集合错 → 服务端判拒。
      if (sop === '0' || cop === '0' ||
          /select|chosen|active|check/i.test(cls)) selected.push(i);
    }
  }
  const sub = d.getElementById('click-submit');
  const ref = d.getElementById('click-refresh');
  return {grids, selected, submit: sub ? center(sub) : null,
          refresh: ref ? center(ref) : null};
})()"""


@dataclass(slots=True)
class BaxiaResult:
    ok: bool
    cells: list[int] = field(default_factory=list)
    points: list[tuple[float, float]] = field(default_factory=list)
    detail: str = ""
    cost: float = 0.0
    image_path: str = ""


def points_to_cells(points: list[tuple[float, float]],
                    layout: dict) -> list[int]:
    """把打码员给的坐标换算成格子索引（0..8），去重保序。"""
    qh = float(layout.get("qh", 48))
    cell = float(layout.get("cell", CELL))
    out: list[int] = []
    for x, y in points:
        if y < qh - 2:          # 点在题干上，忽略
            continue
        col = int(x // cell)
        row = int((y - qh) // cell)
        if 0 <= col < 3 and 0 <= row < 3:
            idx = row * 3 + col
            if idx not in out:
                out.append(idx)
    return out


class BaxiaClickCaptcha:
    def __init__(self, page, logger=None) -> None:
        self.page = page
        self.log = logger

    def _d(self, msg: str) -> None:
        if self.log:
            self.log.info("[baxia] %s", msg)

    # ---------------------------------------------------------- 状态
    def present(self) -> bool:
        st = self.page.eval(JS_STATE) or {}
        return bool(st.get("iframe") and st.get("visible"))

    def solved(self) -> bool:
        st = self.page.eval(JS_STATE) or {}
        # 注意：九宫格是 OTP 后的二次验证，弹窗期间主页面仍是
        # 「验证邮箱」文本——不能用页面文本判通过（2026-11-18 实测误判）。
        # 只信 x5sec cookie 或 iframe 不可见/消失。
        return bool(st.get("x5sec")) or not st.get("visible")

    # ---------------------------------------------------------- 取图
    def capture(self, retries: int = 5) -> dict:
        """取图。换题/提交后组件会重建，这里等一下再取，避免拿到 no-widget。

        JS_CAPTURE 是 async（含 30 帧噪点平均，约 1.1s），必须 await_promise。
        """
        data: dict = {}
        for i in range(1, retries + 1):
            data = self.page.eval(JS_CAPTURE, await_promise=True) or {}
            if not data.get("error"):
                return data
            self._d(f"取图第 {i} 次: {data.get('error')}")
            time.sleep(1.2)
        self._d(f"取图失败: {data}")
        return data

    # ---------------------------------------------------------- 点击
    def centers(self) -> dict:
        return self.page.eval(JS_CENTERS) or {}

    def click_cells(self, cells: list[int],
                    grid_rects: list[dict] | None = None) -> list[int]:
        """逐格点击。每点一次重新取坐标，并**校验选中态**，没选中就补点。

        实测：选中一格后布局会重排（底部出现选中条），用旧坐标点第二格会点空。
        """
        clicked: list[int] = []
        time.sleep(0.8)   # 组件初始化/入场动画期间点击会被忽略，先等稳定
        for idx in cells:
            for _ in range(3):
                data = self.centers()
                grids = {int(g["i"]): g for g in (data.get("grids") or [])}
                g = grids.get(int(idx))
                if not g:
                    break
                self.page.click_xy(float(g["x"]), float(g["y"]), human=True)
                # 诊断：高频采样选中态——区分「从未选中」(点不中)
                # 与「闪现后撤销」(行为判废)；并查点击点最上层元素
                flash = False
                for _s in range(10):
                    time.sleep(0.15)
                    if int(idx) in (self.centers().get("selected") or []):
                        flash = True
                        break
                time.sleep(0.4)
                after = self.centers()
                if int(idx) in (after.get("selected") or []):
                    clicked.append(int(idx))
                    break
                hit = self.page.eval(f"""(() => {{
                  const fr = document.getElementById('baxia-dialog-content');
                  if (!fr || !fr.contentDocument) return 'no-doc';
                  const r = fr.getBoundingClientRect();
                  const el = fr.contentDocument.elementFromPoint(
                    {float(g["x"])} - r.left, {float(g["y"])} - r.top);
                  return el ? (el.id || el.tagName + '.' + el.className) : 'null';
                }})()""")
                self._d(f"格 {idx} 第 {_} 轮: flash={flash} 点后仍在={int(idx) in (after.get('selected') or [])} 命中元素={hit}")
                # 3 轮补点里每一轮的格子真实状态（诊断选中态判定）
                st = {int(x["i"]): (x.get("cls", "")[:24],
                                    f"sop={x.get('sop')} cop={x.get('cop')}")
                      for x in (after.get("grids") or [])}
                self._d(f"格 {idx} 第 {_} 轮未确认选中，全格状态 {st}")
                time.sleep(1.0)   # 被忽略时多等一拍再补点
            time.sleep(0.2)
        return clicked

    def screenshot_instructions(self, scale: float = 3.0,
                                height: float = 130) -> bytes | None:
        """截 iframe 顶部题面区域（题干文字 + 目标图案的渲染结果）。

        题干 canvas 与目标图案 canvas 都是**逐帧随机噪点**动画（单帧截图
        = 噪点糊成一片，打码员读不出描述，只能瞎猜格子 —— 2026-11-17
        实测：单帧读不出，2Captcha 答错格；10 帧平均后描述清晰浮现
        "phone case with maple leaf"）。所以这里连拍 AVG 张、逐像素平均
        再压回 JPEG。噪点零均值，恒定文字/图案帧间抵消。
        """
        rect = self.page.eval(JS_IFRAME_RECT) or {}
        if not rect or rect.get("error"):
            return None
        import base64
        import io

        frames = []
        for _ in range(10):
            try:
                res = self.page.cdp.call("Page.captureScreenshot", {
                    "format": "png",
                    "clip": {"x": rect["x"], "y": max(0.0, rect["y"]),
                             "width": rect["w"], "height": height,
                             "scale": scale},
                })
                frames.append(Image.open(io.BytesIO(
                    base64.b64decode(res["data"]))))
            except Exception as exc:
                self._d(f"题面截图失败: {type(exc).__name__}")
                return None
            time.sleep(0.12)
        if not frames:
            return None
        try:
            import numpy as np

            arr = np.stack([np.asarray(f.convert("RGB"), dtype=np.float64)
                            for f in frames])
            avg = Image.fromarray(arr.mean(axis=0).round().astype("uint8"))
        except Exception:
            avg = frames[-1].convert("RGB")   # numpy 不可用时退回单帧
        buf = io.BytesIO()
        for quality in (0.9, 0.75, 0.6):
            buf = io.BytesIO()
            avg.save(buf, format="JPEG", quality=int(quality * 100))
            if buf.tell() < 95000:   # GridTask imgInstructions 限 100kB
                break
        return buf.getvalue()

    def selected(self) -> list[int]:
        return list(self.centers().get("selected") or [])

    def click_submit(self, pos: dict | None = None) -> bool:
        """重新取一次 submit 坐标再点（提交前布局同样会变）。"""
        data = self.centers()
        p = data.get("submit") or pos
        if not p:
            return False
        self.page.click_xy(float(p["x"]), float(p["y"]), human=True)
        return True

    # ---------------------------------------------------------- 主流程
    def solve(self, click_client: TwoCaptchaClick, max_attempts: int = 3,
              save_path: str = "") -> BaxiaResult:
        res = BaxiaResult(ok=False)
        comment = ("body 图是 3x3 九宫格照片。imgInstructions 图：上面是题干文字，"
                   "下面是从动态噪点中多帧平均重建的目标图案（可能呈文字/"
                   "二维码/图形状，描述文字已降噪清晰可读）。"
                   "请在九宫格中选出所有包含该目标图案的格子，返回这些格子中心点在 body 图中的坐标"
                   "（可以多个）。若一个都没有，返回空数组（页面会直接点 Submit）。")
        grid_comment = ("The instruction image shows the task in Chinese: "
                        "top line = 'select all pictures matching the description', "
                        "the pink band describes the target object "
                        "(e.g. 带蜜蜂的帽子 = hat with a bee). "
                        "Select ALL cells of the 3x3 grid containing the described "
                        "object. If none matches, press 'No matching images'.")
        for attempt in range(1, max_attempts + 1):
            if attempt > 1 and self.solved():
                # 上一轮其实已通过（僵尸 iframe / 页面进下一步），别再烧题分
                res.ok = True
                res.detail = "通过（页面已进下一步）"
                self._d("通过（重试前检测验证码已通过）")
                return res
            data = self.capture()
            if data.get("error"):
                if self.solved():
                    res.ok = True
                    res.detail = "通过（页面已进下一步）"
                    return res
                res.detail = f"取图失败 {data.get('error')}"
                return res

            png = data_url_to_bytes(data["dataUrl"])
            # 题面优先用**真实截图**（canvas 导出常是空白/噪点）；
            # height=84 只截题干+描述带，不含格子边缘（GridTask 提示图更干净）
            q_png = self.screenshot_instructions(height=84) or (
                data_url_to_bytes(data["questionUrl"])
                if data.get("questionUrl") else None)
            if save_path:
                out = save_path.replace(".png", f"_{attempt}.png")
                try:
                    with open(out, "wb") as fh:
                        fh.write(png)
                    if q_png:
                        with open(out.replace(".png", "_q.png"), "wb") as fh:
                            fh.write(q_png)
                    res.image_path = out
                except Exception:
                    pass

            sol = None
            cells: list[int] = []
            for round_i in range(1, 3):        # 同一张图最多问两次（工人偶发失败）
                try:
                    # 首选 GridTask：工人对编号格子直接答格号，无换算误差
                    try:
                        g = click_client.solve_grid(
                            png, grid_comment, img_instructions=q_png)
                        cells = [c - 1 for c in g.cells]
                        sol = g
                        self._d(f"第 {attempt} 次 GridTask → 格子={cells} "
                                f"cost=${g.cost:.4f}")
                    except Exception as gexc:
                        self._d(f"GridTask 失败回退 Coordinates: {str(gexc)[:60]}")
                        sol = click_client.solve(png, comment,
                                                 img_instructions=q_png)
                    break
                except Exception as exc:
                    msg = str(exc)
                    res.detail = f"2Captcha 失败: {msg[:80]}"
                    self._d(f"第 {attempt} 次第 {round_i} 问失败: {msg[:60]}")
                    if "could not solve" in msg.lower() and round_i == 1:
                        time.sleep(2)
                        continue
                    return res
            if sol is None:
                return res

            res.cost += sol.cost
            if not cells:
                res.points = sol.points
                cells = points_to_cells(sol.points, data.get("layout") or {})
            res.cells = cells
            self._d(f"第 {attempt} 次: 格子={cells} cost=${sol.cost:.4f}")

            clicked = self.click_cells(cells)
            sel = self.selected()
            self._d(f"已选 {sel}（期望 {cells}，点到 {clicked}）")
            if sorted(sel) != sorted(cells):
                missing = [c for c in cells if c not in sel]
                extra = [c for c in sel if c not in cells]
                if missing:
                    self.click_cells(missing)
                if extra:
                    self.click_cells(extra)
                sel = self.selected()
                self._d(f"修正后已选 {sel}")
            # 选中条滑入有布局动画：等稳定再点 submit；
            # 点完若选中仍在且未通过 = submit 没点中，补点（实测破绽）
            time.sleep(0.8)
            ok = False
            for sub_try in range(3):
                self.click_submit(data.get("submit"))
                for _ in range(10):          # 每轮最多等 5s 判定
                    time.sleep(0.5)
                    if self.solved():
                        ok = True
                        break
                if ok:
                    break
                if sorted(self.selected()) != sorted(cells):
                    break                    # 选中被清/换题 → 服务端判拒
                self._d(f"submit 第 {sub_try + 1} 次未生效（选中仍在），补点")
            if ok:
                res.ok = True
                res.detail = "通过"
                self._d("通过（iframe 消失 / 拿到 x5sec）")
                return res

            st = self.page.eval(JS_STATE) or {}
            self._d(f"未通过，页面提示: {st.get('body', '')[:80]} 选中={self.selected()}")
            # 换题：用最新坐标点 refresh
            c2 = self.centers()
            ref = c2.get("refresh")
            if ref:
                self.page.click_xy(float(ref["x"]), float(ref["y"]),
                                   human=True)
                time.sleep(2.5)
        res.detail = res.detail or "重试耗尽"
        return res
