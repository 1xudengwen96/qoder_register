"""把 IPDEEP 提取链接的代理写进 data/proxies.txt（保留已有未用条目）。"""

from __future__ import annotations

import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import secrets  # noqa: E402
from app.net.http import no_env_session  # noqa: E402


def main() -> int:
    url = secrets.get("ipdeep.extract_url", "")
    if not url:
        print("secrets 里没有 ipdeep.extract_url")
        return 2
    with no_env_session() as s:
        r = s.get(url, timeout=30)
    r.raise_for_status()
    lines = [l.strip().replace("sessiontime-5", "sessiontime-30")
             for l in r.text.splitlines() if l.strip()]
    print(f"链接返回 {len(lines)} 条")

    path = ROOT / "data" / "proxies.txt"
    keep = []   # 每次整批替换（旧 session 已过期）
    merged = list(dict.fromkeys(keep + lines))
    header = ["# IPDEEP 代理（一条一用）",
              "# 格式：host:port:user:pass 或 socks5://user:pass@host:port"]
    path.write_text("\n".join(header + merged) + "\n", encoding="utf-8")
    print(f"写入 {path}: 共 {len(merged)} 条（新增 {len(merged) - len(keep)}）")
    for line in lines[:3]:
        print("  " + line[:100])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
