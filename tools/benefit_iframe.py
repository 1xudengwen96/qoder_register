"""权益中心 iframe（growth-page/activity-iframe）：dump 内容并点领取。"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.register import cdp_raw  # noqa: E402

PORT = 9222
OUT = ROOT / "data" / "benefit_iframe.txt"

JS_DUMP = """(() => {
  const els = [];
  for (const el of document.querySelectorAll('button, a, [role=button]')) {
    const r = el.getBoundingClientRect();
    if (r.width < 6 || r.height < 6) continue;
    els.push({tag: el.tagName, text: (el.innerText || '').trim().slice(0, 40),
              x: Math.round(r.x + r.width / 2), y: Math.round(r.y + r.height / 2),
              w: Math.round(r.width), h: Math.round(r.height),
              disabled: el.disabled === true});
  }
  return {url: location.href,
          body: (document.body ? document.body.innerText : '').slice(0, 1500),
          els: els.slice(0, 30)};
})()"""


def find_iframe():
    for t in cdp_raw.http_targets(f"127.0.0.1:{PORT}"):
        if t.get("type") == "iframe" and "activity-iframe" in (t.get("url") or ""):
            return t
    return None


def main() -> int:
    tgt = find_iframe()
    if not tgt:
        print("没找到 activity-iframe")
        return 1
    print(f"iframe: {tgt['url']}")
    with cdp_raw.CDP(f"127.0.0.1:{PORT}",
                    ws_url=tgt["webSocketDebuggerUrl"]) as page:
        page.enable()
        data = page.evaluate(JS_DUMP) or {}
        print("body：")
        print(data.get("body") or "")
        print("\n可点元素：")
        for e in data.get("els") or []:
            print(f"  {e['tag']:6s} ({e['x']},{e['y']}) {e['w']}x{e['h']} "
                  f"disabled={e['disabled']} {e['text'][:36]}")
        OUT.write_text(json.dumps(data, ensure_ascii=False, indent=2),
                       encoding="utf-8")

        # 找领取按钮（语言无关优先 class/role，其次文案）
        els = data.get("els") or []
        kws = ["领取", "立即领取", "Claim", "claim", "Lanjutkan", "一键领取", "領取"]
        pick = None
        for e in els:
            if e.get("disabled"):
                continue
            if any(k in (e.get("text") or "") for k in kws):
                pick = e
                break
        if not pick:
            print("\n没找到明确的领取按钮（看上面列表再定坐标）")
            return 1
        print(f"\n点击 ({pick['x']},{pick['y']}) {pick['text']}")
        for typ, btns in (("mousePressed", 1), ("mouseReleased", 0)):
            page.call("Input.dispatchMouseEvent", {
                "type": typ, "x": float(pick["x"]), "y": float(pick["y"]),
                "button": "left", "clickCount": 1, "buttons": btns})
        time.sleep(4)
        after = page.evaluate(JS_DUMP) or {}
        print("点击后 body：")
        print((after.get("body") or "")[:800])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
