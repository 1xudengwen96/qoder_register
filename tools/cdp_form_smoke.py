"""CDP 表单填写的机械验证（不提交，不消耗邮箱）。

    python tools/cdp_form_smoke.py --cdp http://127.0.0.1:53888
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.register.cdp_page import CDPPage, new_page  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--cdp", default="http://127.0.0.1:53888")
    ap.add_argument("--url", default="https://qoder.com/users/sign-up")
    ap.add_argument("--shot", default="data/artifacts/cdp_form_smoke.png")
    args = ap.parse_args()

    page = new_page(args.cdp, "about:blank")
    fails = []
    try:
        page.goto(args.url, wait_ms=2500)
        print(f"url={page.url()}")

        if not page.sel_ready("#basic_firstName", timeout=30):
            print("FAIL 姓名输入框没出现")
            return 1

        for sel, text in (("#basic_firstName", "James"),
                          ("#basic_lastName", "Carter"),
                          ("#basic_email", "smoke.test.001@example.com")):
            ok = page.fill(sel, text, human=True)
            got = page.value(sel)
            print(f"{'PASS' if ok and got == text else 'FAIL'} {sel:22s} "
                  f"value={got!r}")
            if got != text:
                fails.append(sel)

        # React 是否感知：读一下受控 state 的影子（表单序列化）
        checked = page.check("input[type=checkbox]")
        state = page.eval("""(() => {
          const cb = document.querySelector('input[type=checkbox]');
          return {checked: cb ? cb.checked : null,
                  bodyHasEmail: (document.body.innerText || '')
                                 .includes('smoke.test.001@example.com')};
        })()""")
        print(f"{'PASS' if checked else 'FAIL'} checkbox checked={state}")

        # 按钮可点击性（只看坐标，不点）
        btn = page.rect("button[type=submit]")
        print(f"submit 按钮 rect={btn}")

        page.shot(args.shot)
        print(f"截图 {args.shot}")
        print("结果:", "全部通过" if not fails else f"失败项 {fails}")
        return 0 if not fails else 1
    finally:
        page.close()


if __name__ == "__main__":
    raise SystemExit(main())
