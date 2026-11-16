"""去掉 UTF-8 BOM（PowerShell Set-Content 会加上）。"""

from __future__ import annotations

import pathlib
import sys

targets = sys.argv[1:] or [
    "app/mail/relay_qq.py",
    "config/config.yaml",
    "config/config.example.yaml",
]
for p in targets:
    path = pathlib.Path(p)
    data = path.read_bytes()
    if data.startswith(b"\xef\xbb\xbf"):
        path.write_bytes(data[3:])
        print("stripped", p)
    else:
        print("clean", p)
