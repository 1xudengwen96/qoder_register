"""列出某个 CDP 端口上的全部目标（不过滤）。"""

from __future__ import annotations

import json
import sys
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def main() -> int:
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 9222
    with urllib.request.urlopen(f"http://127.0.0.1:{port}/json/list",
                               timeout=6) as r:
        data = json.loads(r.read().decode("utf-8", "replace"))
    print(f"{len(data)} 个目标:")
    for t in data:
        print(f"  {t.get('type'):10s} {(t.get('url') or '(空)')[:100]}")
        print(f"           title={t.get('title')!r}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
