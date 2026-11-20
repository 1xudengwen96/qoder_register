"""窗口池真实环境验收：重配指纹生效 + 残留清零 + 收尾关窗。

用独立 qprobe 前缀 + 临时账本（不碰生产 qpool 账本），
验收完删除探针环境，不留痕迹。

跑法：
    $env:PYTHONPATH='.;.pylibs'
    python tools/probe_bb_pool.py
"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path

from app.config import get, load_config
from app.register.bb_pool import EnvPool
from app.register.bitbrowser import BitBrowserClient
from app.register.cdp_page import new_page

FP_JS = ("JSON.stringify({hc:navigator.hardwareConcurrency,"
         "w:screen.width,h:screen.height,dm:navigator.deviceMemory,"
         "lang:navigator.language,tz:Intl.DateTimeFormat()"
         ".resolvedOptions().timeZone})")


def main() -> int:
    cfg = load_config()
    client = BitBrowserClient(get(cfg, "browser.bitbrowser.api_base",
                                  "http://127.0.0.1:54345"))
    assert client.health(), "比特浏览器服务没启动"
    tmp = Path(tempfile.mkdtemp(prefix="bbprobe_"))
    pool = EnvPool(client, size=1, prefix="qprobe",
                   ledger_path=tmp / "l.json", accounts_path=tmp / "a.json")

    # ① 第一次使用：注入残留（cookie + localStorage + sessionStorage）
    l1 = pool.acquire("probeA", region="us", proxy=None)
    print(f"① 首用窗口 {l1.env_name} cdp={l1.endpoint}")
    page = new_page(l1.endpoint, "about:blank")
    page.goto("https://qoder.com/", wait_ms=6000)
    page.cdp.call("Network.setCookie", {
        "name": "pool_residue", "value": "1", "domain": ".qoder.com",
        "path": "/", "url": "https://qoder.com/"})
    page.eval("localStorage.setItem('pool_residue','1');"
              "sessionStorage.setItem('pool_residue','1');1")
    fp1 = page.eval(FP_JS)
    cookies = page.cdp.call("Network.getCookies",
                            {"urls": ["https://qoder.com/"]}).get("cookies", [])
    assert any(c["name"] == "pool_residue" for c in cookies), "残留注入失败"
    print(f"   残留已注入：cookie + 本地存储；指纹 {fp1}")
    page.close()
    pool.release(l1.env_id)
    client.close_env(l1.env_id)

    # ② 第二次使用：size=1 必回同一窗口，指纹必须换了一套
    l2 = pool.acquire("probeB", region="sg", proxy=None)
    assert l2.env_id == l1.env_id, "size=1 应复用同一窗口"
    page2 = new_page(l2.endpoint, "about:blank")
    page2.goto("https://qoder.com/", wait_ms=9000)
    fp2 = page2.eval(FP_JS)
    print(f"② 复用同窗 {l2.env_name}；新指纹 {fp2}")
    d1, d2 = json.loads(fp1), json.loads(fp2)
    diff = [k for k in d1 if d1[k] != d2.get(k)]
    assert diff, f"指纹没变化：{fp1} == {fp2}"
    print(f"   指纹已重配，差异字段：{diff}")

    # ③ 残留必须清零（复用的前提）
    cookies2 = page2.cdp.call("Network.getCookies",
                              {"urls": ["https://qoder.com/"]}).get("cookies", [])
    assert not any(c["name"] == "pool_residue" for c in cookies2), \
        "cookie 残留没清掉"
    v = page2.eval("localStorage.getItem('pool_residue')")
    assert v in (None, "", "null"), f"localStorage 残留：{v}"
    print("③ 残留清零：cookie / localStorage 均已清")
    page2.close()

    # ④ 进程收尾统一关窗
    closed = pool.close_all()
    assert closed >= 1
    assert not client.get_env(l2.env_id).status, "收尾后窗口仍在运行"
    print(f"④ 收尾关窗 OK（{closed} 个）")

    client.delete_env(l2.env_id)
    print("探针环境已删除，真实环境验收全部通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())