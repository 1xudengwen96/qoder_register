"""打码平台：只用 2Captcha（2026-11-17 决策：自研识别与其他供应商全部移除）。

支持任务
--------
| kind          | 2Captcha 任务             | 单价量级     |
|---------------|---------------------------|--------------|
| recaptcha_v2  | userrecaptcha             | $0.02        |
| recaptcha_v3  | userrecaptcha v3          | $0.02        |
| hcaptcha      | hcaptcha                  | $0.02        |
| turnstile     | turnstile                 | $0.03        |
| image         | base64 图片               | $0.01        |

成本账
------
每解决一次累加 ``spent``，流水线 ``stats`` 输出单号打码摊薄成本。
"""

from __future__ import annotations

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


# ------------------------------------------------------------ 2Captcha
class TwoCaptcha:
    name = "2captcha"
    IN = "https://2captcha.com/in.php"
    RES = "https://2captcha.com/res.php"
    price = {
        "recaptcha_v2": 0.02, "recaptcha_v3": 0.02,
        "hcaptcha": 0.02, "turnstile": 0.03, "image": 0.01,
    }

    def __init__(self, api_key: str, timeout: int = 180) -> None:
        self.api_key = api_key
        self.timeout = timeout

    def supports(self, kind: str) -> bool:
        return kind in self.price

    def _cost(self, kind: str) -> float:
        return self.price.get(kind, 0.02)

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


# ------------------------------------------------------------ 兼容别名
Provider = TwoCaptcha

# ------------------------------------------------------------ 调度器
class CaptchaSolver:
    """单供应商（2Captcha）封装，保持既有 solve/cost/save 接口。"""

    def __init__(self, providers: list[Any] | None = None,
                 prefer: dict[str, list[str]] | None = None) -> None:
        if providers:
            self.provider = providers[0]
        else:
            self.provider = None
        self.providers = ({p.name: p for p in providers} if providers else {})
        self.spent = 0.0
        self.solved = 0
        self.failed = 0
        self.history: list[CaptchaResult] = []

    def solve(self, task: CaptchaTask) -> CaptchaResult:
        p = self.provider
        if p is None or not p.supports(task.kind):
            self.failed += 1
            raise CaptchaError(
                f"2Captcha 未配置或不支持 {task.kind}"
                "（自研识别已移除，配置 captcha.providers.2captcha.api_key）")
        try:
            res = p.solve(task)
            self.spent += res.cost
            self.solved += 1
            self.history.append(res)
            return res
        except CaptchaError:
            self.failed += 1
            raise

    def cost_summary(self) -> str:
        return (f"打码成功 {self.solved} 次，失败 {self.failed} 次，"
                f"累计 ${self.spent:.4f}，均次 ${self.spent / max(self.solved,1):.4f}")

    def save_stats(self, path: str | Path) -> None:
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps({
            "solved": self.solved, "failed": self.failed, "spent": round(self.spent, 4),
            "history": [dataclasses.asdict(h) for h in self.history[-200:]],
        }, ensure_ascii=False, indent=2), encoding="utf-8")


# ------------------------------------------------------------ 工厂
def build_solver(cfg) -> CaptchaSolver:
    """只构造 2Captcha。没配 key 时返回空 solver（solve 会明确报错）。"""
    from app.config import get

    timeout = int(get(cfg, "captcha.timeout", 180))
    key = get(cfg, "captcha.providers.2captcha.api_key", "") or ""
    if not key:
        key = os.environ.get("TWOCAPTCHA_KEY", "") or os.environ.get("2CAPTCHA_KEY", "")
    providers: list[Any] = [TwoCaptcha(key, timeout=timeout)] if key else []
    return CaptchaSolver(providers)
