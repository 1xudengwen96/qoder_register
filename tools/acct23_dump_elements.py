"""dump acct23 环境 selectAccounts 页的全部可点元素（含 tag/文本/坐标/class）。"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.register.bitbrowser import BitBrowserClient  # noqa: E402
from app.register.cdp_page import new_page  # noqa: E402

JS = """(() => {
  const out = [];
  const els = document.querySelectorAll('button, a, [role=button], div, span, li');
  for (const el of els) {
    const r = el.getBoundingClientRect();
    if (r.width < 10 || r.height < 10) continue;
    const txt = (el.innerText || '').trim();
    if (!txt || txt.length > 40) continue;
    const clickable = el.tagName === 'BUTTON' || el.tagName === 'A'
      || el.getAttribute('role') === 'button'
      || !!el.onclick || el.style.cursor === 'pointer'
      || getComputedStyle(el).cursor === 'pointer';
    out.push({
      tag: el.tagName, cls: String(el.className).slice(0, 40),
      text: txt, x: Math.round(r.x + r.width / 2), y: Math.round(r.y + r.height / 2),
      w: Math.round(r.width), h: Math.round(r.height), clickable,
    });
  }
  return {url: location.href, body: (document.body.innerText || '').slice(0, 300),
          els: out.slice(0, 40)};
})()"""


def main() -> int:
    client = BitBrowserClient()
    env = next((e for e in client.list_all_envs() if e.name == "acct23"), None)
    launch = client.open_env(env.id)
    page = new_page(launch.cdp_http, "about:blank")
    # 找已开着的 selectAccounts tab；没有就导航当前链接
    data = page.eval(JS) or {}
    if "selectAccounts" not in str(data.get("url") or ""):
        link = (ROOT / "data" / "clipboard_link.txt").read_text(
            encoding="utf-8-sig").strip()
        page.goto(link, wait_ms=6000)
        data = page.eval(JS) or {}
    out = {"url": data.get("url"), "body": data.get("body"), "els": data.get("els")}
    (ROOT / "data" / "acct23_elements.json").write_text(
        json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(out, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
