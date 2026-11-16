"""看客户端本地 token 归属，以及客户端是否开了 CDP（用于自动点登录）。"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.claim.token_harvester import (  # noqa: E402
    client_status,
    harvest_token,
    qoder_data_dir,
)


def mask(s: str) -> str:
    return s[:12] + "..." + s[-6:] if len(s) > 24 else "***"


def main() -> int:
    d = qoder_data_dir()
    print(f"数据目录: {d}  存在={d.exists()}")

    mt = d / "SharedClientCache" / "cache" / "machine_token.json"
    print(f"\nmachine_token.json 存在={mt.exists()}")
    if mt.exists():
        try:
            raw = json.loads(mt.read_text(encoding="utf-8"))
            safe = {}
            for k, v in raw.items():
                safe[k] = mask(v) if isinstance(v, str) and len(v) > 24 else v
            print(json.dumps(safe, ensure_ascii=False, indent=2)[:900])
        except Exception as exc:
            print(f"  解析失败: {exc}")

    try:
        tok = harvest_token()
        if tok:
            print(f"\nharvest: token={mask(tok.token)} source={tok.source} "
                  f"device={tok.device_id} type={tok.token_type}")
        else:
            print("\nharvest: 没有 token")
    except Exception as exc:
        print(f"\nharvest 失败: {type(exc).__name__}: {exc}")

    try:
        print("\nclient_status:", json.dumps(client_status(), ensure_ascii=False)[:400])
    except Exception as exc:
        print(f"client_status 失败: {exc}")

    dev = d / "DevToolsActivePort"
    print(f"\nDevToolsActivePort 存在={dev.exists()}")
    if dev.exists():
        print("  内容:", dev.read_text(encoding="utf-8", errors="replace").strip()[:60])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
