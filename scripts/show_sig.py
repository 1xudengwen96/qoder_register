"""打印 ui_login.drive_login 的签名与参数说明。"""

from __future__ import annotations

import inspect
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.claim import ui_login  # noqa: E402


def main() -> int:
    fn = ui_login.drive_login
    print("签名:", inspect.signature(fn))
    src = inspect.getsource(fn)
    print("\n开头 60 行:")
    for line in src.splitlines()[:60]:
        print("  " + line[:110])
    # 有没有 env_id / 复用环境的入口
    for kw in ("env_id", "ensure_env", "open_for_register", "env_name"):
        hits = [m.start() for m in re.finditer(kw, inspect.getsource(ui_login))]
        print(f"\n{kw}: {len(hits)} 处")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
