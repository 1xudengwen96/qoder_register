"""本机密钥与运行参数加载（config/secrets.yaml）。

优先级：**环境变量 > secrets.yaml > 内置默认**。
这样 CI/临时覆盖用环境变量，日常跑直接读文件，不用每次 set。

用法
----
    from app import secrets

    secrets.get("bitbrowser.env_id")                 # 取配置
    secrets.env_or("captcha.twocaptcha_key",
                   "TWOCAPTCHA_KEY")                 # 环境变量优先
    secrets.export_env()                             # 把已知密钥灌进 os.environ
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import yaml

DEFAULT_PATH = Path("config/secrets.yaml")

# 配置项 → 环境变量名（env_or / export_env 用同一张表）
ENV_MAP = {
    "captcha.twocaptcha_key": "TWOCAPTCHA_KEY",
    "ipdeep.key": "IPDEEP_KEY",
    "ipdeep.secret": "IPDEEP_SECRET",
    "mail.outlook_file": "QODER_MAIL_FILE",
    "bitbrowser.env_id": "BITBROWSER_ENV_ID",
}

_cache: dict | None = None
_cache_path: str = ""


def load(path: str | Path | None = None, reload: bool = False) -> dict:
    global _cache, _cache_path
    p = Path(path) if path else DEFAULT_PATH
    if not p.exists():
        return {}
    key = str(p.resolve())
    if _cache is not None and _cache_path == key and not reload:
        return _cache
    try:
        data = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    except Exception:
        data = {}
    _cache = data if isinstance(data, dict) else {}
    _cache_path = key
    return _cache


def get(dotted: str, default: Any = None) -> Any:
    node: Any = load()
    for part in dotted.split("."):
        if isinstance(node, dict) and part in node:
            node = node[part]
        else:
            return default
    return default if node in (None, "") else node


def env_or(dotted: str, env_name: str | None = None,
           default: Any = "") -> Any:
    """环境变量优先，其次 secrets.yaml，最后 default。"""
    name = env_name or ENV_MAP.get(dotted, "")
    if name:
        val = os.environ.get(name)
        if val:
            return val
    val = get(dotted)
    return default if val in (None, "") else val


def export_env() -> list[str]:
    """把 secrets 里的密钥灌进 os.environ（不覆盖已有值），返回灌了哪些。"""
    touched: list[str] = []
    for dotted, name in ENV_MAP.items():
        if os.environ.get(name):
            continue
        val = get(dotted)
        if val in (None, ""):
            continue
        os.environ[name] = str(val)
        touched.append(name)
    return touched


def describe() -> str:
    """给 CLI 打印用：列出已配置项（密钥打码）。"""
    data = load()
    lines: list[str] = [f"配置文件: {_cache_path or DEFAULT_PATH}"]
    for dotted in ENV_MAP:
        val = get(dotted)
        if val in (None, ""):
            lines.append(f"  {dotted:32s} (空)")
        else:
            s = str(val)
            masked = s[:6] + "*" * max(0, len(s) - 10) + s[-4:] if len(s) > 12 else "***"
            lines.append(f"  {dotted:32s} {masked}")
    if not data:
        lines.append("  (文件不存在或为空)")
    return "\n".join(lines)
