"""启动比特环境读真实指纹（UA/平台/硬件/时区/语言），确认：
1. UA 的 Chrome 版本是否与内核 156 一致（旧 UA 是否真被用）
2. isRandomFinger=True 下我们的指纹字段哪些生效
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.register.bitbrowser import BitBrowserClient  # noqa: E402
from app.register import cdp_raw  # noqa: E402

ENV_NAME = "acct30"

JS = r"""(() => ({
  ua: navigator.userAgent,
  platform: navigator.platform,
  lang: navigator.language,
  langs: (navigator.languages || []).join(','),
  hc: navigator.hardwareConcurrency,
  mem: navigator.deviceMemory,
  tz: Intl.DateTimeFormat().resolvedOptions().timeZone,
  screen: screen.width + 'x' + screen.height + ' cd=' + screen.colorDepth,
  webdriver: navigator.webdriver,
}))()"""


def main() -> int:
    client = BitBrowserClient()
    items, _ = client.list_envs(page=0, page_size=200, name=ENV_NAME)
    env = next((e for e in items if e.name == ENV_NAME), None)
    if not env:
        print(f"环境 {ENV_NAME} 不存在")
        return 1
    endpoint, res = None, None
    try:
        res = client.open_env(env.id)
        print(f"endpoint={res.cdp_http}")
        cdp = cdp_raw.CDP(res.cdp_http)
        cdp.enable()
        out = cdp.evaluate(JS)
        for k, v in (out or {}).items():
            print(f"  {k}: {v}")
    finally:
        if endpoint:
            try:
                client.close_env(env.id)
            except Exception:
                pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
