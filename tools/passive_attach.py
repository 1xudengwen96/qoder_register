"""被动附着实验：CDP 只连接、零命令，用户手动注册，看二次验证是否出现。

判定矩阵：
- 手动 + 无 CDP        → 不弹（用户已证实）
- 手动 + 被动 CDP（本工具）→ 弹？  → CDP 附着本身被探测
- 手动 + 被动 CDP      → 不弹？ → 我们的主动操作（evaluate/输入）是触发点

用法：
    $env:PYTHONPATH='.;.pylibs'
    python tools/passive_attach.py --minutes 12
工具打开 qfresh_test1 窗口并建立 CDP 连接后**不发任何命令**，
用户在窗口里手动注册；工具只通过 HTTP /json/list 观察 URL 变化。
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
from app.register.bitbrowser import BitBrowserClient, resolve_base  # noqa: E402
from app.register.cdp_page import new_page           # noqa: E402
from app.register.qoder_signup_cdp import SIGNUP_URL  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--minutes", type=int, default=12)
    ap.add_argument("--network", action="store_true",
                    help="附着后只开 Network.enable（验证它是否被探测）")
    ap.add_argument("--env-name", default="qfresh_replay",
                    help="复用哪个已有实验环境")
    args = ap.parse_args()

    secrets.export_env()
    cfg = load_config()
    client = BitBrowserClient(resolve_base(cfg))
    envs, _ = client.list_envs(page=0, page_size=500)
    env = next((e for e in envs if e.name == args.env_name), None)
    if env is None:
        print(f"找不到实验环境 {args.env_name}")
        return 1
    launch = bb_cdp.open_for_register(
        client, proxy=None, region=get(cfg, "region", "sg"),
        env_id=env.id, env_name=args.env_name)
    print(f"窗口就绪 {launch.env_name} cdp={launch.endpoint}", flush=True)

    # 建立 CDP 连接（构造即连接；enable() 已是空操作，零 domain）。
    # 之后不调用任何 eval/input/navigate —— 纯被动附着。
    page = new_page(launch.endpoint, "about:blank", reuse_blank=True)
    # 环境准备：清 cookie 保证未登录态（一次性浏览器级操作，非实验变量）
    try:
        page.cdp.call("Network.clearBrowserCookies")
    except Exception:  # noqa: BLE001
        pass
    if args.network:
        page.cdp.call("Network.enable")
        print("已开 Network.enable（其余零命令）。请在窗口里手动注册。",
              flush=True)
    else:
        print("CDP 已附着（零命令被动模式）。请在窗口里手动注册。", flush=True)
    print(f"观察 {args.minutes} 分钟，只读 HTTP /json/list 看 URL。", flush=True)

    base = launch.endpoint.rstrip("/")
    deadline = time.time() + args.minutes * 60
    last_urls: list[str] = []
    reached_download = False
    try:
        while time.time() < deadline:
            try:
                with urllib.request.urlopen(base + "/json/list", timeout=5) as r:
                    ts = json.loads(r.read().decode("utf-8", "replace"))
                urls = [t.get("url", "") for t in ts if t.get("type") == "page"]
                if urls != last_urls:
                    stamp = time.strftime("%H:%M:%S")
                    print(f"[{stamp}] URL 变化: {urls}", flush=True)
                    last_urls = urls
                if any("/download" in u for u in urls):
                    reached_download = True
                    print(">>> 已到下载页：手动注册成功。"
                          "请回忆刚才提交验证码后有没有弹二次滑块/九宫格 <<<",
                          flush=True)
                    break
            except Exception as exc:  # noqa: BLE001
                print(f"观察异常: {exc}", flush=True)
            time.sleep(3)
    finally:
        # 收尾才发第一条命令：导回空白页并断开
        try:
            page.goto("about:blank", wait_ms=200)
            page.close()
        except Exception:
            pass
        bb_cdp.close_env(client, launch.env_id)
        print("窗口已关，实验结束。", flush=True)
    print(f"结果: reached_download={reached_download}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())