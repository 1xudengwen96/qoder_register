"""2Captcha 点选类验证码（ClickCaptcha / CoordinatesTask）。

与 ``DragAndDropTask`` 的区别：
- DragAndDrop 返回「拼图块目标坐标」一个点
- CoordinatesTask 返回**多个点击点**，适合「九宫格里选出符合描述的所有图」

2Captcha 接口：``POST /createTask`` type=``CoordinatesTask``
  body        : base64 PNG（不带 data: 前缀）
  comment     : 给打码员的说明（中文/英文都行）
  imgInstructions : 可选，额外提示图
返回：``solution.coordinates = [{"x":..,"y":..}, ...]``
"""

from __future__ import annotations

import base64
import time
from dataclasses import dataclass, field

import requests

from app.net.http import no_env_session

CREATE = "https://api.2captcha.com/createTask"
RESULT = "https://api.2captcha.com/getTaskResult"
BALANCE = "https://api.2captcha.com/getBalance"


class ClickTaskError(RuntimeError):
    def __init__(self, msg: str, code: str = ""):
        super().__init__(msg)
        self.code = code


@dataclass(slots=True)
class ClickSolution:
    ok: bool
    points: list[tuple[float, float]] = field(default_factory=list)
    cost: float = 0.0
    elapsed: float = 0.0
    error: str = ""
    raw: dict = field(default_factory=dict)


def _as_png_bytes(image) -> bytes:
    """PIL.Image 或 bytes 都接受。"""
    if isinstance(image, (bytes, bytearray)):
        return bytes(image)
    import io

    buf = io.BytesIO()
    image.save(buf, format="PNG")
    return buf.getvalue()


def data_url_to_bytes(data_url: str) -> bytes:
    if "," in data_url:
        data_url = data_url.split(",", 1)[1]
    return base64.b64decode(data_url + "=" * ((-len(data_url)) % 4))


class TwoCaptchaClick:
    def __init__(self, api_key: str, proxy: str | None = None,
                 timeout: int = 180, poll: float = 3.0) -> None:
        self.api_key = api_key
        self.proxy = proxy
        self.timeout = timeout
        self.poll = poll

    def _post(self, url: str, payload: dict, retries: int = 2) -> dict:
        last = ""
        session = no_env_session()
        try:
            for attempt in range(retries + 1):
                for label, px in (("代理", self.proxy), ("直连", None)):
                    if label == "直连" and not self.proxy:
                        continue
                    proxies = ({"http": px, "https": px} if px
                               else {"http": "", "https": ""})
                    try:
                        r = session.post(url, json=payload, proxies=proxies,
                                         timeout=40)
                        return r.json()
                    except Exception as exc:
                        last = f"{label} {type(exc).__name__}: {exc}"
                if attempt < retries:
                    time.sleep(2.0 * (attempt + 1))
        finally:
            session.close()
        raise ClickTaskError(f"请求失败: {last}")

    def balance(self) -> float:
        d = self._post(BALANCE, {"clientKey": self.api_key})
        if d.get("errorId", 1) != 0:
            raise ClickTaskError(d.get("errorDescription") or "取余额失败",
                                 str(d.get("errorCode", "")))
        return float(d.get("balance", 0.0))

    def solve_text(self, image, comment: str = "") -> str:
        """ImageToTextTask：把图里的文字读出来（诊断题干用）。"""
        payload = {
            "clientKey": self.api_key,
            "task": {
                "type": "ImageToTextTask",
                "body": base64.b64encode(_as_png_bytes(image)).decode(),
            },
        }
        if comment:
            payload["task"]["comment"] = comment
        d = self._post(CREATE, payload)
        if d.get("errorId", 1) != 0:
            raise ClickTaskError(d.get("errorDescription") or "建任务失败",
                                 str(d.get("errorCode", "")))
        task_id = d.get("taskId")
        t0 = time.time()
        while time.time() - t0 < self.timeout:
            time.sleep(self.poll)
            r = self._post(RESULT, {"clientKey": self.api_key,
                                    "taskId": task_id})
            if r.get("errorId", 1) != 0:
                raise ClickTaskError(r.get("errorDescription") or "查询失败",
                                     str(r.get("errorCode", "")))
            if r.get("status") == "processing":
                continue
            if r.get("status") != "ready":
                raise ClickTaskError(f"未知状态 {r.get('status')}")
            return str((r.get("solution") or {}).get("text") or "")
        raise ClickTaskError(f"超时 {self.timeout}s")

    def solve(self, image, comment: str,
              img_instructions: bytes | None = None) -> ClickSolution:
        payload = {
            "clientKey": self.api_key,
            "task": {
                "type": "CoordinatesTask",
                "body": base64.b64encode(_as_png_bytes(image)).decode(),
                "comment": comment,
            },
        }
        if img_instructions:
            payload["task"]["imgInstructions"] = base64.b64encode(
                img_instructions).decode()

        d = self._post(CREATE, payload)
        if d.get("errorId", 1) != 0:
            raise ClickTaskError(d.get("errorDescription") or "建任务失败",
                                 str(d.get("errorCode", "")))
        task_id = d.get("taskId")
        if not task_id:
            raise ClickTaskError("未返回 taskId")

        t0 = time.time()
        while time.time() - t0 < self.timeout:
            time.sleep(self.poll)
            r = self._post(RESULT, {"clientKey": self.api_key,
                                    "taskId": task_id})
            if r.get("errorId", 1) != 0:
                raise ClickTaskError(r.get("errorDescription") or "查询失败",
                                     str(r.get("errorCode", "")))
            status = r.get("status")
            if status == "processing":
                continue
            if status != "ready":
                raise ClickTaskError(f"未知状态 {status}")
            sol = r.get("solution") or {}
            coords = sol.get("coordinates") or []
            pts = [(float(c.get("x", 0)), float(c.get("y", 0)))
                   for c in coords if isinstance(c, dict)]
            return ClickSolution(ok=bool(pts), points=pts,
                                 cost=float(r.get("cost") or 0),
                                 elapsed=time.time() - t0, raw=r)
        raise ClickTaskError(f"超时 {self.timeout}s")
