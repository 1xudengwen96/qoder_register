"""验证：同一 account_tag 跨浏览器进程的指纹是否完全一致。

这是最关键的可行性指标。若同一账号两次访问（比如注册后回来领取）
指纹漂移，检测器直接认定异常，前面所有伪装都白费。

用法：
    python tools/verify_seed_stability.py
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from playwright.sync_api import sync_playwright  # noqa: E402

from app.register.fingerprint_engine import make_profile, stealth_init_script  # noqa: E402

PROBE = r"""
() => {
  const c = document.createElement('canvas');
  c.width = 240; c.height = 60;
  const ctx = c.getContext('2d');
  ctx.textBaseline = 'top';
  ctx.font = '15px "Segoe UI"';
  ctx.fillStyle = '#f60';
  ctx.fillRect(0, 0, 240, 60);
  ctx.fillStyle = '#069';
  ctx.fillText('seed-stability-probe-指纹', 3, 3);
  const d = ctx.getImageData(0, 0, 240, 60).data;
  let h = 0;
  for (let i = 0; i < d.length; i += 61) { h = ((h << 5) - h + d[i]) | 0; }
  const gl = document.createElement('canvas').getContext('webgl');
  const dbg = gl && gl.getExtension('WEBGL_debug_renderer_info');
  return {
    canvas: h.toString(16),
    ua: navigator.userAgent,
    tz: Intl.DateTimeFormat().resolvedOptions().timeZone,
    locale: Intl.DateTimeFormat().resolvedOptions().locale,
    hw: navigator.hardwareConcurrency,
    mem: navigator.deviceMemory,
    vendor: dbg ? gl.getParameter(dbg.UNMASKED_VENDOR_WEBGL) : null,
    renderer: dbg ? gl.getParameter(dbg.UNMASKED_RENDERER_WEBGL) : null,
  };
}
"""


def collect(account_tag: str) -> dict:
    p = make_profile(region="us", seed=account_tag)
    with sync_playwright() as pw:
        b = pw.chromium.launch(headless=True,
                               args=["--disable-blink-features=AutomationControlled"])
        ctx = b.new_context(user_agent=p.user_agent, locale=p.locale,
                            timezone_id=p.timezone,
                            viewport={"width": p.viewport_w, "height": p.viewport_h})
        ctx.add_init_script(stealth_init_script(p))
        page = ctx.new_page()
        page.goto("data:text/html,<html><body></body></html>")
        out = page.evaluate(PROBE)
        ctx.close()
        b.close()
    out["_profile_seed"] = p.canvas_noise_seed
    return out


def main() -> int:
    tag = "acct_1024"
    print(f"=== 同一 account_tag={tag} 跑三次独立进程 ===\n")

    # 每次起独立 python 进程，模拟真实使用（流水线每轮是新进程）
    script = (
        "import sys, json;"
        f"sys.path.insert(0, r'{ROOT}');"
        "from tools.verify_seed_stability import collect;"
        f"print(json.dumps(collect('{tag}')))"
    )
    samples = []
    for i in range(3):
        r = subprocess.run([sys.executable, "-c", script],
                           capture_output=True, text=True, timeout=180)
        if r.returncode != 0:
            print(f"第 {i+1} 次失败：{r.stderr[:300]}")
            return 2
        samples.append(json.loads(r.stdout.strip().splitlines()[-1]))
        print(f"第 {i+1} 次：canvas={samples[-1]['canvas']}  "
              f"tz={samples[-1]['tz']}  hw={samples[-1]['hw']}  "
              f"seed={samples[-1]['_profile_seed']}")

    keys = ["canvas", "ua", "tz", "locale", "hw", "mem", "vendor", "renderer",
            "_profile_seed"]
    print()
    all_ok = True
    for k in keys:
        vals = {s[k] for s in samples}
        ok = len(vals) == 1
        all_ok &= ok
        shown = list(vals)[0]
        if isinstance(shown, str) and len(shown) > 60:
            shown = shown[:60] + "..."
        print(f"[{'PASS' if ok else 'FAIL'}] {k:<14} {shown}"
              f"{'' if ok else '  <== 三次不一致：' + str(vals)}")

    print(f"\n结论：同一 account_tag 的指纹{'完全稳定' if all_ok else '会漂移'}")
    return 0 if all_ok else 2


if __name__ == "__main__":
    raise SystemExit(main())
