"""比特浏览器 + 纯 CDP 的注册开场动作。

职责只有一件事：**把代理自动写进比特环境，然后把环境拉起来，交回 CDP 端点**。

为什么单独一层
--------------
1. 代理来源是本地文件（``host:port:user:pass`` 四段冒号，代理商导出的原样），
   需要在开环境前解析成 ``Proxy``，再翻成比特的 12 个代理字段；
2. 复用同名环境时**必须重写代理**，否则窗口里还是上一轮的出口 IP，
   注册侧一眼就能看出"同一设备不同国家"；
3. 启动后要用 ``/json/version`` 做真探测，不能只信 API 返回的 http 字段；
4. 全程不用 Playwright（本机沙箱里它连 node 驱动都起不来）。

用法
----
    from app.register.bitbrowser import BitBrowserClient
    from app.register.bb_cdp import open_for_register

    client = BitBrowserClient()
    launch = open_for_register(client, proxy=px, region="us", account_tag="acct12")
    # launch.endpoint → "http://127.0.0.1:58123"
"""

from __future__ import annotations

import dataclasses
import logging
import time
from typing import Any

from app.net.proxy import Proxy

log = logging.getLogger("bb-cdp")


@dataclasses.dataclass(slots=True)
class BBLaunch:
    """一次成功启动后的连接信息。"""

    env_id: str
    env_name: str
    endpoint: str
    created: bool
    pid: int = 0
    core_version: str = ""
    proxy_label: str = ""
    proxy_raw: str = ""

    def as_dict(self) -> dict:
        return {
            "env_id": self.env_id,
            "env_name": self.env_name,
            "endpoint": self.endpoint,
            "created": self.created,
            "pid": self.pid,
            "core_version": self.core_version,
            "proxy": self.proxy_label,
        }


def _fingerprint_for(region: str, account_tag: str) -> dict:
    """按地区画像造指纹对象（时区/语言必须跟出口 IP 对齐）。"""
    from app.register.bitbrowser import build_fingerprint
    from app.register.fingerprint_engine import make_profile

    prof = make_profile(region=region, seed=account_tag or None)
    return build_fingerprint(
        ua=prof.user_agent,
        screen_w=prof.viewport_w,
        screen_h=prof.viewport_h,
        timezone=prof.timezone,
        languages=[prof.locale, "en"],
        os_type=prof.platform,
        hardware_concurrency=prof.hardware_concurrency,
        device_memory=prof.device_memory,
        fonts=prof.fonts,
    )


def find_env_id(client, name: str) -> str | None:
    """按名字精确找环境 id。"""
    if not name:
        return None
    try:
        items, _ = client.list_envs(page=0, page_size=100, name=name)
    except Exception as exc:  # noqa: BLE001
        log.warning("列环境失败：%s", exc)
        return None
    for e in items:
        if e.name == name:
            return e.id
    return None


def ensure_env(
    client,
    *,
    name: str,
    region: str = "us",
    proxy: Proxy | None = None,
    group_id: str | None = None,
    reuse_env: bool = True,
    force_new: bool = False,
) -> tuple[str, bool]:
    """确保存在一个带正确代理的环境，返回 ``(env_id, created)``。"""
    from app.register.bitbrowser import build_proxy_config

    proxy_fields = build_proxy_config(proxy)
    env_id = None if force_new else (find_env_id(client, name) if reuse_env else None)

    if env_id:
        # 复用：指纹保持身份稳定，代理每次都重写
        client.update_env(env_id, **proxy_fields)
        log.info("复用环境 %s（%s），代理已重写为 %s", name, env_id,
                 proxy.label if proxy else "直连")
        return env_id, False

    fp = _fingerprint_for(region, name)
    body: dict[str, Any] = {
        "name": name,
        "remark": "qoder_register/cdp",
        "browserFingerPrint": fp,
        **proxy_fields,
    }
    if group_id:
        body["groupIds"] = [group_id]
    data = client.call("/browser/update", body, timeout=90).get("data") or {}
    new_id = data.get("id") or ""
    if not new_id:
        raise RuntimeError(f"比特浏览器建环境失败：{data}")
    log.info("新建环境 %s -> %s，代理 %s", name, new_id,
             proxy.label if proxy else "直连")
    return new_id, True


def launch_env(client, env_id: str, *, headless: bool = False,
               wait_cdp: float = 25.0) -> tuple[str, Any]:
    """启动环境并等到 CDP 端点真的可用，返回 ``(endpoint, LaunchResult)``。"""
    from app.register.bitbrowser import LaunchRestrictedError

    try:
        res = client.open_env(env_id, headless=headless)
    except LaunchRestrictedError:
        raise
    endpoint = res.cdp_http
    deadline = time.time() + wait_cdp
    while time.time() < deadline:
        if client.probe_cdp(endpoint) is not None:
            return endpoint, res
        # 有的客户端返回的 http 端点要等一两秒才监听
        time.sleep(0.6)
    raise RuntimeError(f"比特浏览器已返回 CDP {endpoint}，但 /json/version 探测不通")


def open_for_register(
    client,
    *,
    proxy: Proxy | None,
    region: str = "us",
    account_tag: str = "",
    env_name: str = "",
    env_id: str = "",
    group_id: str | None = None,
    reuse_env: bool = True,
    force_new: bool = False,
    headless: bool = False,
    wait_cdp: float = 25.0,
) -> BBLaunch:
    """注册前的一步到位：定位/新建环境 → 写入代理 → 启动 → 校验 CDP。

    传了 ``env_id`` 就直接用那个环境（只重写代理），否则按 ``env_name``/``account_tag`` 找。
    """
    from app.register.bitbrowser import build_proxy_config

    created = False
    if env_id:
        client.update_env(env_id, **build_proxy_config(proxy))
        name = env_name or (client.get_env(env_id).name or env_id)
        log.info("指定环境 %s(%s)，代理已重写为 %s", name, env_id,
                 proxy.label if proxy else "直连")
    else:
        name = env_name or account_tag or f"qoder_{region}_{int(time.time())}"
        env_id, created = ensure_env(
            client, name=name, region=region, proxy=proxy,
            group_id=group_id, reuse_env=reuse_env, force_new=force_new,
        )

    endpoint, res = launch_env(client, env_id, headless=headless, wait_cdp=wait_cdp)
    return BBLaunch(
        env_id=env_id,
        env_name=name,
        endpoint=endpoint,
        created=created,
        pid=res.pid,
        core_version=res.core_version,
        proxy_label=(proxy.label if proxy else "直连"),
        proxy_raw=(proxy.raw if proxy else ""),
    )


def close_env(client, env_id: str) -> bool:
    try:
        return client.close_env(env_id)
    except Exception:  # noqa: BLE001
        return False
