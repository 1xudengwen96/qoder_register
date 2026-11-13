"""2Captcha 阿里云验证码客户端。

官方文档：https://2captcha.com/api-docs/alibaba-captcha
任务类型：AlibabaTaskProxyless（用 2Captcha 自己的代理）/ AlibabaTask（用你自己的代理）

Qoder 的实测参数（2026-11-12）
------------------------------
从注册页内联脚本挖到：

    window.AliyunCaptchaConfig = { region: "sgp", prefix: "13lbkb5" }
    window.captchaScene = {
      "create_me_verification_codes": "udwgwv7k2",
      "create_sessions": "17on5mo7v",
      "create_verification_codes": "1r7eif79x"   ← 注册场景
    }

实际验证请求：

    POST https://13lbkb5.captcha-open-southeast.aliyuncs.com/
    body: ...&Action=InitCaptchaV3&SceneId=1r7eif79x&Language=en...

所以：

    region  = sgp
    prefix  = 13lbkb5
    sceneId = 1r7eif79x
    apiGetLib = https://o.alicdn.com/captcha-frontend/aliyunCaptcha/AliyunCaptcha.js

工作流
------
1. createTask(AlibabaTaskProxyless + 上述参数) → taskId
2. 轮询 getTaskResult → solution
3. solution 里是 token（u_atoken / u_asig），需要回填到页面
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field

import requests

CREATE_TASK = "https://2captcha.com/createTask"
GET_RESULT = "https://2captcha.com/getTaskResult"
GET_BALANCE = "https://2captcha.com/getBalance"

# Qoder 注册页实测参数
QODER_ALIYUN = {
    "websiteURL": "https://qoder.com/users/sign-up",
    "sceneId": "1r7eif79x",
    "prefix": "13lbkb5",
    "region": "sgp",
    "apiGetLib": ("https://o.alicdn.com/captcha-frontend/"
                  "aliyunCaptcha/AliyunCaptcha.js"),
}


class TwoCaptchaError(RuntimeError):
    def __init__(self, msg: str, code: str = "", raw: dict | None = None):
        super().__init__(msg)
        self.code = code
        self.raw = raw or {}


@dataclass(slots=True)
class AliyunSolution:
    ok: bool
    token: str = ""                # u_atoken
    sig: str = ""                  # u_asig
    raw: dict = field(default_factory=dict)
    elapsed: float = 0.0
    error: str = ""

    @property
    def payload(self) -> dict:
        """回填页面用的完整载荷。"""
        return self.raw.get("solution", {}) if self.raw else {}


class TwoCaptchaAliyun:
    """2Captcha 阿里云任务封装。"""

    def __init__(self, api_key: str, proxy: str | None = "http://127.0.0.1:7897",
                 timeout: int = 300, poll: float = 4.0) -> None:
        self.api_key = api_key
        self.proxy = proxy
        self.timeout = timeout
        self.poll = poll

    def _post(self, url: str, payload: dict) -> dict:
        proxies = {"http": self.proxy, "https": self.proxy} if self.proxy else None
        try:
            r = requests.post(url, json=payload, proxies=proxies, timeout=40)
        except Exception as exc:
            raise TwoCaptchaError(f"请求失败 {type(exc).__name__}: {exc}") from exc
        try:
            return r.json()
        except Exception:
            raise TwoCaptchaError(f"返回非 JSON: {r.text[:200]}")

    # ---------------------------------------------------------- 余额
    def balance(self) -> float:
        d = self._post(GET_BALANCE, {"clientKey": self.api_key})
        if d.get("errorId", 1) != 0:
            raise TwoCaptchaError(
                d.get("errorDescription") or d.get("errorCode") or "取余额失败",
                str(d.get("errorCode", "")), d)
        return float(d.get("balance", 0.0))

    # ---------------------------------------------------------- 建任务
    def create_aliyun_task(
        self,
        scene_id: str | None = None,
        prefix: str | None = None,
        region: str | None = None,
        website_url: str | None = None,
        api_get_lib: str | None = None,
        user_id: str = "",
        user_user_id: str = "",
        verify_type: str = "",
        user_certify_id: str = "",
        use_proxy: bool = False,
        proxy_type: str = "http",
        proxy_address: str = "",
        proxy_port: str = "",
        proxy_login: str = "",
        proxy_password: str = "",
    ) -> str:
        cfg = dict(QODER_ALIYUN)
        if scene_id:
            cfg["sceneId"] = scene_id
        if prefix:
            cfg["prefix"] = prefix
        if region:
            cfg["region"] = region
        if website_url:
            cfg["websiteURL"] = website_url
        if api_get_lib:
            cfg["apiGetLib"] = api_get_lib

        task: dict = {
            "type": "AlibabaTask" if use_proxy else "AlibabaTaskProxyless",
            "websiteURL": cfg["websiteURL"],
            "sceneId": cfg["sceneId"],
            "prefix": cfg["prefix"],
        }
        # 可选参数，非空才传
        for k, v in (
            ("region", cfg["region"]),
            ("apiGetLib", cfg["apiGetLib"]),
            ("userId", user_id),
            ("userUserId", user_user_id),
            ("verifyType", verify_type),
            ("userCertifyId", user_certify_id),
        ):
            if v:
                task[k] = v

        if use_proxy:
            task.update({
                "proxyType": proxy_type,
                "proxyAddress": proxy_address,
                "proxyPort": proxy_port,
            })
            if proxy_login:
                task["proxyLogin"] = proxy_login
            if proxy_password:
                task["proxyPassword"] = proxy_password

        d = self._post(CREATE_TASK, {"clientKey": self.api_key, "task": task})
        if d.get("errorId", 1) != 0:
            raise TwoCaptchaError(
                d.get("errorDescription") or "建任务失败",
                str(d.get("errorCode", "")), d)
        tid = d.get("taskId")
        if not tid:
            raise TwoCaptchaError("未返回 taskId", raw=d)
        return str(tid)

    # ---------------------------------------------------------- 轮询
    def wait_result(self, task_id: str) -> AliyunSolution:
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
            if status == "ready":
                sol = d.get("solution") or {}
                return AliyunSolution(
                    ok=True,
                    token=str(sol.get("u_atoken") or sol.get("token") or ""),
                    sig=str(sol.get("u_asig") or sol.get("sig") or ""),
                    raw=d,
                    elapsed=time.time() - t0,
                )
            # 其他状态视为失败
            raise TwoCaptchaError(f"未知状态 {status}", raw=d)

        raise TwoCaptchaError(f"超时 {self.timeout}s")

    # ---------------------------------------------------------- 一步到位
    def solve(self, **kwargs) -> AliyunSolution:
        tid = self.create_aliyun_task(**kwargs)
        return self.wait_result(tid)
