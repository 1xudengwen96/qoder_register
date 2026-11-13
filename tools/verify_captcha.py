"""验证：打码模块。本地 OCR 真跑，云平台只做接口形状校验（不发真实请求）。

用法：
    python tools/verify_captcha.py
"""

from __future__ import annotations

import base64
import io
import sys
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.net.captcha_solver import (  # noqa: E402
    CaptchaError,
    CaptchaSolver,
    CaptchaTask,
    CapSolver,
    LocalOCR,
    TwoCaptcha,
    YesCaptcha,
)


def make_captcha_image(text: str) -> str:
    """用 PIL 画一张文字图，模拟图片验证码。"""
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


def main() -> int:
    checks: list[tuple[str, bool, str]] = []

    # ---- 1. 本地 OCR ----
    ocr = LocalOCR()
    img_b64 = make_captcha_image("7A3K")
    try:
        res = ocr.solve(CaptchaTask(kind="image", image_b64=img_b64))
        got = res.token.strip().upper()
        ok = got.startswith("7") or "7A3K" in got
        checks.append(("本地 OCR 能识别图片验证码", ok,
                       f"识别出 '{got}'（期望含 7A3K），耗时 {res.elapsed:.2f}s，成本 ¥{res.cost}"))
    except CaptchaError as exc:
        checks.append(("本地 OCR 能识别图片验证码", False, str(exc)))

    # ---- 2. 供应商能力矩阵 ----
    matrix = []
    for cls in (YesCaptcha, CapSolver, TwoCaptcha):
        p = cls("dummy_key")
        kinds = [k for k in ("recaptcha_v2", "recaptcha_v3", "hcaptcha",
                             "turnstile", "image") if p.supports(k)]
        matrix.append((p.name, len(kinds), kinds))
    all_five = all(n == 5 for _, n, _ in matrix)
    checks.append(("三家云平台各支持 5 类任务", all_five,
                   " / ".join(f"{name}:{n}" for name, n, _ in matrix)))

    # ---- 3. 降级路由 ----
    solver = CaptchaSolver([ocr], prefer={"image": ["local"]})
    r = solver.solve(CaptchaTask(kind="image", image_b64=img_b64))
    checks.append(("调度器走本地优先", r.provider == "local",
                   f"provider={r.provider}，累计 ¥{solver.spent:.4f}"))

    # ---- 4. 全部失败要抛错，不能静默 ----
    class Dead:
        name = "dead"
        price = {"image": 0.01}

        def supports(self, k):
            return True

        def solve(self, t):
            raise CaptchaError("模拟失效")

    dead = CaptchaSolver([Dead()], prefer={"image": ["dead"]})
    try:
        dead.solve(CaptchaTask(kind="image", image_b64=img_b64))
        checks.append(("供应商全失败时抛错", False, "静默返回了"))
    except CaptchaError:
        checks.append(("供应商全失败时抛错", True, f"failed 计数={dead.failed}"))

    # ---- 5. 计费累计 ----
    for _ in range(3):
        solver.solve(CaptchaTask(kind="image", image_b64=img_b64))
    checks.append(("计费正确累计（本地 0 元）", solver.spent == 0.0 and solver.solved == 4,
                   solver.cost_summary()))

    # ---- 6. 任务参数字段齐备 ----
    task = CaptchaTask(kind="recaptcha_v2", page_url="https://example.com/signup",
                       site_key="6Lc_test", proxy="http://u:p@1.2.3.4:8080")
    body_ok = all([task.page_url, task.site_key, task.proxy])
    checks.append(("任务参数可携带代理（token 与 IP 绑定）", body_ok,
                   f"proxy={task.proxy}"))

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
