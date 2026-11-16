"""打印 IPDEEP 提取链接的原始返回，并检查比特浏览器是否在线。"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import secrets  # noqa: E402
from app.net.http import no_env_session  # noqa: E402
from app.register.bitbrowser import BitBrowserClient  # noqa: E402


def main() -> int:
    url = secrets.get("ipdeep.extract_url", "")
    print(f"提取链接: {url[:90]}")
    if url:
        with no_env_session() as s:
            r = s.get(url, timeout=30)
        print(f"HTTP {r.status_code}  长度 {len(r.text)}")
        for line in r.text.splitlines()[:12]:
            print("  " + line[:120])

    print("\n=== 比特浏览器 ===")
    c = BitBrowserClient()
    print("端口监听:", c.is_port_open())
    print("健康:", c.health())
    try:
        envs, total = c.list_envs()
        print(f"环境 {total} 个:")
        for e in envs:
            print(f"  {e.id} status={e.status} {e.name} "
                  f"proxy={e.proxy_method} {e.host}:{e.port}")
    except Exception as exc:
        print(f"列环境失败: {type(exc).__name__}: {exc}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
