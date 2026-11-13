"""诊断：同一 profile_id 的 seed 是否稳定、Canvas 噪声是否确定。"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from playwright.sync_api import sync_playwright

from app.register.fingerprint_engine import make_profile, stealth_init_script

PROBE = r"""
() => {
  const c = document.createElement('canvas');
  c.width = 200; c.height = 50;
  const ctx = c.getContext('2d');
  ctx.textBaseline = 'top';
  ctx.font = '14px Arial';
  ctx.fillStyle = '#f60';
  ctx.fillRect(0, 0, 200, 50);
  ctx.fillStyle = '#069';
  ctx.fillText('fingerprint-probe', 2, 2);
  const d1 = ctx.getImageData(0, 0, 200, 50).data;
  const d2 = ctx.getImageData(0, 0, 200, 50).data;
  const hash = (d) => { let h = 0; for (let i = 0; i < d.length; i += 97) { h = ((h << 5) - h + d[i]) | 0; } return h.toString(16); };
  // 第三次：新画布重画一遍，模拟"换页面再采一次"
  const c2 = document.createElement('canvas');
  c2.width = 200; c2.height = 50;
  const ctx2 = c2.getContext('2d');
  ctx2.textBaseline = 'top';
  ctx2.font = '14px Arial';
  ctx2.fillStyle = '#f60';
  ctx2.fillRect(0, 0, 200, 50);
  ctx2.fillStyle = '#069';
  ctx2.fillText('fingerprint-probe', 2, 2);
  const d3 = ctx2.getImageData(0, 0, 200, 50).data;
  return { same_ctx_1: hash(d1), same_ctx_2: hash(d2), new_ctx: hash(d3) };
}
"""


def probe(seed: str, headless: bool) -> dict:
    p = make_profile(region="us", seed=seed)
    with sync_playwright() as pw:
        b = pw.chromium.launch(headless=headless,
                               args=["--disable-blink-features=AutomationControlled"])
        ctx = b.new_context(locale=p.locale, timezone_id=p.timezone,
                            viewport={"width": p.viewport_w, "height": p.viewport_h})
        ctx.add_init_script(stealth_init_script(p))
        page = ctx.new_page()
        page.goto("data:text/html,<html><body></body></html>")
        out = page.evaluate(PROBE)
        ctx.close()
        b.close()
    out["seed"] = p.canvas_noise_seed
    return out


def main() -> int:
    for i in range(3):
        # 注意：seed 参数只做 profile_id，不控制 canvas_noise_seed
        r = probe(f"diag_{i}", headless=True)
        print(f"第 {i+1} 次（新浏览器）: canvas_seed={r['seed']}")
        print(f"   同 ctx 两次: {r['same_ctx_1']} / {r['same_ctx_2']}  "
              f"{'一致' if r['same_ctx_1'] == r['same_ctx_2'] else '不一致'}")
        print(f"   新 ctx     : {r['new_ctx']}  "
              f"{'一致' if r['same_ctx_1'] == r['new_ctx'] else '不一致'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
