"""验证：打码模块。只走 2Captcha（自研识别与其他供应商已移除）。

用法：
    python tools/verify_captcha.py
"""

from __future__ import annotations

import base64
import io
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.net.captcha_solver import (  # noqa: E402
    CaptchaError,
    CaptchaSolver,
    CaptchaTask,
    TwoCaptcha,
)


def make_captcha_image(text: str) -> str:
    """用 PIL 画一张文字图，模拟图片验证码（不发真实请求，仅测形状）。"""
    from PIL import Image, ImageDraw, ImageFont

    img = Image.new("RGB", (160, 60), (255, 255, 255))
    d = ImageDraw.Draw(img)
    try:
        font = ImageFont.truetype("arial.ttf", 36)
    except Exception:
        font = ImageFont.load_default()
    d.text((14, 10), text, fill=(10, 10, 10), font=font)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return base64.b64encode(buf.getvalue()).decode()


class _FakeTwoCaptcha(TwoCaptcha):
    """不发网络请求的 2Captcha 形状替身。"""

    def __init__(self) -> None:
        super().__init__("dummy_key")

    def solve(self, task: CaptchaTask):
        from app.net.captcha_solver import CaptchaResult
        import time as _t
        return CaptchaResult("TOKEN_OK", self.name, self._cost(task.kind),
                             _t.time() - _t.time())


def main() -> int:
    checks: list[tuple[str, bool, str]] = []

    # ---- 1. 2Captcha 能力矩阵 ----
    p = TwoCaptcha("dummy_key")
    kinds = [k for k in ("recaptcha_v2", "recaptcha_v3", "hcaptcha",
                         "turnstile", "image") if p.supports(k)]
    checks.append(("2Captcha 支持 5 类任务", len(kinds) == 5,
                   f"支持 {len(kinds)} 类: {kinds}"))

    # ---- 2. 调度器走 2Captcha ----
    fake = _FakeTwoCaptcha()
    solver = CaptchaSolver([fake])
    r = solver.solve(CaptchaTask(kind="image", image_b64=make_captcha_image("7A3K")))
    checks.append(("调度器走 2Captcha", r.provider == "2captcha" and r.token == "TOKEN_OK",
                   f"provider={r.provider} token={r.token}"))

    # ---- 3. 未配置/不支持类型要抛错，不能静默 ----
    empty = CaptchaSolver([])
    try:
        empty.solve(CaptchaTask(kind="image", image_b64="xx"))
        checks.append(("未配置时抛错", False, "静默返回了"))
    except CaptchaError:
        checks.append(("未配置时抛错", True, f"failed 计数={empty.failed}"))

    class Dead(TwoCaptcha):
        def solve(self, t):
            raise CaptchaError("模拟失效")

    dead = CaptchaSolver([Dead("k")])
    try:
        dead.solve(CaptchaTask(kind="image", image_b64="xx"))
        checks.append(("供应商失败时抛错", False, "静默返回了"))
    except CaptchaError:
        checks.append(("供应商失败时抛错", True, f"failed 计数={dead.failed}"))

    # ---- 4. 计费累计（image 单价 0.01） ----
    for _ in range(3):
        solver.solve(CaptchaTask(kind="image", image_b64="xx"))
    checks.append(("计费正确累计（image 单价 0.01）",
                   abs(solver.spent - 0.04) < 1e-6 and solver.solved == 4,
                   solver.cost_summary()))

    # ---- 5. 任务参数字段齐备 ----
    task = CaptchaTask(kind="recaptcha_v2", page_url="https://example.com/signup",
                       site_key="6Lc_test", proxy="http://u:p@1.2.3.4:8080")
    body_ok = all([task.page_url, task.site_key, task.proxy])
    checks.append(("任务参数可携带代理（token 与 IP 绑定）", body_ok,
                   f"proxy={task.proxy}"))

    # ---- 6. 请求参数构造（带代理） ----
    params = p._params(task)
    params_ok = (params.get("proxy") == "1.2.3.4:8080"
                 and params.get("method") == "userrecaptcha")
    checks.append(("2Captcha 请求参数构造正确", params_ok,
                   f"method={params.get('method')} proxy={params.get('proxy')}"))

    print("=== 打码模块验证 ===\n")
    passed = 0
    width = max(len(c[0]) for c in checks) + 2
    for name, ok, detail in checks:
        print(f"[{'PASS' if ok else 'FAIL'}] {name:<{width}} {detail}")
        passed += ok
    print(f"\n{passed}/{len(checks)} 通过")
    return 0 if passed == len(checks) else 2


if __name__ == "__main__":
    raise SystemExit(main())
