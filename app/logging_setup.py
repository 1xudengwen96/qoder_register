"""统一日志：控制台 + 按天滚动文件。"""

from __future__ import annotations

import logging
import sys
from logging.handlers import TimedRotatingFileHandler
from pathlib import Path


def setup(level: str = "INFO", log_dir: str | Path = "data/logs", name: str = "qoder_register") -> logging.Logger:
    d = Path(log_dir)
    d.mkdir(parents=True, exist_ok=True)

    logger = logging.getLogger(name)
    logger.setLevel(getattr(logging, level.upper(), logging.INFO))
    if logger.handlers:
        return logger

    fmt = logging.Formatter(
        "%(asctime)s %(levelname)-7s %(name)s | %(message)s", "%Y-%m-%d %H:%M:%S"
    )

    sh = logging.StreamHandler(sys.stdout)
    sh.setFormatter(fmt)
    logger.addHandler(sh)

    fh = TimedRotatingFileHandler(d / "run.log", when="midnight",
                                  backupCount=14, encoding="utf-8")
    fh.setFormatter(fmt)
    logger.addHandler(fh)
    return logger
