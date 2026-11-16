"""登录后在客户端数据目录里找 token 的真实落点。"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
DATA = Path.home() / "AppData/Roaming/Qoder"


def main() -> int:
    now = time.time()
    print(f"扫描 {DATA}")
    hits: list[tuple[float, int, Path]] = []
    for dirpath, dirnames, filenames in os.walk(DATA):
        # 跳过体积大的缓存目录
        dirnames[:] = [d for d in dirnames
                       if d not in ("Cache", "Code Cache", "GPUCache",
                                    "blob_storage", "Session Storage",
                                    "Local Storage", "logs", "Crashpad")]
        for name in filenames:
            low = name.lower()
            if ("token" in low or "auth" in low or "session" in low
                    or low in ("storage.json", "state.vscdb")):
                p = Path(dirpath) / name
                try:
                    st = p.stat()
                except OSError:
                    continue
                hits.append((st.st_mtime, st.st_size, p))
    hits.sort(reverse=True)
    print(f"命中 {len(hits)} 个文件（按修改时间）:")
    for mt, size, p in hits[:25]:
        age = (now - mt) / 60
        print(f"  {age:7.1f} 分钟前  {size:>9}B  {p.relative_to(DATA)}")

    # 看看 storage.json 里有没有 token 类键
    st = DATA / "User/globalStorage/storage.json"
    if st.exists():
        try:
            data = json.loads(st.read_text(encoding="utf-8"))
            keys = [k for k in data if any(t in k.lower()
                                           for t in ("token", "auth", "user", "session"))]
            print(f"\nstorage.json 里可疑键 {len(keys)} 个:")
            for k in keys[:20]:
                v = data[k]
                s = json.dumps(v, ensure_ascii=False)
                print(f"  {k} = {s[:80]}")
        except Exception as exc:
            print("storage.json 解析失败:", exc)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
