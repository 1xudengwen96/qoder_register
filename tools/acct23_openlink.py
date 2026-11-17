"""acct23 一一对应实测：在注册环境里打开客户端登录链接。

- 用注册时的比特浏览器环境 acct23（同指纹同代理同 IP，注册 cookie 还在）
- 不重写代理、不动指纹：client.open_env 只启动，不 update
- 页面状态机复用 ui_login 的读取逻辑，抓到 qoder:// 回跳就 dispatch_deep_link
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.register.bitbrowser import BitBrowserClient  # noqa: E402
from app.register.cdp_page import new_page  # noqa: E402
from app.claim.ui_login import _state, _click_text, dispatch_deep_link  # noqa: E402

LINK = (ROOT / "data" / "clipboard_link.txt").read_text(
    encoding="utf-8-sig").strip()
OUT = ROOT / "data" / "acct23_claim_result.json"


def main() -> int:
    client = BitBrowserClient()
    env = next((e for e in client.list_all_envs() if e.name == "acct23"), None)
    if env is None:
        print("找不到 acct23 环境")
        return 2
    print(f"env={env.id} name={env.name} status={env.status} "
          f"proxy={env.host}:{env.port} lastIp={env.raw.get('lastIp')}")
    launch = client.open_env(env.id)
    endpoint = launch.cdp_http
    print(f"CDP={endpoint}")

    page = new_page(endpoint, "about:blank")
    page.goto(LINK, wait_ms=6000)

    result = {"deep_link": "", "final_url": "", "steps": []}
    deadline = time.time() + 240
    last_text = ""
    while time.time() < deadline:
        st = _state(page)
        url = str(st.get("url") or "")
        text = str(st.get("text") or "")[:200].replace("\n", " | ")
        deep = str(st.get("deepLink") or "")
        if deep:
            result["deep_link"] = deep
            result["final_url"] = url
            print(f"[deep-link] {deep[:120]}")
            break
        if text != last_text:
            last_text = text
            print(f"[page] {url[:80]} :: {text[:160]}")
            result["steps"].append(f"{url[:80]} :: {text[:160]}")
        if url and "users/sign-in" not in url and "selectAccounts" not in url:
            result["final_url"] = url
            print(f"[left-login] {url[:100]}")
            break
        if "selectAccounts" in url:
            # 语言无关：主按钮（繼續/Continue/继续）都是 ant-btn-primary，
            # CDP eval 返回中文是 mojibake，文本比对不可靠 → 原生 click
            clicked = page.eval(
                "(() => { const b = document.querySelector("
                "'button.ant-btn-primary');"
                " if (!b) return false; b.click(); return true; })()")
            print(f"[selectAccounts] js-click ant-btn-primary -> {clicked}")
            time.sleep(3)
            continue
        time.sleep(2)

    OUT.write_text(json.dumps(result, ensure_ascii=False, indent=2),
                   encoding="utf-8")
    if result["deep_link"]:
        ok = dispatch_deep_link(result["deep_link"])
        print(f"dispatch_deep_link -> {ok}")
        return 0
    print("没抓到回跳链接")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
