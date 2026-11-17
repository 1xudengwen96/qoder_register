"""打码客户端封装：只用 2Captcha（2026-11-17 决策）。

只做两件事：提交任务、轮询结果。计费累加在实例上，供 stats 展示单号成本。
API 文档：https://2captcha.com/2captcha-api
"""

from __future__ import annotations

import time
from typing import Any

import requests


class CaptchaError(RuntimeError):
    pass


class CaptchaClient:
    TWOCAPTCHA_IN = "https://2captcha.com/in.php"
    TWOCAPTCHA_RES = "https://2captcha.com/res.php"

    def __init__(self, provider: str = "2captcha", api_key: str = "",
                 timeout: int = 180) -> None:
        self.provider = "2captcha" if (provider or "").lower() in (
            "", "2captcha", "twocaptcha") else provider.lower()
        self.api_key = api_key
        self.timeout = timeout
        self.spent = 0.0          # 累计花费（元）
        self.solved = 0
        self.failed = 0

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
        if self.provider != "2captcha":
            raise CaptchaError(
                f"不支持的供应商：{self.provider}（只支持 2captcha）")
        if kind == "recaptcha_v2":
            return self.solve_recaptcha_v2_2captcha(
                kwargs["site_key"], kwargs["page_url"])
        raise CaptchaError(f"不支持的任务类型：{kind}")

    def cost_summary(self) -> str:
        return f"打码 {self.solved} 次，花费 ¥{self.spent:.4f}，失败 {self.failed}"
