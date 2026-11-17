"""用全新 user-data-dir 启动干净 Qoder 客户端（9223），测新人 300 积分触发。

- 不动现有 9222 客户端（保留现场）
- 新 data-dir = 全新 machine_id / telemetry / 安装状态
- 登录后等几分钟读 usage/profile 看是否有 Pro Trial / 300 Credits
"""
from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

EXE = r"C:\Users\Administrator\AppData\Local\Programs\Qoder\Qoder.exe"
DATA = str(ROOT / "data" / "qoder_client_fresh")
PORT = 9223


def main() -> int:
    Path(DATA).mkdir(parents=True, exist_ok=True)
    proc = subprocess.Popen(
        [EXE, f"--user-data-dir={DATA}",
         f"--remote-debugging-port={PORT}"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    print(f"pid={proc.pid} data-dir={DATA} port={PORT}")

    from app.register import cdp_raw

    for _ in range(40):
        time.sleep(1.5)
        try:
            if cdp_raw.http_json(PORT, "/json/version"):
                print("CDP 9223 就绪")
                break
        except Exception:
            continue
    else:
        print("9223 没起来")
        return 1

    for t in cdp_raw.http_targets(f"127.0.0.1:{PORT}"):
        if t.get("type") == "page":
            print(f"page: {(t.get('url') or '')[:90]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
