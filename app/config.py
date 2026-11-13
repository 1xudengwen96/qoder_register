"""统一配置加载：YAML 文件 + 环境变量插值 + 强类型访问。

设计要点：
- 所有密钥用 ``env:VAR_NAME`` 形式引用，禁止明文写进配置文件；
- 配置文件缺失时回落到 ``config/config.example.yaml``，保证 CLI 首次可跑；
- 用 SimpleNamespace 递归包装，支持 ``cfg.mail.outlook_tag.max_tags_per_account`` 点号访问。
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import yaml

DEFAULT_CONFIG_PATH = Path("config/config.yaml")
EXAMPLE_CONFIG_PATH = Path("config/config.example.yaml")

_ENV_PATTERN = re.compile(r"^env:([A-Za-z_][A-Za-z0-9_]*)$")


class ConfigError(RuntimeError):
    """配置缺失或格式错误。"""


def _interpolate(value: Any) -> Any:
    """把 ``env:XXX`` 替换成真实环境变量值；缺失时留空串并在 stderr 侧提示。"""
    if isinstance(value, dict):
        return {k: _interpolate(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_interpolate(v) for v in value]
    if isinstance(value, str):
        m = _ENV_PATTERN.match(value.strip())
        if m:
            return os.environ.get(m.group(1), "")
    return value


def _to_namespace(value: Any) -> Any:
    if isinstance(value, dict):
        return SimpleNamespace(**{k: _to_namespace(v) for k, v in value.items()})
    if isinstance(value, list):
        return [_to_namespace(v) for v in value]
    return value


def to_plain(value: Any) -> Any:
    """把 SimpleNamespace 递归还原成 dict/list，供需要下标访问的旧接口使用。"""
    if isinstance(value, SimpleNamespace):
        return {k: to_plain(v) for k, v in vars(value).items() if not k.startswith("_")}
    if isinstance(value, dict):
        return {k: to_plain(v) for k, v in value.items()}
    if isinstance(value, list):
        return [to_plain(v) for v in value]
    return value


def load_config(path: str | Path | None = None) -> SimpleNamespace:
    """加载配置。

    优先级：显式 path > config/config.yaml > config/config.example.yaml
    """
    candidates: list[Path] = []
    if path:
        candidates.append(Path(path))
    candidates.extend([DEFAULT_CONFIG_PATH, EXAMPLE_CONFIG_PATH])

    chosen: Path | None = None
    for c in candidates:
        if c.exists():
            chosen = c
            break
    if chosen is None:
        raise ConfigError(
            "找不到配置文件：请先复制 config/config.example.yaml 为 config/config.yaml"
        )

    raw = yaml.safe_load(chosen.read_text(encoding="utf-8")) or {}
    cfg = _to_namespace(_interpolate(raw))
    cfg.__dict__["_source_path"] = str(chosen)
    return cfg


def get(cfg: SimpleNamespace, dotted: str, default: Any = None) -> Any:
    """安全取嵌套配置，例如 get(cfg, "mail.strategy", "weighted")。"""
    node: Any = cfg
    for part in dotted.split("."):
        if isinstance(node, SimpleNamespace) and hasattr(node, part):
            node = getattr(node, part)
        elif isinstance(node, dict) and part in node:
            node = node[part]
        else:
            return default
    return node
