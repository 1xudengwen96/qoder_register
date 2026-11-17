"""额度读取：没有抓包接口时，直接读账号页（登录态在浏览器 profile 里）。

实测位置（中文/英文站同一套 DOM）：

    https://qoder.com/account/profile      → 计划名（Free / Pro Trial）+ 提示文案
        "Download and sign in to the client to claim your free Pro Trial and 300 Credits."
        ↑ 这句话就是领取条件：网页注册完还得客户端登录一次才算领
    https://qoder.com/account/usage        → 真实额度数字
        "Plan Credits / Monthly quota and usage for the current plan."
        "0 / 0 (0% used) 0 left"

所以 ``claim.credit_probe_url`` 为空时，走这里解析，不需要抓包。
"""

from __future__ import annotations

import dataclasses
import logging
import re
import time
from pathlib import Path

log = logging.getLogger("claim.credits")

USAGE_URL = "https://qoder.com/account/usage"
PROFILE_URL = "https://qoder.com/account/profile"

# 0 / 0 (0% used) 0 left
_USAGE_RE = re.compile(
    r"([\d,]+)\s*/\s*([\d,]+)\s*\(\s*(\d+)%\s*used\s*\)\s*([\d,]+)\s*left", re.I)
# 300 Credits
_CREDIT_RE = re.compile(r"([\d,]+)\s*Credits?", re.I)
# 徽章形态： "| Free | email" / "| Pro Trial | email"
_PLAN_BADGE_RE = re.compile(
    r"\|\s*(Free|Pro\s*Trial|Pro\+|Pro|Ultimate)\s*\|", re.I)
# 兜底：只认这几个，不含 Enterprise/Team（导航栏里到处都是）
_PLAN_RE = re.compile(r"\b(Free|Pro\s*Trial|Pro\+|Pro|Ultimate)\b", re.I)


@dataclasses.dataclass(slots=True)
class Credits:
    used: int = 0
    total: int = 0
    left: int = 0
    percent: int = 0
    plan: str = ""
    source: str = ""
    raw: str = ""

    @property
    def credits(self) -> int:
        """入库用的“持有额度”：优先剩余，其次总量。

        之前写 ``self.left or self.total``，剩余恰好为 0（已用光/权益还没
        到账）会被误报成总量 300。改成只信 usage 页读到的 left——
        ``source`` 是 ``usage-page`` 才算真读到，0 也是有效值。
        """
        return self.left if self.source.startswith("usage-page") else self.total

    def as_dict(self) -> dict:
        return {"used": self.used, "total": self.total, "left": self.left,
                "percent": self.percent, "plan": self.plan,
                "source": self.source}


def parse_usage(text: str) -> Credits:
    """从 /account/usage 的正文里抠额度。"""
    out = Credits(raw=(text or "")[:2000])
    m = _USAGE_RE.search(text or "")
    if m:
        out.used = int(m.group(1).replace(",", ""))
        out.total = int(m.group(2).replace(",", ""))
        out.percent = int(m.group(3))
        out.left = int(m.group(4).replace(",", ""))
        out.source = "usage-page"
        return out
    # 退一步：只找到 "N Credits" 就当作总量
    hits = [int(x.replace(",", "")) for x in _CREDIT_RE.findall(text or "")]
    if hits:
        out.total = max(hits)
        out.left = out.total
        out.source = "usage-page(loose)"
    return out


def parse_plan(text: str) -> str:
    body = text or ""
    m = _PLAN_BADGE_RE.search(body)
    if m:
        return m.group(1).strip()
    # 退一步：只看前 400 字，避开页面下方导航/页脚
    m = _PLAN_RE.search(body[:400])
    return m.group(1).strip() if m else ""


def read_credits(cfg, *, env_name: str = "", region: str = "us",
                 proxy=None, timeout: int = 40) -> Credits:
    """开窗口读账号页，解析计划与额度。登录态靠 profile 里的 cookie。"""
    from app.register import bb_cdp
    from app.register.bitbrowser import BitBrowserClient
    from app.register.cdp_page import new_page

    api_base = "http://127.0.0.1:54345"
    if cfg is not None:
        from app.config import get
        api_base = get(cfg, "browser.bitbrowser.api_base", api_base)
    client = BitBrowserClient(api_base)
    launch = None
    page = None
    out = Credits()
    try:
        launch = bb_cdp.open_for_register(
            client, proxy=proxy, region=region,
            env_name=env_name or "qoder_login", reuse_env=True,
        )
        page = new_page(launch.endpoint, "about:blank")
        page.goto(USAGE_URL, wait_ms=5000)
        time.sleep(2)
        usage_text = page.text()
        out = parse_usage(usage_text)
        page.goto(PROFILE_URL, wait_ms=4000)
        time.sleep(1)
        prof_text = page.text()
        out.plan = parse_plan(prof_text)
        if not out.plan:
            out.plan = "Pro Trial" if "Pro Trial" in prof_text else ""
        log.info("额度读取：%s 计划=%s", out.as_dict(), out.plan)
        return out
    except Exception as exc:  # noqa: BLE001
        out.source = f"error:{type(exc).__name__}"
        log.warning("额度读取失败：%s", exc)
        return out
    finally:
        try:
            if page is not None:
                page.close()
        except Exception:  # noqa: BLE001
            pass
        if launch:
            bb_cdp.close_env(client, launch.env_id)


def claim_hint(profile_text: str) -> bool:
    """账号页是否仍在提示「去客户端领取」——说明权益还没到账。"""
    low = (profile_text or "").lower()
    return ("sign in to the client" in low) or ("客户端" in profile_text and "领取" in profile_text)
