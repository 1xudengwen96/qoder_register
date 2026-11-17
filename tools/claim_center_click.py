"""客户端权益中心：找“权益中心/待领取/限时加赠”入口，点击进入领取流程。

中文文本用 \\uXXXX 转义写进 JS：CDP 传输层中文会出现 mojibake，
python 侧拼字符串匹配不可靠，但 JS 源码里的 \\u 转义在页面内展开是原生字符，
页面内匹配（innerText 含字符）不受编码层影响。
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.register import cdp_raw  # noqa: E402

PORT = 9222

# 待匹配的文案（python 侧，稍后转 \uXXXX 注入 JS）
KEYWORDS = ["权益中心", "待领取", "限时加赠", "领取", "Credits"]

JS_FIND = """(() => {
  const kws = %KEYWORDS%;
  const out = [];
  const els = document.querySelectorAll('button, a, [role=button], div, span');
  for (const el of els) {
    const r = el.getBoundingClientRect();
    if (r.width < 8 || r.height < 8) continue;
    const txt = (el.innerText || '').trim();
    if (!txt) continue;
    for (const k of kws) {
      if (txt.includes(k)) {
        out.push({tag: el.tagName, text: txt.slice(0, 60),
                  x: Math.round(r.x + r.width / 2),
                  y: Math.round(r.y + r.height / 2),
                  w: Math.round(r.width), h: Math.round(r.height), kw: k});
        break;
      }
    }
  }
  return out.slice(0, 60);
})()"""

JS_BODY = "(() => (document.body ? document.body.innerText : '').slice(0, 1200))()"


def js_with_kws() -> str:
    kws = json.dumps(KEYWORDS, ensure_ascii=True)
    return JS_FIND.replace("%KEYWORDS%", kws)


def main() -> int:
    targets = [t for t in cdp_raw.http_targets(f"127.0.0.1:{PORT}")
               if t.get("type") == "page"]
    tgt = targets[0]
    with cdp_raw.CDP(f"127.0.0.1:{PORT}",
                     ws_url=tgt["webSocketDebuggerUrl"]) as page:
        page.enable()
        hits = page.evaluate(js_with_kws()) or []
        print(f"命中 {len(hits)} 个：")
        for h in hits:
            print(f"  {h['tag']:6s} ({h['x']},{h['y']}) {h['w']}x{h['h']} "
                  f"[kw={h['kw']}] {h['text'][:50]}")

        if not hits:
            print("没找到权益入口")
            return 1

        def _click(h) -> None:
            for typ, btns in (("mousePressed", 1), ("mouseReleased", 0)):
                page.call("Input.dispatchMouseEvent", {
                    "type": typ, "x": float(h["x"]), "y": float(h["y"]),
                    "button": "left", "clickCount": 1, "buttons": btns})

        # 第一步：点“权益中心，有权益待领取”行的 24x24 图标（已实测：
        # 点它弹出账号菜单，菜单里有“权益中心”菜单项；点整行中心无效果）
        benefit = [h for h in hits
                   if h["kw"] in ("权益中心", "待领取")]
        pick = min(benefit or hits, key=lambda h: h["w"] * h["h"])
        print(f"\n点击1 ({pick['x']},{pick['y']}) kw={pick['kw']} "
              f"{pick['text'][:40]}")
        _click(pick)
        time.sleep(2.5)

        # 第二步：菜单弹出后，点菜单里的“权益中心”菜单项（文本精确短匹配）
        hits2 = page.evaluate(js_with_kws()) or []
        menu = [h for h in hits2
                if (h["text"].strip().rstrip("，, ").endswith("权益中心")
                    or h["text"].strip() == "权益中心")
                and h["w"] * h["h"] < 60000]
        if menu:
            m = min(menu, key=lambda h: h["w"] * h["h"])
            print(f"点击2 ({m['x']},{m['y']}) 菜单项 {m['text'][:30]}")
            _click(m)
        else:
            print("菜单里没找到权益中心项（可能第一步已直接进入）")
        time.sleep(4)
        body = page.evaluate(JS_BODY) or ""
        print("\n点击后正文：")
        print(body[:1500])
        (ROOT / "data" / "client_benefit.txt").write_text(
            body, encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
