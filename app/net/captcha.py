"""打码平台封装：YesCaptcha / 2Captcha。

只做两件事：提交任务、轮询结果。计费累加在实例上，供 stats 展示单号成本。
API 文档索引：
  - YesCaptcha: https://yescaptcha.atlassian.net/wiki/spaces/API/
  - 2Captcha:   https://2captcha.com/2captcha-api
"""

from __future__ import annotations

import time
from typing import Any

import requests


class CaptchaError(RuntimeError):
    pass


class CaptchaClient:
    YESCAPTCHA_CREATE = "https://api.yescaptcha.com/createTask"
    YESCAPTCHA_RESULT = "https://api.yescaptcha.com/getTaskResult"
    TWOCAPTCHA_IN = "https://2captcha.com/in.php"
    TWOCAPTCHA_RES = "https://2captcha.com/res.php"

    def __init__(self, provider: str, api_key: str, timeout: int = 180) -> None:
        self.provider = provider.lower()
        self.api_key = api_key
        self.timeout = timeout
        self.spent = 0.0          # 累计花费（元）
        self.solved = 0
        self.failed = 0

    # ---- YesCaptcha -------------------------------------------------------
    def solve_recaptcha_v2_yescaptcha(self, site_key: str, page_url: str,
                                     proxy: str | None = None) -> str:
        payload: dict[str, Any] = {
            "clientKey": self.api_key,
            "task": {
                "type": "NoCaptchaTaskProxyless",
                "websiteURL": page_url,
                "websiteKey": site_key,
            },
        }
        if proxy:
            payload["task"] = {
                "type": "NoCaptchaTask",
                "websiteURL": page_url,
                "websiteKey": site_key,
                "proxyType": "http",
                "proxyAddress": proxy.split("://")[-1].split(":")[0],
                "proxyPort": int(proxy.rsplit(":", 1)[-1]),
            }
        task_id = self._yc_create(payload)
        return self._yc_wait(task_id)

    def solve_image_yescaptcha(self, b64_image: str) -> str:
        payload = {
            "clientKey": self.api_key,
            "task": {"type": "ImageToTextTask", "body": b64_image},
        }
        task_id = self._yc_create(payload)
        return self._yc_wait(task_id, text_mode=True)

    def _yc_create(self, payload: dict) -> str:
        r = requests.post(self.YESCAPTCHA_CREATE, json=payload, timeout=30)
        data = r.json()
        if data.get("errorId", 1) != 0:
            self.failed += 1
            raise CaptchaError(f"YesCaptcha 建任务失败：{data}")
        return str(data["taskId"])

    def _yc_wait(self, task_id: str, text_mode: bool = False) -> str:
        deadline = time.time() + self.timeout
        while time.time() < deadline:
            r = requests.post(
                self.YESCAPTCHA_RESULT,
                json={"clientKey": self.api_key, "taskId": task_id},
                timeout=30,
            )
            data = r.json()
            if data.get("status") == "ready":
                self.solved += 1
                self.spent += 0.0015 if text_mode else 0.003
                sol = data["solution"]
                return sol.get("gRecaptchaResponse") or sol.get("text") or ""
            time.sleep(4)
        self.failed += 1
        raise CaptchaError("YesCaptcha 超时")

    # ---- 2Captcha ---------------------------------------------------------
    def solve_recaptcha_v2_2captcha(self, site_key: str, page_url: str) -> str:
        payload = {
            "key": self.api_key,
            "method": "userrecaptcha",
            "googlekey": site_key,
            "pageurl": page_url,
            "json": 1,
        }
        r = requests.post(self.TWOCAPTCHA_IN, data=payload, timeout=30)
        data = r.json()
        if data.get("status") != 1:
            self.failed += 1
            raise CaptchaError(f"2Captcha 提交失败：{data}")
        req_id = data["request"]

        deadline = time.time() + self.timeout
        while time.time() < deadline:
            time.sleep(5)
            rr = requests.get(
                self.TWOCAPTCHA_RES,
                params={"key": self.api_key, "action": "get", "id": req_id, "json": 1},
                timeout=30,
            )
            res = rr.json()
            if res.get("status") == 1:
                self.solved += 1
                self.spent += 0.02
                return res["request"]
        self.failed += 1
        raise CaptchaError("2Captcha 超时")

    # ---- 统一入口 ---------------------------------------------------------
    def solve(self, kind: str, **kwargs) -> str:
        if self.provider == "yescaptcha":
            if kind == "recaptcha_v2":
                return self.solve_recaptcha_v2_yescaptcha(
                    kwargs["site_key"], kwargs["page_url"], kwargs.get("proxy")
                )
            if kind == "image":
                return self.solve_image_yescaptcha(kwargs["b64_image"])
        elif self.provider == "2captcha":
            if kind == "recaptcha_v2":
                return self.solve_recaptcha_v2_2captcha(kwargs["site_key"], kwargs["page_url"])
        raise CaptchaError(f"不支持的组合：{self.provider}/{kind}")

    def cost_summary(self) -> str:
        return f"打码 {self.solved} 次，花费 ¥{self.spent:.4f}，失败 {self.failed}"
