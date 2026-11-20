"""超级鹰（chaojiying.com）打码通道 —— 试验性接入，2Captcha 原逻辑保留。

官方接口（https://www.chaojiying.com/api-5.html）
------------------------------------------------
POST https://upload.chaojiying.net/Upload/Processing.php
  user / pass(或 pass2=密码md5) / softid / codetype / file_base64
返回 {"err_no":0, "err_str":"OK", "pic_id":"...", "pic_str":"...", "md5":"..."}
err_no==0 即扣费；答案错误可在 3 分钟内调 ReportError.php 返分。

本项目用法
----------
| 场景       | codetype | 说明                                        |
|------------|----------|---------------------------------------------|
| 滑块缺口   | 9900     | AI 图形块定位，返回矩形(x1,y1,x2,y2)按可信度排序 |
| 九宫格点选 | 9006     | 人工坐标(1~6个)，题面+提示+格子合成一张图上传   |

适配器与 2Captcha 客户端**同构**（鸭子类型），可直接替换：
- ``ChaojiyingSlider.solve(bg, pz)``  → DragSolution（x/y=缺口中心，自然像素）
- ``ChaojiyingClick.solve_grid(...)`` → ClickSolution（cells 1 起）
- ``ChaojiyingClick.solve(...)``      → ClickSolution（points）

认证
----
标准方式 user+pass+softid（secrets: captcha.chaojiying.user/password/softid）。
只配 key 时按 key 模式尝试（key 当 pass 传）；key 是否被官方接受由
tools/chaojiying_probe.py 实测确认，失败会明确报错并回退 2Captcha。
"""

from __future__ import annotations

import base64
import hashlib
import io
import time

from PIL import Image, ImageDraw, ImageFont

from app.net.http import no_env_session
from app.net.twocaptcha_click import ClickSolution
from app.net.twocaptcha_dragdrop import DragSolution

UPLOAD = "https://upload.chaojiying.net/Upload/Processing.php"
REPORT = "https://upload.chaojiying.net/Upload/ReportError.php"
SCORE = "https://upload.chaojiying.net/Upload/GetScore.php"

MAX_BYTES = 900_000          # 官方限 1M，留点余量


class ChaojiyingError(RuntimeError):
    def __init__(self, msg: str, code: int | str = ""):
        super().__init__(msg)
        self.code = code


def _img_bytes(image, fmt: str = "PNG") -> bytes:
    if isinstance(image, (bytes, bytearray)):
        return bytes(image)
    buf = io.BytesIO()
    image.save(buf, format=fmt)
    return buf.getvalue()


def _jpeg_under(image: Image.Image, limit: int = MAX_BYTES) -> bytes:
    """压到限重以内（超级鹰推荐 jpg，且分辨率比例要正常）。"""
    img = image.convert("RGB")
    for quality in (90, 75, 60, 45):
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=quality)
        if buf.tell() <= limit:
            return buf.getvalue()
    # 还超就缩尺寸
    while True:
        img = img.resize((max(1, img.width * 3 // 4),
                          max(1, img.height * 3 // 4)))
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=60)
        if buf.tell() <= limit or img.width < 100:
            return buf.getvalue()


def _cjk_font(size: int):
    """中文字体（超级鹰工人要读中文提示）：Windows 系统字体优先。"""
    import os

    windir = os.environ.get("WINDIR", r"C:\Windows")
    for name in ("msyh.ttc", "msyhbd.ttc", "simhei.ttf", "simsun.ttc"):
        p = os.path.join(windir, "Fonts", name)
        if os.path.exists(p):
            try:
                return ImageFont.truetype(p, size)
            except Exception:
                continue
    return ImageFont.load_default()


def parse_points(pic_str: str) -> list[tuple[float, float]]:
    """"x1,y1|x2,y2" → [(x1,y1),(x2,y2)]；非法段跳过。"""
    pts: list[tuple[float, float]] = []
    for seg in str(pic_str or "").split("|"):
        parts = seg.strip().split(",")
        if len(parts) != 2:
            continue
        try:
            pts.append((float(parts[0]), float(parts[1])))
        except ValueError:
            continue
    return pts


class ChaojiyingClient:
    """超级鹰 HTTP 标准接口封装（认证/上传/返分/查题分）。"""

    name = "chaojiying"

    def __init__(self, user: str = "", password: str = "", softid: str = "",
                 key: str = "", timeout: int = 90) -> None:
        self.user = str(user or "")
        self.password = str(password or "")
        self.softid = str(softid or "")
        self.key = str(key or "")
        self.timeout = timeout
        if not (self.user and self.password) and not self.key:
            raise ChaojiyingError("超级鹰未配置（user+password 或 key）")

    # ---------------------------------------------------------- 认证
    def _auth(self) -> dict:
        if self.user and self.password:
            d = {"user": self.user,
                 "pass2": hashlib.md5(self.password.encode()).hexdigest()}
            if self.softid:
                d["softid"] = self.softid
            return d
        # key 模式：官方标准接口是账号密码认证，key 先按 pass 传，
        # 是否可用由探针实测；不行会返回明确 err_str。
        return {"user": self.user or self.key, "pass": self.key}

    def _post(self, url: str, fields: dict, retries: int = 2) -> dict:
        last = ""
        session = no_env_session()
        try:
            for attempt in range(retries + 1):
                try:
                    r = session.post(url, data=fields, timeout=self.timeout,
                                     proxies={"http": "", "https": ""})
                    return r.json()
                except Exception as exc:
                    last = f"{type(exc).__name__}: {exc}"
                if attempt < retries:
                    time.sleep(1.5 * (attempt + 1))
        finally:
            session.close()
        raise ChaojiyingError(f"请求失败: {last}")

    # ---------------------------------------------------------- 基础接口
    def score(self) -> dict:
        d = self._post(SCORE, self._auth())
        if d.get("err_no") != 0:
            raise ChaojiyingError(
                f"查题分失败: {d.get('err_str')}", d.get("err_no", ""))
        return d

    def upload(self, image, codetype: int, str_debug: str = "") -> dict:
        """上传识别。err_no==0 即扣费，返回原始 dict（pic_str/pic_id）。"""
        data = _img_bytes(image)
        if len(data) > MAX_BYTES:
            data = _jpeg_under(Image.open(io.BytesIO(data)))
        fields = self._auth()
        fields.update({"codetype": str(codetype),
                       "file_base64": base64.b64encode(data).decode()})
        if str_debug:
            fields["str_debug"] = str_debug
        d = self._post(UPLOAD, fields)
        if d.get("err_no") != 0:
            raise ChaojiyingError(
                f"识别失败: {d.get('err_str')}", d.get("err_no", ""))
        return d

    def report_error(self, pic_id: str) -> bool:
        """报错返分（仅限确认答案错误、且拿到 pic_id 3 分钟内）。"""
        if not pic_id:
            return False
        fields = self._auth()
        fields["id"] = str(pic_id)
        try:
            d = self._post(REPORT, fields, retries=0)
            return d.get("err_no") == 0
        except ChaojiyingError:
            return False


# ------------------------------------------------------------ 滑块适配器
class ChaojiyingSlider:
    """与 TwoCaptchaDragDrop 同构：solve(bg, pz) → DragSolution。

    9900 返回背景上所有图形块矩形（按可信度排序）。缺口筛选利用已知
    几何：拼图块显示 y = 缺口 y（阿里云滑块同高），拼图 alpha bbox 宽
    ≈ 缺口宽。命中多个时取「y 对齐 + 宽度接近」综合评分最高者。
    返回缺口**中心**坐标（与 2Captcha DragAndDropTask 语义一致，
    aliyun_slider_cdp.identify 按中心换算左沿）。
    """

    name = "chaojiying"

    def __init__(self, client: ChaojiyingClient) -> None:
        self.cli = client
        self.last_pic_id = ""

    def balance(self) -> float:
        """超级鹰以题分计费，这里返回题分数（日志用，非美元）。"""
        d = self.cli.score()
        return float(d.get("tifen", 0))

    def solve(self, background: Image.Image, piece: Image.Image,
              **kwargs) -> DragSolution:
        t0 = time.time()
        bg_w, bg_h = background.size
        pw, ph = piece.size
        # 拼图块在背景左侧起始列显示：其 alpha bbox 顶 y 即缺口顶 y
        from app.register import captcha_vision

        _c, _m, bbox = captcha_vision.crop_to_alpha(piece)
        want_cy = bbox[1] + ph / 2
        try:
            d = self.cli.upload(background, 9900)
        except ChaojiyingError:
            # API 异常抛出去，让 PaidWithFallback 回退 2Captcha
            raise
        self.last_pic_id = str(d.get("pic_id") or "")
        rects = []
        for seg in str(d.get("pic_str") or "").split("|"):
            p = seg.strip().split(",")
            if len(p) != 4:
                continue
            try:
                rects.append(tuple(float(v) for v in p))
            except ValueError:
                continue
        if not rects:
            return DragSolution(False, raw=d, elapsed=time.time() - t0,
                                error=f"9900 无图形块: {d.get('pic_str')}")
        best, best_score = None, -1e9
        for x1, y1, x2, y2 in rects:
            cy, w = (y1 + y2) / 2, x2 - x1
            if not (0 < w <= bg_w and 0 < y1 < y2 <= bg_h + 2):
                continue
            # y 对齐（容差半块高）+ 宽度接近拼图宽
            s = -abs(cy - want_cy) / max(ph, 1) - abs(w - pw) / max(pw, 1)
            if s > best_score:
                best, best_score = (x1, y1, x2, y2), s
        if best is None or best_score < -2.5:
            # 筛选不中：退回 9901（单图形块中心），再不行取可信度第一框
            try:
                d1 = self.cli.upload(background, 9901)
                pts = parse_points(d1.get("pic_str", ""))
                if pts:
                    self.last_pic_id = str(d1.get("pic_id") or "")
                    return DragSolution(True, x=pts[0][0], y=pts[0][1],
                                        raw=d1, elapsed=time.time() - t0)
            except ChaojiyingError:
                pass
            best = rects[0]
        x1, y1, x2, y2 = best
        return DragSolution(True, x=(x1 + x2) / 2, y=(y1 + y2) / 2,
                            raw=d, elapsed=time.time() - t0)

    def report_last_error(self) -> bool:
        """拖拽判定失败且怀疑答案错时返分（上层按需调用）。"""
        ok = self.cli.report_error(self.last_pic_id)
        self.last_pic_id = ""
        return ok


# ------------------------------------------------------------ 九宫格适配器
class ChaojiyingClick:
    """与 TwoCaptchaClick 同构：solve_grid / solve → ClickSolution。

    超级鹰人工坐标类（9006）要求**操作提示直接画在图上**且醒目，
    所以把「红色大字提示 + 题面图 + 九宫格」纵向合成一张上传。
    返回坐标按合成布局换算回格子（1 起）；点在提示/题面区视为
    「没有符合的格子」（工人按提示点文字区表达空答案）。
    """

    name = "chaojiying"

    def __init__(self, client: ChaojiyingClient) -> None:
        self.cli = client
        self.last_pic_id = ""

    def balance(self) -> float:
        d = self.cli.score()
        return float(d.get("tifen", 0))

    def _ocr_question(self, q_enhanced: Image.Image) -> str:
        """9800 文字定位读题干：裁出题面带→AI 逐字返回→按 x 排序拼接。

        工人对碎笔画粉字误读率高（实测三轮全错），先把题干读成
        清晰文本再渲染进合成图。读不出返回 ""（回退原增强带）。
        """
        h = q_enhanced.height
        band = q_enhanced.crop((0, int(h * 0.18), q_enhanced.width,
                                int(h * 0.80)))
        try:
            d = self.cli.upload(band, 9800)
        except ChaojiyingError:
            return ""
        chars: list[tuple[float, str]] = []
        for seg in str(d.get("pic_str") or "").split("|"):
            parts = seg.strip().split(",")
            if len(parts) != 3:
                continue
            ch = parts[0]
            if ch.startswith("\\u"):
                try:
                    ch = ch.encode().decode("unicode_escape")
                except Exception:
                    pass
            try:
                chars.append((float(parts[1]), ch))
            except ValueError:
                continue
        chars.sort(key=lambda t: t[0])
        text = "".join(c for _, c in chars)
        return text if len(text) >= 2 else ""

    @staticmethod
    def _enhance_question(q: Image.Image) -> Image.Image:
        """题面低对比度增强：粉字粉底→黑字白底。

        亮度通道抓黑字；粉色差通道(R-G, blur+阈值+膨胀)抓粉字；
        高阈饱和度通道抓彩色目标图案（蓝/白底噪不会被放大）。
        numpy 不可用时原样返回。
        """
        try:
            import numpy as np
            from numpy.lib.stride_tricks import sliding_window_view
        except Exception:
            return q

        def boxblur(x, r):
            pad = np.pad(x, r, mode="edge")
            return sliding_window_view(
                pad, (2 * r + 1, 2 * r + 1)).mean(axis=(2, 3))

        def stretch(x):
            lo, hi = np.percentile(x, (2, 98))
            return np.clip((x - lo) / max(1.0, hi - lo) * 255, 0, 255)

        a = np.asarray(q.convert("RGB")).astype(np.int32)
        dark_lum = 255 - stretch(a.mean(axis=2))
        pink_b = boxblur((a[:, :, 0] - a[:, :, 1]).astype(float), 2)
        thr = float(np.percentile(pink_b, 85))
        dark_pink = np.clip((pink_b - thr) * 20, 0, 255)
        pad = np.pad(dark_pink, 1, mode="edge")
        dark_pink = sliding_window_view(pad, (3, 3)).max(axis=(2, 3))
        sat_b = boxblur((a.max(axis=2) - a.min(axis=2)).astype(float), 1)
        thr99 = float(np.percentile(sat_b, 99))
        dark_col = np.clip((sat_b - thr99) * 15, 0, 255)
        dark = np.maximum(np.maximum(dark_lum, dark_pink), dark_col)
        return Image.fromarray((255 - dark).astype(np.uint8))

    @staticmethod
    def _composite(grid_png: bytes, q_png: bytes | None,
                   ocr_text: str = "") -> tuple[bytes, int, int]:
        """合成图 + 格子区起始 y + 图宽。提示条红字白底，面积占比足够醒目。

        ocr_text 非空时题干用清晰黑字渲染（替代噪点增强带）。
        """
        grid = Image.open(io.BytesIO(grid_png)).convert("RGB")
        q = (Image.open(io.BytesIO(q_png)).convert("RGB")
             if q_png else None)
        if q is not None:
            q = ChaojiyingClick._enhance_question(q)
        if ocr_text:
            # 题干清晰重渲染：黑字白底，字号与提示条同级
            tsize = max(28, (q.width if q is not None else grid.width) // 16)
            tfont = _cjk_font(tsize)
            q = Image.new("RGB", (q.width if q is not None else grid.width,
                                  tsize + 24), "white")
            ImageDraw.Draw(q).text((10, 12), "目标：" + ocr_text,
                                   fill=(0, 0, 0), font=tfont)
        # 以题面原生宽度为准（题面是 3x 截图，缩小会把字压碎）；
        # 格子放大对齐，坐标换算用放大后的格宽
        w = max(grid.width, q.width if q is not None else 0)
        if grid.width != w:
            grid = grid.resize((w, w), Image.LANCZOS)
        if q is not None and q.width != w:
            q = q.resize((w, max(1, round(q.height * w / q.width))),
                         Image.LANCZOS)
        text = "请点击所有包含目标图案的格子(可多个)；若一个都没有，请点击本行红色文字"
        size = max(24, w // 16)
        font = _cjk_font(size)
        per_line = max(8, (w - 20) // size)     # 中文近似方形，按宽换行
        lines = [text[i:i + per_line]
                 for i in range(0, len(text), per_line)]
        line_h = size + 6
        banner = Image.new("RGB", (w, line_h * len(lines) + 12), "white")
        bd = ImageDraw.Draw(banner)
        for li, line in enumerate(lines):
            bd.text((10, 6 + li * line_h), line, fill=(220, 0, 0), font=font)
        qh = q.height if q is not None else 0
        canvas = Image.new("RGB", (w, banner.height + qh + grid.height),
                           "white")
        canvas.paste(banner, (0, 0))
        if q is not None:
            canvas.paste(q, (0, banner.height))
        canvas.paste(grid, (0, banner.height + qh))
        return _jpeg_under(canvas), banner.height + qh, w

    def _solve_points(self, grid_png: bytes, q_png: bytes | None
                      ) -> tuple[list[tuple[float, float]], int, int, dict, float]:
        t0 = time.time()
        ocr_text = ""
        if q_png:
            q_img = ChaojiyingClick._enhance_question(
                Image.open(io.BytesIO(q_png)).convert("RGB"))
            ocr_text = self._ocr_question(q_img)
        comp, grid_y0, grid_w = self._composite(grid_png, q_png, ocr_text)
        d = self.cli.upload(comp, 9006)
        self.last_pic_id = str(d.get("pic_id") or "")
        pts = parse_points(d.get("pic_str", ""))
        return pts, grid_y0, grid_w, d, time.time() - t0

    def solve_grid(self, image, comment: str = "",
                   img_instructions: bytes | None = None,
                   rows: int = 3, columns: int = 3) -> ClickSolution:
        grid_png = _img_bytes(image)
        try:
            pts, grid_y0, grid_w, raw, elapsed = self._solve_points(
                grid_png, img_instructions)
        except ChaojiyingError as exc:
            raise RuntimeError(str(exc)) from exc
        cw, ch = grid_w / columns, grid_w / rows
        cells: list[int] = []
        for x, y in pts:
            if y < grid_y0 - 2:      # 点在提示/题面区 = 空答案
                continue
            col, row = int(x // cw), int((y - grid_y0) // ch)
            if 0 <= col < columns and 0 <= row < rows:
                idx = row * columns + col + 1
                if idx not in cells:
                    cells.append(idx)
        return ClickSolution(ok=True, points=pts, cells=cells,
                             elapsed=elapsed, raw=raw)

    def solve(self, image, comment: str = "",
              img_instructions: bytes | None = None) -> ClickSolution:
        """CoordinatesTask 同构回退：返回相对九宫格图的坐标。"""
        grid_png = _img_bytes(image)
        try:
            pts, grid_y0, grid_w, raw, elapsed = self._solve_points(
                grid_png, img_instructions)
        except ChaojiyingError as exc:
            raise RuntimeError(str(exc)) from exc
        rel = [(x, y - grid_y0) for x, y in pts if y >= grid_y0 - 2]
        return ClickSolution(ok=bool(rel), points=rel,
                             elapsed=elapsed, raw=raw)

    def report_last_error(self) -> bool:
        ok = self.cli.report_error(self.last_pic_id)
        self.last_pic_id = ""
        return ok


# ------------------------------------------------------------ 工厂/回退
def _cfg() -> dict:
    from app import secrets

    return {
        "enabled": bool(secrets.get("captcha.chaojiying.enabled", False)),
        "user": str(secrets.get("captcha.chaojiying.user", "") or ""),
        "password": str(secrets.get("captcha.chaojiying.password", "") or ""),
        "softid": str(secrets.get("captcha.chaojiying.softid", "") or ""),
        "key": str(secrets.get("captcha.chaojiying.key", "") or ""),
    }


def build_client() -> ChaojiyingClient | None:
    c = _cfg()
    if not c["enabled"]:
        return None
    if not ((c["user"] and c["password"]) or c["key"]):
        return None
    return ChaojiyingClient(c["user"], c["password"], c["softid"], c["key"])


class PaidWithFallback:
    """超级鹰优先、2Captcha 兜底（仅 API 异常时回退，答案对错无法本地判定）。

    同时兼容滑块（solve/bg/pz + balance）与点选（solve_grid/solve）接口。
    """

    def __init__(self, primary, fallback) -> None:
        self.primary = primary
        self.fallback = fallback
        fb_name = getattr(fallback, "name", "2captcha") if fallback else "无"
        self.name = f"{getattr(primary, 'name', 'cjy')}+{fb_name}"

    def _call(self, method: str, *args, **kwargs):
        try:
            return getattr(self.primary, method)(*args, **kwargs)
        except Exception as exc:
            if self.fallback is None:
                raise
            import logging

            logging.getLogger("cjy").warning(
                "超级鹰 %s 异常，回退 2Captcha: %s", method, str(exc)[:100])
            return getattr(self.fallback, method)(*args, **kwargs)

    def balance(self) -> float:
        try:
            return self.primary.balance()
        except Exception:
            return self.fallback.balance() if self.fallback else 0.0

    def solve(self, *args, **kwargs):
        return self._call("solve", *args, **kwargs)

    def solve_grid(self, *args, **kwargs):
        if hasattr(self.primary, "solve_grid"):
            return self._call("solve_grid", *args, **kwargs)
        if self.fallback is None:
            raise ChaojiyingError("无 solve_grid 可用客户端")
        return self.fallback.solve_grid(*args, **kwargs)

    def report_last_error(self) -> bool:
        fn = getattr(self.primary, "report_last_error", None)
        return bool(fn and fn())


def build_slider_paid(twocaptcha_client=None):
    """滑块打码客户端：超级鹰启用则优先（异常回退 2Captcha），否则原样。"""
    cli = build_client()
    if cli is None:
        return twocaptcha_client
    return PaidWithFallback(ChaojiyingSlider(cli), twocaptcha_client)


def build_click_client(twocaptcha_client=None):
    """九宫格打码客户端：同上。"""
    cli = build_client()
    if cli is None:
        return twocaptcha_client
    return PaidWithFallback(ChaojiyingClick(cli), twocaptcha_client)