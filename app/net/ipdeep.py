"""IPDEEP V2 开放 API 正确客户端（依据官方文档实测）。

关键修正（之前搞错的地方）
--------------------------
1. **Action 名全小写**
   错：``/openApi/V2/Account/GetBalance``
   对：``/openApi/V2/Account/balance``

2. **业务参数 PascalCase**
   错：``{"proxyType": "dynamicResidential"}``
   对：``{"ProxyType": "dynamicResidential"}``

3. **ResData 是加密字符串**，需要用同一把 AES key 解密：
   Base64 → 前16字节是 IV → AES-CBC 解密 → PKCS7 去填充 → JSON

4. **extractType 不需要传**。认证方式由账号配置决定：
   先调 ``updateDynamicPassword`` 设置密码，之后提取的代理就是账密认证。

已知接口
--------
| 接口 | 说明 | 参数 |
|---|---|---|
| POST /openApi/V2/Account/balance | 账户余额 | 无 |
| POST /openApi/V2/Account/traffic | 流量与静态IP数 | 无 |
| POST /openApi/V2/DynamicProxy/countries | 支持的国家 | ProxyType |
| POST /openApi/V2/DynamicProxy/states | 州/省 | ProxyType, CountryCode |
| POST /openApi/V2/DynamicProxy/cities | 城市 | ProxyType, CountryCode, StateCode |
| POST /openApi/V2/DynamicProxy/updateDynamicPassword | 改动态代理密码 | DynamicPassword |

ProxyType 取值：``dynamicDataCenter`` / ``dynamicResidential`` / ``mobile``
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import os
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

import requests

try:
    from cryptography.hazmat.primitives import padding as sym_padding
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
    _CRYPTO = "cryptography"
except ImportError:
    try:
        from Crypto.Cipher import AES
        _CRYPTO = "pycryptodome"
    except ImportError:
        _CRYPTO = None


DEFAULT_HOST = "https://openapi.ipdeep.com"
API_PREFIX = "/openApi/V2"
DEFAULT_PROXY = "http://127.0.0.1:7897"


class IpdeepError(RuntimeError):
    def __init__(self, msg: str, code: str = "", raw: dict | None = None):
        super().__init__(msg)
        self.code = code
        self.raw = raw or {}


# ============================================================ 数据类
@dataclass(slots=True)
class ProxyInfo:
    host: str = ""
    port: int = 0
    username: str = ""
    password: str = ""
    protocol: str = "http"
    country: str = ""
    state: str = ""
    city: str = ""
    expire_at: str = ""
    raw: dict = field(default_factory=dict)

    @property
    def url(self) -> str:
        if self.username:
            return (f"{self.protocol}://{self.username}:{self.password}"
                    f"@{self.host}:{self.port}")
        return f"{self.protocol}://{self.host}:{self.port}"

    @classmethod
    def from_dict(cls, d: dict) -> "ProxyInfo":
        def pick(*keys, default=""):
            for k in keys:
                v = d.get(k)
                if v not in (None, ""):
                    return v
            return default

        try:
            port = int(pick("Port", "port", default=0) or 0)
        except Exception:
            port = 0
        proto = str(pick("Protocol", "protocol", default="http")).lower()
        return cls(
            host=str(pick("Ip", "IP", "ip", "Host", "host", "Server")),
            port=port,
            username=str(pick("Username", "UserName", "username", "User")),
            password=str(pick("Password", "password", "Pwd")),
            protocol=proto,
            country=str(pick("CountryCode", "countryCode")),
            state=str(pick("StateCode", "stateCode")),
            city=str(pick("CityCode", "cityCode")),
            expire_at=str(pick("ExpireTime", "expireTime", "EndTime")),
            raw=d,
        )


# ============================================================ AES
def _aes_key_candidates(key: str, secret: str) -> list[tuple[str, bytes]]:
    """aesKey = SHA256(key+secret).Substring(0,32)

    Substring(0,32) 在 C# 里取的是**字符串**前 32 个字符。
    SHA256 的 hex 字符串是 64 字符，取前 32 → 32 个 ASCII 字符 = 32 字节 → AES-256。
    但也兼容 digest 前 16 字节（AES-128）。
    """
    h = hashlib.sha256(f"{key}{secret}".encode())
    return [
        ("hex32_utf8", h.hexdigest()[:32].encode()),
        ("hex32_lower", h.hexdigest()[:32].lower().encode()),
        ("digest16", h.digest()[:16]),
    ]


def _aes_decrypt(aes_key: bytes, b64: str) -> bytes:
    raw = base64.b64decode(b64)
    if len(raw) <= 16:
        raise IpdeepError("密文太短")
    iv, ct = raw[:16], raw[16:]
    if _CRYPTO == "cryptography":
        dec = Cipher(algorithms.AES(aes_key), modes.CBC(iv)).decryptor()
        padded = dec.update(ct) + dec.finalize()
        unpadder = sym_padding.PKCS7(128).unpadder()
        return unpadder.update(padded) + unpadder.finalize()
    if _CRYPTO == "pycryptodome":
        padded = AES.new(aes_key, AES.MODE_CBC, iv).decrypt(ct)
        n = padded[-1]
        return padded[:-n] if 0 < n <= 16 else padded
    raise IpdeepError("缺少 AES 库")


def _aes_encrypt(aes_key: bytes, plain: bytes) -> str:
    iv = uuid.uuid4().bytes
    if _CRYPTO == "cryptography":
        padder = sym_padding.PKCS7(128).padder()
        padded = padder.update(plain) + padder.finalize()
        enc = Cipher(algorithms.AES(aes_key), modes.CBC(iv)).encryptor()
        ct = enc.update(padded) + enc.finalize()
    elif _CRYPTO == "pycryptodome":
        n = 16 - len(plain) % 16
        padded = plain + bytes([n]) * n
        ct = AES.new(aes_key, AES.MODE_CBC, iv).encrypt(padded)
    else:
        raise IpdeepError("缺少 AES 库：pip install cryptography")
    return base64.b64encode(iv + ct).decode()


# ============================================================ 客户端
class IpdeepClient:
    def __init__(
        self,
        key: str,
        secret: str,
        host: str = DEFAULT_HOST,
        proxy: str | None = DEFAULT_PROXY,
        timeout: int = 30,
    ) -> None:
        self.key = key
        self.secret = secret
        self.host = host.rstrip("/")
        self.proxy = proxy
        self.timeout = timeout
        self._aes_key = hashlib.sha256(f"{key}{secret}".encode()).hexdigest()[:32].encode()
        self._aes_mode = "hex32_utf8"

    # ---------------------------------------------------------- 签名
    def _sign(self, payload: dict) -> str:
        """签名算法（实测确认）。

        除 sign 外所有顶级字段按名排序，拼 ``key+value``，末尾拼 **secret**，MD5。

        注意：文档写的是 ``OpenApiSignHelper.GenerateSign(parameters, key)``，
        但实测末尾必须拼 **secret** 才能通过校验。
        证伪过程：
          - 末尾拼 key    → 10006 Sign Is Invalid
          - 末尾拼 secret → 10005 Duplicate Request（nonce 检查，说明签名已过）
        """
        parts = []
        for k in sorted(payload):
            if k == "sign":
                continue
            parts.append(f"{k}{payload[k]}")
        return hashlib.md5(("".join(parts) + self.secret).encode()).hexdigest()

    # ---------------------------------------------------------- 调用
    def call(self, controller: str, action: str,
             biz: dict | None = None, decrypt: bool = True,
             retries: int = 2) -> Any:
        """调用接口。``action`` 必须小写。"""
        path = f"{API_PREFIX}/{controller}/{action}"
        biz = biz or {}
        last: Exception | None = None

        for attempt in range(retries + 1):
            data_str = _aes_encrypt(self._aes_key,
                                    json.dumps(biz, separators=(",", ":")).encode())
            payload = {
                "key": self.key,
                "timestamp": int(time.time() * 1000),
                "nonce": str(uuid.uuid4()),
                "data": data_str,
            }
            payload["sign"] = self._sign(payload)

            proxies = {"http": self.proxy, "https": self.proxy} if self.proxy else None
            try:
                r = requests.post(self.host + path, json=payload,
                                  proxies=proxies, timeout=self.timeout)
            except Exception as exc:
                last = exc
                time.sleep(1.5 * (attempt + 1))
                continue

            ct = r.headers.get("Content-Type", "")
            if "json" not in ct:
                raise IpdeepError(
                    f"{path} 非 JSON（status={r.status_code}）：{r.text[:100]}",
                    raw={"status": r.status_code})

            body = r.json()
            code = str(body.get("ErrCode", ""))
            if body.get("Success") is True or code == "0":
                res = body.get("ResData")
                if decrypt:
                    return self._maybe_decrypt(res)
                return res

            msg = body.get("ErrMsg", "")
            if code in ("10007", "10005") and attempt < retries:
                time.sleep(2.0 * (attempt + 1))
                last = IpdeepError(msg, code, body)
                continue
            raise IpdeepError(msg or f"{controller}/{action} 失败", code, body)

        raise IpdeepError(f"重试耗尽：{last}")

    # ---------------------------------------------------------- 解密
    def _maybe_decrypt(self, res: Any) -> Any:
        """ResData 可能是加密串，也可能已是明文对象。"""
        if res is None:
            return None
        if isinstance(res, (dict, list)):
            return res
        if not isinstance(res, str):
            return res

        s = res.strip()
        # 明文 JSON
        if s.startswith("{") or s.startswith("["):
            try:
                return json.loads(s)
            except Exception:
                pass

        # 加密串：依次尝试不同 key 推导方式
        for name, key in _aes_key_candidates(self.key, self.secret):
            try:
                plain = _aes_decrypt(key, s)
                txt = plain.decode("utf-8", "ignore").strip()
                if txt.startswith("{") or txt.startswith("["):
                    return json.loads(txt)
                if txt:
                    self._aes_key = key
                    self._aes_mode = name
                    return txt
            except Exception:
                continue
        # 不是加密串，原样返回
        return s

    # ---------------------------------------------------------- 账户
    def balance(self) -> Any:
        return self.call("Account", "balance")

    def traffic(self) -> Any:
        return self.call("Account", "traffic")

    # ---------------------------------------------------------- 地理
    def countries(self, proxy_type: str = "dynamicResidential") -> Any:
        return self.call("DynamicProxy", "countries", {"ProxyType": proxy_type})

    def states(self, country_code: str,
               proxy_type: str = "dynamicResidential") -> Any:
        return self.call("DynamicProxy", "states",
                         {"ProxyType": proxy_type, "CountryCode": country_code})

    def cities(self, country_code: str, state_code: str,
               proxy_type: str = "dynamicResidential") -> Any:
        return self.call("DynamicProxy", "cities", {
            "ProxyType": proxy_type, "CountryCode": country_code,
            "StateCode": state_code,
        })

    # ---------------------------------------------------------- 代理密码
    def update_dynamic_password(self, password: str) -> Any:
        """设置动态代理密码。设了之后提取的代理就是账密认证。"""
        return self.call("DynamicProxy", "updateDynamicPassword",
                         {"DynamicPassword": password})
