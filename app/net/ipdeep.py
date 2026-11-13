"""IPDEEP V2 account/region transport.

Routes and PascalCase business fields follow the supplied official documentation.
The documentation excerpt does NOT specify GenerateSign canonicalization, IV
serialization, or a response test vector. Earlier MD5/IV-prefix guesses were
not verified: Duplicate Request does not prove authentication passed.

A verified official signer and codec must be supplied explicitly; without them
requests fail locally rather than probing credentials or reporting false success.
ProxyInfo also provides an independent, explicit proxy connectivity check.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import os
import time
import urllib.parse
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

    # ---------------------------------------------------------- 提取代理
    EXTRACT_ACTIONS = ("extractDynamicIP", "extract", "extractIps",
                       "getDynamicIp", "generateDynamicIp")

    def extract_proxies(self, *, proxy_type: str = "dynamicResidential",
                        country: str = "", state: str = "", city: str = "",
                        num: int = 1, protocol: str = "http",
                        duration: int = 30, rotation: str = "stickiness",
                        fmt: str = "info") -> list[ProxyInfo]:
        """提取动态代理，返回可用代理列表。

        接口名与业务字段在官方文档摘录里没有明写，这里按「动作名 × 字段命名风格」
        依次试探，命中即返回；全失败时把每次的错误码一起抛出，便于定位。
        """
        base = {
            "ProxyType": proxy_type, "Num": int(num), "Protocol": protocol,
            "Duration": int(duration), "RotationType": rotation,
            "Format": fmt,
        }
        for key, val in (("CountryCode", country), ("StateCode", state),
                         ("CityCode", city)):
            if val:
                base[key] = val

        camel = _to_camel(base)
        attempts: list[tuple[str, dict]] = []
        for action in self.EXTRACT_ACTIONS:
            attempts.append((action, dict(base)))
            attempts.append((action, dict(camel)))

        errors: list[str] = []
        for action, biz in attempts:
            try:
                res = self.call("DynamicProxy", action, biz)
            except IpdeepError as exc:
                errors.append(f"{action}/{','.join(sorted(biz))[:40]}: "
                              f"{exc.code or '?'} {exc}")
                continue
            proxies = parse_proxy_payload(res)
            if proxies:
                return proxies
            errors.append(f"{action}: 返回里解析不出代理 {str(res)[:80]}")
        raise IpdeepError("提取失败，尝试过的组合：\n" + "\n".join(errors))


def _to_camel(d: dict) -> dict:
    """PascalCase → camelCase（有些接口按 DTO 序列化成小驼峰）。"""
    out = {}
    for k, v in d.items():
        out[k[:1].lower() + k[1:]] = v
    return out


def parse_proxy_payload(payload: Any) -> list[ProxyInfo]:
    """把提取结果解析成 ``ProxyInfo`` 列表。

    兼容三种形态：
    1. ``[{"Ip": "...", "Port": 8080, "Username": "...", "Password": "..."}]``
    2. ``{"list": [...]}`` / ``{"data": [...]}``
    3. 纯文本 ``ip:port:user:pass`` 或 ``user:pass@ip:port``，一行一个
    """
    if payload is None:
        return []
    if isinstance(payload, str):
        text = payload.strip()
        if text.startswith("{") or text.startswith("["):
            try:
                return parse_proxy_payload(json.loads(text))
            except Exception:
                return _parse_lines(text)
        return _parse_lines(text)
    if isinstance(payload, dict):
        for key in ("list", "data", "List", "Data", "proxies", "Proxies",
                    "items", "result"):
            if isinstance(payload.get(key), (list, dict)):
                got = parse_proxy_payload(payload[key])
                if got:
                    return got
        info = ProxyInfo.from_dict(payload)
        return [info] if info.host and info.port else []
    if isinstance(payload, list):
        out: list[ProxyInfo] = []
        for item in payload:
            if isinstance(item, dict):
                got = parse_proxy_payload(item)
                out.extend(got)
            elif isinstance(item, str):
                out.extend(_parse_lines(item))
        return out
    return []


def _parse_lines(text: str) -> list[ProxyInfo]:
    out: list[ProxyInfo] = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        try:
            if "@" in line:
                url = line if "://" in line else "http://" + line
                u = urllib.parse.urlparse(url)
                if u.hostname and u.port:
                    out.append(ProxyInfo(
                        host=u.hostname, port=int(u.port),
                        username=urllib.parse.unquote(u.username or ""),
                        password=urllib.parse.unquote(u.password or "")))
                continue
            parts = line.split(":")
            if len(parts) == 4:
                out.append(ProxyInfo(host=parts[0], port=int(parts[1]),
                                     username=parts[2], password=parts[3]))
            elif len(parts) == 2:
                out.append(ProxyInfo(host=parts[0], port=int(parts[1])))
        except (ValueError, TypeError):
            continue
    return out

