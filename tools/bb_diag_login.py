"""诊断比特浏览器本地 API：列表原始返回 / 登录态 / 能否建环境。"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import requests  # noqa: E402

from app.register.bitbrowser import BitBrowserClient, build_fingerprint  # noqa: E402


def post(base: str, path: str, body: dict) -> dict:
    try:
        r = requests.post(base + path, json=body, timeout=20)
        return {"status": r.status_code, "json": r.json()}
    except Exception as exc:
        return {"error": f"{type(exc).__name__}: {exc}", "text": ""}


def main() -> int:
    c = BitBrowserClient()
    for path, body in (("/health", {}), ("/browser/list", {"page": 0, "pageSize": 5}),
                       ("/group/list", {"page": 0, "pageSize": 5})):
        out = post(c.base, path, body)
        print(f"{path}: {json.dumps(out, ensure_ascii=False)[:400]}")

    print("\n试建一个环境…")
    try:
        env = c.create_env(name="probe_login_check",
                           fingerprint=build_fingerprint(
                               ua="Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                                  "AppleWebKit/537.36 (KHTML, like Gecko) "
                                  "Chrome/155.0.8059.86 Safari/537.36"),
                           proxy_raw="http://127.0.0.1:7897")
        print("建环境成功:", env.id)
        print("删除:", c.delete_env(env.id))
    except Exception as exc:
        print(f"建环境失败: {type(exc).__name__}: {exc} code={getattr(exc, 'code', '')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
