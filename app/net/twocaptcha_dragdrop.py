"""2Captcha Drag & Drop 滑块识别（替代自研识别）。

官方文档：https://2captcha.com/api-docs/drag-and-drop

任务类型：``DragAndDropTask``
  输入：background（背景图 Base64）+ images（要拖的图数组，Base64）
  输出：``solution.coordinates = [{"x":120,"y":340}, null, ...]``
        null 表示该图没移动，位置与 images 数组一一对应。

为什么用这个而不是 AlibabaTask
------------------------------
``AlibabaTask`` 是「重放业务请求」模式：它自己开浏览器过验证码，
返回 certifyId/deviceToken/data，需要把 token 注入目标页面的阿里云 SDK 实例。
Qoder 是前端内嵌组件，注入路径不明确。

``DragAndDropTask`` 只做「看图给坐标」，返回的 x/y 可以直接喂给现有拖拽代码，
跟本地识别完全同构 —— 是零成本替换。

与本地识别的配合
----------------
1. 优先本地 OpenCV（免费、快）
2. 本地置信度不足 / 换题多次失败 → 调 2Captcha
3. 拿到坐标后统一走同一个拖拽函数
"""

from __future__ import annotations

import base64
import io
import time
from dataclasses import dataclass, field

import requests
from PIL import Image

CREATE_TASK = "https://api.2captcha.com/createTask"
GET_RESULT = "https://api.2captcha.com/getTaskResult"
GET_BALANCE = "https://api.2captcha.com/getBalance"


class TwoCaptchaError(RuntimeError):
    def __init__(self, msg: str, code: str = "", raw: dict | None = None):
        super().__init__(msg)
        self.code = code
        self.raw = raw or {}


@dataclass(slots=True)
class DragSolution:
    ok: bool
    x: float = 0.0
    y: float = 0.0
    raw: dict = field(default_factory=dict)
    elapsed: float = 0.0
    cost: float = 0.0
    error: str = ""


def _b64_png(img: Image.Image) -> str:
    """PIL → Base64 PNG（不带 data: 前缀，2Captcha 要裸 base64）。"""
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return base64.b64encode(buf.getvalue()).decode()


class TwoCaptchaDragDrop:
    """2Captcha 拖拽识别封装。"""

    def __init__(self, api_key: str, proxy: str | None = "http://127.0.0.1:7897",
                 timeout: int = 180, poll: float = 3.0) -> None:
        self.api_key = api_key
        self.proxy = proxy
        self.timeout = timeout
        self.poll = poll

    def _post(self, url: str, payload: dict, retries: int = 3) -> dict:
        """POST 到 2Captcha。代理失败自动回落直连，最多 3 次退避重试。

        本机实测：本地 Clash 的节点偶发 `SSL: UNEXPECTED_EOF_WHILE_READING`，
        一次失败就抛会让整轮注册白跑，所以这里分「先代理、后直连」两条腿。
        """
        last = ""
        for attempt in range(retries + 1):
            for label, px in (("代理", self.proxy), ("直连", None)):
                if label == "直连" and not self.proxy:
                    continue
                proxies = {"http": px, "https": px} if px else None
                try:
                    r = requests.post(url, json=payload, proxies=proxies,
                                      timeout=40)
                    return r.json()
                except Exception as exc:
                    last = f"{label} {type(exc).__name__}: {exc}"
                    continue
            if attempt < retries:
                time.sleep(2.0 * (attempt + 1))
        raise TwoCaptchaError(f"请求失败（{retries + 1} 轮）: {last}")

    def balance(self) -> float:
        d = self._post(GET_BALANCE, {"clientKey": self.api_key})
        if d.get("errorId", 1) != 0:
            raise TwoCaptchaError(
                d.get("errorDescription") or "取余额失败",
                str(d.get("errorCode", "")), d)
        return float(d.get("balance", 0.0))

    # ---------------------------------------------------------- 建任务
    def create_task(self, background: Image.Image, pieces: list[Image.Image],
                    comment: str = "Drag the puzzle piece to the gap "
                                   "on the background image") -> str:
        payload = {
            "clientKey": self.api_key,
            "task": {
                "type": "DragAndDropTask",
                "background": _b64_png(background),
                "images": [_b64_png(p) for p in pieces],
                "comment": comment,
            },
        }
        d = self._post(CREATE_TASK, payload)
        if d.get("errorId", 1) != 0:
            raise TwoCaptchaError(
                d.get("errorDescription") or "建任务失败",
                str(d.get("errorCode", "")), d)
        tid = d.get("taskId")
        if not tid:
            raise TwoCaptchaError("未返回 taskId", raw=d)
        return str(tid)

    # ---------------------------------------------------------- 等结果
    def wait_result(self, task_id: str) -> DragSolution:
        t0 = time.time()
        deadline = t0 + self.timeout
        while time.time() < deadline:
            time.sleep(self.poll)
            d = self._post(GET_RESULT, {"clientKey": self.api_key,
                                        "taskId": task_id})
            if d.get("errorId", 1) != 0:
                raise TwoCaptchaError(
                    d.get("errorDescription") or "查询失败",
                    str(d.get("errorCode", "")), d)

            status = d.get("status")
            if status == "processing":
                continue
            if status != "ready":
                raise TwoCaptchaError(f"未知状态 {status}", raw=d)

            sol = d.get("solution") or {}
            coords = sol.get("coordinates") or []
            if not coords or coords[0] is None:
                return DragSolution(False, raw=d, elapsed=time.time() - t0,
                                    error="返回坐标为空")

            c0 = coords[0]
            return DragSolution(
                ok=True,
                x=float(c0.get("x", 0)),
                y=float(c0.get("y", 0)),
                raw=d,
                elapsed=time.time() - t0,
                cost=float(d.get("cost") or 0),
            )

        raise TwoCaptchaError(f"超时 {self.timeout}s")

    # ---------------------------------------------------------- 一步到位
    def solve(self, background: Image.Image, piece: Image.Image,
              **kwargs) -> DragSolution:
        tid = self.create_task(background, [piece], **kwargs)
        return self.wait_result(tid)
