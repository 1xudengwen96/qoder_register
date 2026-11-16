"""关掉比特浏览器的「恢复上次标签页」：每次启动都是干净的。

三步：
1. 环境字段：``url`` 只留一个空白页（原来塞了 about:blank + qoder.com，每次重开）
2. 环境字段：``syncTabs = false``（标签同步关掉）
3. profile 的 Chrome ``Preferences``：``session.restore_on_startup = 4``
   （4 = 打开新标签页，不恢复上次会话），并清空 ``startup_urls``

    python tools/bb_no_restore.py            # 改配置 + 修 Preferences
    python tools/bb_no_restore.py --verify   # 启动并数标签页验证
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import secrets  # noqa: E402
from app.register import cdp_raw  # noqa: E402
from app.register.bitbrowser import BitBrowserClient  # noqa: E402


def find_profile_dir(env_id: str) -> Path | None:
    """profile 在客户端的 ``browser_cache_dir`` 下，目录名就是环境 id。

    默认 ``D:\\bitbrowser-cache``（可从客户端全局配置 ``browser_cache_dir`` 读到）。
    """
    candidates = [Path("D:/bitbrowser-cache") / env_id,
                  Path.home() / "AppData/Local/BitBrowser" / env_id,
                  Path.home() / "AppData/Roaming/BitBrowser" / env_id]
    for c in candidates:
        if (c / "Default" / "Preferences").exists() or c.is_dir():
            return c
    # 兜底：从运行中的 chrome 命令行挖
    try:
        out = subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             "Get-CimInstance Win32_Process -Filter \"Name='chrome.exe'\" | "
             "Select-Object -ExpandProperty CommandLine"],
            capture_output=True, text=True, timeout=30,
            encoding="utf-8", errors="replace").stdout
    except Exception:
        return None
    for line in out.splitlines():
        if "--user-data-dir=" not in line:
            continue
        m = re.search(r'--user-data-dir="?([^" ]+)"?', line)
        if m and env_id in m.group(1):
            return Path(m.group(1))
    return None


def patch_preferences(profile: Path) -> str:
    prefs = profile / "Default" / "Preferences"
    if not prefs.exists():
        prefs = profile / "Preferences"
    if not prefs.exists():
        return f"没找到 Preferences: {prefs}"
    try:
        data = json.loads(prefs.read_text(encoding="utf-8"))
    except Exception as exc:
        return f"Preferences 解析失败: {exc}"
    session = data.setdefault("session", {})
    before = dict(session)
    session["restore_on_startup"] = 4      # 4 = Open the New Tab page
    session["startup_urls"] = []
    prof = data.setdefault("profile", {})
    # 避免「恢复崩溃标签」气泡
    prof.pop("exit_type", None)
    prof["exited_cleanly"] = True
    prefs.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    return f"{prefs}\n    原 session={before}\n    新 session={session}"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--env-id", default=secrets.get("bitbrowser.env_id", ""))
    ap.add_argument("--startup-url", default="about:blank",
                    help="每次启动只开这一个地址")
    ap.add_argument("--verify", action="store_true", help="启动并数标签页")
    ap.add_argument("--workbench", default=None,
                    help='试改 workbench 字段（localserver / "" ），用来关掉工作台标签')
    args = ap.parse_args()
    if not args.env_id:
        print("缺 env id")
        return 2

    client = BitBrowserClient()
    env = client.get_env(args.env_id)
    print(f"环境 {env.id} {env.name} 运行中={env.status}")

    # ---- 1/2) 环境字段
    if env.status == 1:
        client.close_env(env.id)
        time.sleep(3)
    extra = {"url": args.startup_url, "syncTabs": False}
    if args.workbench is not None:
        # PowerShell 传不了空串，用 "none" 表示清空
        extra["workbench"] = "" if args.workbench == "none" else args.workbench
    client.update_env(args.env_id, **extra)
    e2 = client.get_env(args.env_id)
    print(f"PASS url={e2.raw.get('url')!r} syncTabs={e2.raw.get('syncTabs')}")

    # ---- 3) Preferences
    launch = client.open_env(args.env_id)
    time.sleep(6)
    profile = find_profile_dir(args.env_id)
    if profile:
        print(f"profile: {profile}")
        print("PASS " + patch_preferences(profile))
    else:
        print("没定位到 profile 目录（先用 --verify 确认标签数）")

    # ---- 4) 验证
    if args.verify:
        client.close_env(args.env_id)
        time.sleep(4)
        launch = client.open_env(args.env_id)
        time.sleep(8)
        targets = [t for t in cdp_raw.http_targets(launch.cdp_http)
                   if t.get("type") == "page"]
        print(f"\n启动后标签页 {len(targets)} 个:")
        for t in targets:
            print(f"  - {(t.get('url') or '')[:90]}")
        client.close_env(args.env_id)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
