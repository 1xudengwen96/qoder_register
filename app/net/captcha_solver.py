"""打码平台方案：多供应商、按任务类型路由、成本统计、失败降级。

支持的供应商
------------
| provider     | 支持任务                                        | 单价量级 |
|--------------|-------------------------------------------------|----------|
| ``yescaptcha`` | reCAPTCHA v2/v3、hCaptcha、Cloudflare Turnstile、图片点选 | ¥0.002~0.02 |
| ``2captcha``   | reCAPTCHA v2/v3、hCaptcha、Turnstile、图片       | ¥0.015~0.03 |
| ``capsolver``  | reCAPTCHA v2/v3、hCaptcha、Turnstile、DataDome   | ¥0.01~0.04 |
| ``local``      | 本地模型（ddddocr 等），只做图片验证码            | ¥0 |

路由策略
--------
- 站点用 reCAPTCHA v2 → 优先 ``capsolver`` / ``yescaptcha``
- Cloudflare Turnstile → 只有 ``capsolver`` / ``yescaptcha`` 支持
- 纯图片验证码 → 优先 ``local``（零成本），失败回落云平台

成本账
------
每解决一次累加 ``spent``，流水线 ``stats`` 输出单号打码摊薄成本。
"""

from __future__ import annotations

import base64
import dataclasses
import json
import os
import time
from pathlib import Path
from typing import Any

import requests


class CaptchaError(RuntimeError):
    pass


class CaptchaTimeout(CaptchaError):
    pass


# ------------------------------------------------------------ 数据类
@dataclasses.dataclass(slots=True)
class CaptchaTask:
    kind: str                        # recaptcha_v2 | recaptcha_v3 | hcaptcha | turnstile | image
    page_url: str = ""
    site_key: str = ""
    action: str = "submit"           # recaptcha_v3
    min_score: float = 0.7           # recaptcha_v3
    image_b64: str = ""              # image
    proxy: str | None = None         # http://user:pass@host:port
    user_agent: str | None = None


@dataclasses.dataclass(slots=True)
class CaptchaResult:
    token: str
    provider: str
    cost: float
    elapsed: float
    ok: bool = True
    error: str = ""


# ------------------------------------------------------------ 供应商基类
class Provider:
    name = "base"
    price: dict[str, float] = {}

    def __init__(self, api_key: str, timeout: int = 180) -> None:
        self.api_key = api_key
        self.timeout = timeout

    def solve(self, task: CaptchaTask) -> CaptchaResult:
        raise NotImplementedError

    def supports(self, kind: str) -> bool:
        raise NotImplementedError

    def _cost(self, kind: str) -> float:
        return self.price.get(kind, 0.02)


# ------------------------------------------------------------ YesCaptcha
class YesCaptcha(Provider):
    name = "yescaptcha"
    CREATE = "https://api.yescaptcha.com/createTask"
    RESULT = "https://api.yescaptcha.com/getTaskResult"
    price = {
        "recaptcha_v2": 0.003, "recaptcha_v3": 0.003,
        "hcaptcha": 0.003, "turnstile": 0.006, "image": 0.0015,
    }

    def supports(self, kind: str) -> bool:
        return kind in self.price

    def _task_body(self, task: CaptchaTask) -> dict:
        if task.kind == "recaptcha_v2":
            body = {"type": "NoCaptchaTaskProxyless",
                    "websiteURL": task.page_url, "websiteKey": task.site_key}
        elif task.kind == "recaptcha_v3":
            body = {"type": "RecaptchaV3TaskProxyless",
                    "websiteURL": task.page_url, "websiteKey": task.site_key,
                    "pageAction": task.action, "minScore": task.min_score}
        elif task.kind == "hcaptcha":
            body = {"type": "HCaptchaTaskProxyless",
                    "websiteURL": task.page_url, "websiteKey": task.site_key}
        elif task.kind == "turnstile":
            body = {"type": "TurnstileTaskProxyless",
                    "websiteURL": task.page_url, "websiteKey": task.site_key}
        elif task.kind == "image":
            body = {"type": "ImageToTextTask", "body": task.image_b64}
        else:
            raise CaptchaError(f"YesCaptcha 不支持 {task.kind}")

        if task.proxy and task.kind != "image":
            from urllib.parse import urlparse
            u = urlparse(task.proxy if "://" in task.proxy else "http://" + task.proxy)
            body.update({
                "proxyType": u.scheme or "http",
                "proxyAddress": u.hostname,
                "proxyPort": u.port,
                "proxyLogin": u.username or "",
                "proxyPassword": u.password or "",
            })
        return body

    def solve(self, task: CaptchaTask) -> CaptchaResult:
        t0 = time.time()
        r = requests.post(
            self.CREATE,
            json={"clientKey": self.api_key, "task": self._task_body(task)},
            timeout=30,
        )
        data = r.json()
        if data.get("errorId", 1) != 0:
            raise CaptchaError(f"YesCaptcha 建任务失败：{data.get('errorDescription')}")
        tid = data["taskId"]

        deadline = time.time() + self.timeout
        while time.time() < deadline:
            time.sleep(3)
            rr = requests.post(self.RESULT, json={"clientKey": self.api_key, "taskId": tid},
                               timeout=30)
            res = rr.json()
            if res.get("status") == "ready":
                sol = res["solution"]
                token = (sol.get("gRecaptchaResponse") or sol.get("token")
                         or sol.get("text") or json.dumps(sol))
                return CaptchaResult(token, self.name, self._cost(task.kind),
                                     time.time() - t0)
            if res.get("errorId"):
                raise CaptchaError(f"YesCaptcha 求解失败：{res.get('errorDescription')}")
        raise CaptchaTimeout("YesCaptcha 超时")


# ------------------------------------------------------------ 2Captcha
class TwoCaptcha(Provider):
    name = "2captcha"
    IN = "https://2captcha.com/in.php"
    RES = "https://2captcha.com/res.php"
    price = {
        "recaptcha_v2": 0.02, "recaptcha_v3": 0.02,
        "hcaptcha": 0.02, "turnstile": 0.03, "image": 0.01,
    }

    def supports(self, kind: str) -> bool:
        return kind in self.price

    def _params(self, task: CaptchaTask) -> dict:
        base = {"key": self.api_key, "json": 1}
        if task.kind == "recaptcha_v2":
            base.update({"method": "userrecaptcha", "googlekey": task.site_key,
                         "pageurl": task.page_url})
        elif task.kind == "recaptcha_v3":
            base.update({"method": "userrecaptcha", "version": "v3",
                         "googlekey": task.site_key, "pageurl": task.page_url,
                         "action": task.action, "min_score": task.min_score})
        elif task.kind == "hcaptcha":
            base.update({"method": "hcaptcha", "sitekey": task.site_key,
                         "pageurl": task.page_url})
        elif task.kind == "turnstile":
            base.update({"method": "turnstile", "sitekey": task.site_key,
                         "pageurl": task.page_url})
        elif task.kind == "image":
            base.update({"method": "base64", "body": task.image_b64})
        else:
            raise CaptchaError(f"2Captcha 不支持 {task.kind}")
        if task.proxy:
            from urllib.parse import urlparse
            u = urlparse(task.proxy if "://" in task.proxy else "http://" + task.proxy)
            base.update({"proxy": f"{u.hostname}:{u.port}",
                         "proxytype": u.scheme.upper() or "HTTP",
                         "proxy_user": u.username or "",
                         "proxy_pwd": u.password or ""})
        return base

    def solve(self, task: CaptchaTask) -> CaptchaResult:
        t0 = time.time()
        r = requests.post(self.IN, data=self._params(task), timeout=30)
        data = r.json()
        if data.get("status") != 1:
            raise CaptchaError(f"2Captcha 提交失败：{data.get('request')}")
        req_id = data["request"]
        deadline = time.time() + self.timeout
        while time.time() < deadline:
            time.sleep(5)
            rr = requests.get(self.RES, params={
                "key": self.api_key, "action": "get", "id": req_id, "json": 1,
            }, timeout=30)
            res = rr.json()
            if res.get("status") == 1:
                return CaptchaResult(str(res["request"]), self.name,
                                     self._cost(task.kind), time.time() - t0)
            if res.get("request") not in ("CAPCHA_NOT_READY",):
                raise CaptchaError(f"2Captcha 求解失败：{res.get('request')}")
        raise CaptchaTimeout("2Captcha 超时")


# ------------------------------------------------------------ CapSolver
class CapSolver(Provider):
    name = "capsolver"
    CREATE = "https://api.capsolver.com/createTask"
    RESULT = "https://api.capsolver.com/getTaskResult"
    price = {
        "recaptcha_v2": 0.01, "recaptcha_v3": 0.012,
        "hcaptcha": 0.01, "turnstile": 0.02, "image": 0.005,
    }

    def supports(self, kind: str) -> bool:
        return kind in self.price

    def _task(self, task: CaptchaTask) -> dict:
        mapping = {
            "recaptcha_v2": "ReCaptchaV2TaskProxyLess",
            "recaptcha_v3": "ReCaptchaV3TaskProxyLess",
            "hcaptcha": "HCaptchaTaskProxyLess",
            "turnstile": "AntiTurnstileTaskProxyLess",
            "image": "ImageToTextTask",
        }
        body: dict[str, Any] = {"type": mapping[task.kind]}
        if task.kind == "image":
            body["body"] = task.image_b64
            return body
        body["websiteURL"] = task.page_url
        body["websiteKey"] = task.site_key
        if task.kind == "recaptcha_v3":
            body["pageAction"] = task.action
            body["minScore"] = task.min_score
        if task.proxy:
            from urllib.parse import urlparse
            u = urlparse(task.proxy if "://" in task.proxy else "http://" + task.proxy)
            body["type"] = mapping[task.kind].replace("ProxyLess", "")
            body.update({"proxyType": u.scheme or "http", "proxyAddress": u.hostname,
                         "proxyPort": u.port, "proxyLogin": u.username or "",
                         "proxyPassword": u.password or ""})
        return body

    def solve(self, task: CaptchaTask) -> CaptchaResult:
        t0 = time.time()
        r = requests.post(self.CREATE, json={
            "clientKey": self.api_key, "task": self._task(task),
        }, timeout=30)
        data = r.json()
        if data.get("errorId", 1) != 0:
            raise CaptchaError(f"CapSolver 建任务失败：{data.get('errorDescription')}")
        tid = data["taskId"]
        deadline = time.time() + self.timeout
        while time.time() < deadline:
            time.sleep(3)
            rr = requests.post(self.RESULT, json={
                "clientKey": self.api_key, "taskId": tid,
            }, timeout=30)
            res = rr.json()
            if res.get("status") == "ready":
                sol = res.get("solution", {})
                token = (sol.get("gRecaptchaResponse") or sol.get("token")
                         or sol.get("text") or json.dumps(sol))
                return CaptchaResult(token, self.name, self._cost(task.kind),
                                     time.time() - t0)
            if res.get("errorId"):
                raise CaptchaError(f"CapSolver 求解失败：{res.get('errorDescription')}")
        raise CaptchaTimeout("CapSolver 超时")


# ------------------------------------------------------------ 本地 OCR
class LocalOCR(Provider):
    """图片验证码走本地模型，零成本。依赖 ddddocr。"""

    name = "local"
    price = {"image": 0.0}

    def __init__(self, api_key: str = "", timeout: int = 30) -> None:
        super().__init__(api_key, timeout)
        self._ocr = None

    def supports(self, kind: str) -> bool:
        return kind == "image"

    def solve(self, task: CaptchaTask) -> CaptchaResult:
        t0 = time.time()
        if self._ocr is None:
            try:
                import ddddocr
                self._ocr = ddddocr.DdddOcr(show_ad=False)
            except Exception as exc:
                raise CaptchaError(f"本地 OCR 不可用：{exc}")
        raw = base64.b64decode(task.image_b64)
        text = self._ocr.classification(raw)
        return CaptchaResult(text, self.name, 0.0, time.time() - t0)


# ------------------------------------------------------------ 调度器
class CaptchaSolver:
    """按任务类型在多个供应商之间路由，失败自动降级到下一个。"""

    def __init__(self, providers: list[Provider], prefer: dict[str, list[str]] | None = None) -> None:
        self.providers = {p.name: p for p in providers}
        self.prefer = prefer or {
            "image": ["local", "yescaptcha", "2captcha"],
            "recaptcha_v2": ["capsolver", "yescaptcha", "2captcha"],
            "recaptcha_v3": ["capsolver", "yescaptcha", "2captcha"],
            "hcaptcha": ["capsolver", "yescaptcha", "2captcha"],
            "turnstile": ["capsolver", "yescaptcha", "2captcha"],
        }
        self.spent = 0.0
        self.solved = 0
        self.failed = 0
        self.history: list[CaptchaResult] = []

    def solve(self, task: CaptchaTask) -> CaptchaResult:
        order = self.prefer.get(task.kind, list(self.providers))
        last_err = ""
        for name in order:
            p = self.providers.get(name)
            if p is None or not p.supports(task.kind):
                continue
            try:
                res = p.solve(task)
                self.spent += res.cost
                self.solved += 1
                self.history.append(res)
                return res
            except CaptchaError as exc:
                last_err = f"{name}: {exc}"
                continue
        self.failed += 1
        raise CaptchaError(f"全部供应商失败（{task.kind}）：{last_err}")

    def cost_summary(self) -> str:
        return (f"打码成功 {self.solved} 次，失败 {self.failed} 次，"
                f"累计 ¥{self.spent:.4f}，均次 ¥{self.spent / max(self.solved,1):.4f}")

    def save_stats(self, path: str | Path) -> None:
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps({
            "solved": self.solved, "failed": self.failed, "spent": round(self.spent, 4),
            "history": [dataclasses.asdict(h) for h in self.history[-200:]],
        }, ensure_ascii=False, indent=2), encoding="utf-8")


# ------------------------------------------------------------ 工厂
def build_solver(cfg) -> CaptchaSolver:
    from app.config import get

    providers: list[Provider] = []
    timeout = int(get(cfg, "captcha.timeout", 180))

    if get(cfg, "captcha.providers.local.enabled", True):
        providers.append(LocalOCR(timeout=30))

    for name, cls in (("yescaptcha", YesCaptcha), ("2captcha", TwoCaptcha),
                      ("capsolver", CapSolver)):
        key = get(cfg, f"captcha.providers.{name}.api_key", "") or ""
        if not key:
            key = os.environ.get(f"{name.upper()}_KEY", "")
        if key:
            providers.append(cls(key, timeout=timeout))

    prefer = get(cfg, "captcha.prefer", None)
    if prefer is not None and not isinstance(prefer, dict):
        prefer = None
    return CaptchaSolver(providers, prefer=prefer)
