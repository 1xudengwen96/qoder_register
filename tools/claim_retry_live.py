"""重启客户端（登录态在）→ 清弹层 → 扫领权益按钮 → dump 各页状态。"""
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / ".pylibs"))

from app.claim.claimer import Claimer  # noqa: E402
from app.claim.ui_login import (claim_client_campaign,  # noqa: E402
                               dismiss_client_overlays)

c = Claimer(client_path="C:/Users/Administrator/AppData/Local/Programs/Qoder/Qoder.exe",
            login_mode="cdp")
print("running:", c.is_running())
if not c.is_running():
    print("launch:", c.launch(remote_debug=True))
time.sleep(12)
print("dismiss:", dismiss_client_overlays())
print("claimed:", claim_client_campaign())
time.sleep(2)
print("dismiss2:", dismiss_client_overlays())
print("claimed2:", claim_client_campaign())