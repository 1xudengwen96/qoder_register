"""eval 重放实验：用户手动注册 + 程序按原节奏重放注册流程的 JS 求值。

判定矩阵续：
- 手动 + 被动 CDP（零命令）      → 不弹（已证实）
- 手动 + 重放流程 eval（本工具） → 弹？  → JS 求值本身被探测
                                  → 不弹？→ 嫌疑转向 navigate/Network 等其它命令

用法：
    $env:PYTHONPATH='.;.pylibs'
    python tools/eval_replay.py --trace data/artifacts/eval_trace.jsonl --minutes 12

只重放纯读取型表达式（querySelector/rect/value/innerText 等），
跳过 fetch/click/dispatchEvent/setter/focus 等一切副作用表达式，
不导航、不输入——注册全程由用户手动完成。
"""
from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / ".pylibs"))

from app import secrets                              # noqa: E402
from app.config import get, load_config              # noqa: E402
from app.register import bb_cdp                      # noqa: E402
from app.register.bitbrowser import (BitBrowserClient, build_fingerprint,  # noqa: E402
                                     build_proxy_config, resolve_base)
from app.register.cdp_page import new_page           # noqa: E402
from app.register.fingerprint_engine import make_profile  # noqa: E402

# 副作用黑名单：命中即跳过，保证重放不干扰用户手动操作
SIDE_EFFECT = ("fetch(", ".click(", "dispatchEvent", "setter", "sign_out",
               ".focus()", "scrollIntoView", "localStorage", "document.cookie",
               "appendChild", "remove()", "innerHTML", "value =", "checked =",
               "XMLHttpRequest", "history.", "location =", "location.href =",
               "window.open", "alert(", "confirm(")


def load_readonly_evals(path: Path) -> list[dict]:
    items = []
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            d = json.loads(line)
        except ValueError:
            continue
        expr = d.get("expr", "")
        if not expr or any(b in expr for b in SIDE_EFFECT):
            continue
        items.append(d)
    return items


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--trace", default="data/artifacts/eval_trace.jsonl")
    ap.add_argument("--minutes", type=int, default=12)
    args = ap.parse_args()

    evals = load_readonly_evals(ROOT / args.trace)
    if not evals:
        print("trace 里没有可重放的只读求值")
        return 1
    print(f"载入只读求值 {len(evals)} 条（已过滤副作用表达式）", flush=True)

    secrets.export_env()
    cfg = load_config()
    client = BitBrowserClient(resolve_base(cfg))

    # 优先复用已有实验环境；没有才腾名额新建（避免反复删建）
    envs, _ = client.list_envs(page=0, page_size=500)
    existing = next((e for e in envs if e.name == "qfresh_replay"), None)
    if existing is not None:
        env_id = existing.id
        print(f"复用已有实验环境 {env_id}", flush=True)
    else:
        accounts = {}
        acc_path = ROOT / "data/.bb_pool_accounts.json"
        if acc_path.exists():
            accounts = json.loads(acc_path.read_text(encoding="utf-8"))
        keep = {v.get("name") for v in accounts.values()}
        pids = client.running_pids([e.id for e in envs])
        victim = next((e for e in envs if e.id not in pids
                       and e.name not in keep
                       and (e.name or "").startswith("qpool_")), None)
        if victim is None:
            print("没有可腾的空闲池窗")
            return 2
        print(f"腾名额：删除 {victim.name}", flush=True)
        client.delete_env(victim.id)
        prof = make_profile(region=get(cfg, "region", "sg"), seed="qreplay1")
        fp = build_fingerprint(
            screen_w=prof.viewport_w, screen_h=prof.viewport_h,
            timezone=prof.timezone, languages=[prof.locale, "en"],
            os_type=prof.platform, hardware_concurrency=prof.hardware_concurrency,
            device_memory=prof.device_memory, fonts=prof.fonts)
        r = client.call("/browser/update", {
            "name": "qfresh_replay", "remark": "qoder_register/replay-test",
            "browserFingerPrint": fp, **build_proxy_config(None)}, timeout=90)
        env_id = (r.get("data") or {}).get("id", "")
    launch = bb_cdp.open_for_register(
        client, proxy=None, region=get(cfg, "region", "sg"),
        env_id=env_id, env_name="qfresh_replay")
    print(f"窗口就绪 {launch.env_name} cdp={launch.endpoint}", flush=True)

    page = new_page(launch.endpoint, "about:blank", reuse_blank=True)
    print(">>> 请在窗口里手动注册（qoder.com/users/sign-up）。"
          "程序将按原节奏重放流程的 JS 求值，不导航不输入。 <<<", flush=True)

    base = launch.endpoint.rstrip("/")
    deadline = time.time() + args.minutes * 60
    t0 = time.time()
    trace_t0 = evals[0]["ts"]
    idx = 0
    fired = 0
    last_urls: list[str] = []
    try:
        while time.time() < deadline:
            if idx >= len(evals):
                # 一轮放完循环下一轮，窗口保持打开等用户填完
                idx = 0
                t0 = time.time()
                trace_t0 = evals[0]["ts"]
                print("一轮重放完成，循环继续（窗口保持打开）…", flush=True)
            # 按 trace 原始节奏推进（长等待压缩到 ≤2s，避免实验干等）
            target = t0 + min(evals[idx]["ts"] - trace_t0,
                              (evals[idx]["ts"] - trace_t0) * 0.3 + 2)
            wait = target - time.time()
            if wait > 0:
                time.sleep(min(wait, 1.0))
                continue
            try:
                page.eval(evals[idx]["expr"])
                fired += 1
            except Exception:  # noqa: BLE001
                pass
            idx += 1
            if fired % 100 == 0:
                print(f"已重放 {fired} 条求值…", flush=True)
            # 旁观 URL
            try:
                with urllib.request.urlopen(base + "/json/list", timeout=3) as resp:
                    ts = json.loads(resp.read().decode("utf-8", "replace"))
                urls = [t.get("url", "") for t in ts if t.get("type") == "page"]
                if urls != last_urls:
                    print(f"[{time.strftime('%H:%M:%S')}] URL: {urls}", flush=True)
                    last_urls = urls
                if any("/download" in u for u in urls):
                    print(">>> 已到下载页：手动注册成功。请回忆提交验证码后"
                          "有没有弹二次滑块/九宫格 <<<", flush=True)
                    break
            except Exception:  # noqa: BLE001
                pass
    finally:
        try:
            page.goto("about:blank", wait_ms=200)
            page.close()
        except Exception:
            pass
        bb_cdp.close_env(client, launch.env_id)
        print(f"实验结束：重放 {fired} 条求值，窗口已关。", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())