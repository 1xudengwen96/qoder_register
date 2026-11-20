"""填表事件取证：程序 CDP 注入 vs 真实人手，页面看到的事件流差在哪。

背景（2026-11-19）：拟人慢速填表后注册仍在 OTP 验证接口被 Baxia 弹
九宫格；用户同窗口手动注册却正常 → 怀疑 CDP 注入事件与真实硬件输入
在页面遥测层面存在可分辨差异（如 pointermove 的 coalescedEvents：
真实硬件一帧多样本，CDP 注入永远单样本）。

跑法：
    $env:PYTHONPATH='.;.pylibs'
    python tools/event_forensics.py            # 只跑程序注入段
    python tools/event_forensics.py --manual 15  # 注入后再留 15s 给人手操作

产物：data/artifacts/forensics_cdp.json / forensics_manual.json
"""

from __future__ import annotations

import argparse
import json
import random
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / ".pylibs"))

from app.register import bb_cdp                      # noqa: E402
from app.register.bitbrowser import BitBrowserClient, resolve_base  # noqa: E402
from app.register.cdp_page import CDPPage, new_page  # noqa: E402
from app.config import get, load_config              # noqa: E402
from app import secrets                              # noqa: E402

# 记录器：pointer/mouse/key 全链 + isTrusted + coalesced 样本数 + 帧内时序
JS_RECORDER = """(() => {
  if (window.__rec) return 'already';
  window.__rec = [];
  const push = (kind, e, extra) => {
    if (window.__rec.length > 6000) return;
    let coal = -1;
    try { if (e.getCoalescedEvents) coal = e.getCoalescedEvents().length; } catch (x) {}
    window.__rec.push(Object.assign({
      k: kind, t: Math.round(performance.now() * 100) / 100,
      tr: e.isTrusted, coal: coal,
      x: Math.round(e.clientX || 0), y: Math.round(e.clientY || 0),
    }, extra || {}));
  };
  ['pointerdown','pointerup','pointermove','mousedown','mouseup','mousemove','click']
    .forEach(k => window.addEventListener(k, e => push(k, e), true));
  ['keydown','keypress','keyup'].forEach(k =>
    window.addEventListener(k, e => push(k, e, {key: e.key, code: e.code, rep: e.repeat}), true));
  ['beforeinput','input'].forEach(k =>
    window.addEventListener(k, e => push(k, e, {it: e.inputType || '', data: e.data || ''}), true));
  ['focus','blur'].forEach(k =>
    window.addEventListener(k, e => push(k, e, {tgt: (e.target && e.target.id) || e.target.tagName}), true));
  return 'installed';
})()"""

TEST_PAGE = "https://example.com/"

# data: 页面上 Chrome 会丢弃 CDP 注入的鼠标事件（安全限制），
# 必须用真实 https 页；加载后注入一个输入框当靶子
JS_INJECT_INPUT = """(() => {
  if (document.getElementById('t')) return 'already';
  const i = document.createElement('input');
  i.id = 't';
  i.style.cssText = 'width:420px;height:40px;font-size:18px;margin:20px';
  i.placeholder = 'type here';
  document.body.prepend(i);
  return 'injected';
})()"""


def dump(page: CDPPage, path: Path, label: str) -> dict:
    rec = page.eval("JSON.stringify(window.__rec || [])")
    events = json.loads(rec) if isinstance(rec, str) else (rec or [])
    stat: dict = {}
    for e in events:
        k = e["k"]
        s = stat.setdefault(k, {"n": 0, "trusted": 0, "coal_hist": {}})
        s["n"] += 1
        s["trusted"] += 1 if e.get("tr") else 0
        c = str(e.get("coal", -1))
        s["coal_hist"][c] = s["coal_hist"].get(c, 0) + 1
    out = {"label": label, "count": len(events), "stat": stat, "events": events}
    path.write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"\n=== {label}: {len(events)} 事件 → {path.name} ===")
    for k, s in stat.items():
        print(f"  {k:<12} n={s['n']:<5} trusted={s['trusted']:<5} coalesced分布={s['coal_hist']}")
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--manual", type=int, default=0,
                    help="程序注入段结束后，留 N 秒给真人手动操作同一输入框")
    ap.add_argument("--env", default="qpool_27",
                    help="复用哪个已有窗口（新建环境会被比特数量上限拒绝）")
    ap.add_argument("--os", action="store_true",
                    help="程序段改走 OS 级 SendInput（验证 coalesced 修复）")
    args = ap.parse_args()

    secrets.export_env()
    cfg = load_config()
    client = BitBrowserClient(resolve_base(cfg))
    launch = bb_cdp.open_for_register(
        client, proxy=None, region=get(cfg, "region", "sg"),
        account_tag="forensics", env_name=args.env, reuse_env=True)
    print(f"窗口就绪 {launch.env_name} cdp={launch.endpoint}")
    page = new_page(launch.endpoint, TEST_PAGE, reuse_blank=True)
    try:
        page.goto(TEST_PAGE, wait_ms=2500)
        print("靶框:", page.eval(JS_INJECT_INPUT))
        if args.os:
            from app.register.os_input import OSInput

            page.os_input = OSInput(page, launch.pid)
            print("OS 输入 ready=", page.os_input.ready,
                  "偏移=", page.os_input.dx, page.os_input.dy)
        print("记录器:", page.eval(JS_RECORDER))
        page.eval("window.__rec.length = 0")

        # ---- 程序段：与注册流程完全相同的 click + fill 路径 ----
        time.sleep(1.0)
        page.click("#t", human=True)
        time.sleep(random.uniform(0.3, 0.6))
        page.type_text("Hello.World@2026", delay=(0.09, 0.26))
        time.sleep(0.8)
        dump(page, ROOT / ("data/artifacts/forensics_os.json" if args.os
                           else "data/artifacts/forensics_cdp.json"),
             "OS SendInput" if args.os else "CDP 注入")

        if args.manual > 0:
            page.eval("window.__rec.length = 0")
            print(f"\n>>> 现在请到 {launch.env_name} 窗口：点一下输入框，"
                  f"手动打几个字、晃晃鼠标（{args.manual}s）<<<", flush=True)
            time.sleep(args.manual)
            dump(page, ROOT / "data/artifacts/forensics_manual.json", "真人手动")
    finally:
        try:
            page.goto("about:blank", wait_ms=200)
            page.close()
        except Exception:
            pass
        bb_cdp.close_env(client, launch.env_id)
        print("窗口已关")
    return 0


if __name__ == "__main__":
    sys.exit(main())