"""HTTP 会话工厂：默认不继承系统代理。

为什么需要
----------
Windows 上 ``requests`` 会从**注册表**读 IE/WinINET 代理设置。本机装了
代理工具后系统代理指向 ``socks5://...``，于是任何没显式给 ``proxies`` 的
请求都会抛：

    InvalidSchema: Missing dependencies for SOCKS support

而我们的直连请求本来就不该走系统代理。统一用 ``no_env_session()``，
需要代理时显式传 ``proxies=``。
"""

from __future__ import annotations

import requests


def no_env_session() -> requests.Session:
    s = requests.Session()
    s.trust_env = False
    return s


def get(url: str, timeout: float = 30, proxies: dict | None = None,
        headers: dict | None = None) -> requests.Response:
    with no_env_session() as s:
        return s.get(url, timeout=timeout, proxies=proxies, headers=headers)


def post(url: str, timeout: float = 30, proxies: dict | None = None,
         json_body=None, data=None, headers: dict | None = None
         ) -> requests.Response:
    with no_env_session() as s:
        return s.post(url, timeout=timeout, proxies=proxies, json=json_body,
                      data=data, headers=headers)
