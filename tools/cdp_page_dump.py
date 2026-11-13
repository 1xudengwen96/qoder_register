"""看注册页 DOM：确认页面渲染 + 选择器还在 + 是否存在滑块容器。"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.register.cdp_page import CDPPage  # noqa: E402

CDP = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:53888"

JS_DUMP = """(() => {
  const vis = (e) => !!(e.offsetWidth || e.offsetHeight || e.getClientRects().length);
  return {
    url: location.href,
    title: document.title,
    text: (document.body ? document.body.innerText : '').slice(0, 400),
    inputs: Array.from(document.querySelectorAll('input')).map(e => ({
      id: e.id, name: e.name, type: e.type, placeholder: e.placeholder || '',
      autocomplete: e.autocomplete, maxlength: e.maxLength,
      value: e.value || '', visible: vis(e),
    })),
    buttons: Array.from(document.querySelectorAll('button')).map(e => ({
      text: (e.innerText || '').trim().slice(0, 30), type: e.type,
      disabled: e.disabled, visible: vis(e),
    })),
    captcha: {
      img: !!document.querySelector('#aliyunCaptcha-img'),
      puzzle: !!document.querySelector('#aliyunCaptcha-puzzle'),
      window: !!document.querySelector('#aliyunCaptcha-window-float'),
      btn: !!document.querySelector('#captcha-element'),
    },
    scripts: Array.from(document.scripts).map(s => s.src || '').filter(Boolean)
              .filter(s => /captcha|aliyun/i.test(s)).slice(0, 5),
  };
})()"""


def main() -> int:
    with CDPPage(CDP) as p:
        d = p.eval(JS_DUMP)
        print(json.dumps(d, ensure_ascii=False, indent=2))
        print("\n选择器核对：")
        for sel in ("#basic_firstName", "#basic_lastName", "#basic_email",
                    "input[type=checkbox]", "button[type=submit]",
                    "#aliyunCaptcha-img", "#captcha-element"):
            print(f"  {sel:26s} {'OK' if p.exists(sel) else '缺失'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
