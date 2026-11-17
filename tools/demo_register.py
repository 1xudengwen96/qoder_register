"""端到端演示：新建 BitBrowser env → 配代理 → 打开浏览器 → 注册 → OTP → 入库。

只跑注册段（orchestrator.register_only），不碰 Qoder 客户端、不碰 device flow。
注册全程在新建的 BitBrowser env 里完成，env 注册完不关（keep_open=True）方便观察。

消耗：1 个 icloud_relay 邮箱地址 + 2Captcha 余额（几美分）+ 代理流量。

用法：
    python tools/demo_register.py
    python tools/demo_register.py --region us
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import secrets  # noqa: E402
from app.config import get, load_config  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--region", default="")
    args = ap.parse_args()

    cfg = load_config()
    region = (args.region or get(cfg, "region", "sg")).lower()
    secrets.export_env()

    # 覆盖三个关键开关：强制新建 env、不复用同名、注册完不关
    cfg.browser.bitbrowser.env_id = ""           # 留空 → 新建
    cfg.browser.bitbrowser.reuse_env = False      # 不按名复用
    cfg.browser.bitbrowser.keep_open = True       # 注册完不关浏览器

    # 只用 icloud_relay（QQ 聚合箱 OTP 收信最可靠），避开 outlook_file OAuth 失效
    cfg.mail.strategy = "strict_priority"
    cfg.mail.strict_priority_order = ["icloud_relay"]

    print(f"=== 注册演示 @ {datetime.now().strftime('%Y-%m-%d %H:%M:%S')} ===")
    print(f"region={region}")
    print(f"env_id=(新建)  reuse_env=False  keep_open=True")
    print(f"mail strategy=strict_priority  order=[icloud_relay]")
    print()

    # 初始化 DB
    from app.db import create_all, init_engine
    init_engine(get(cfg, "storage.url", "sqlite:///data/qoder.db"))
    create_all()

    # 构造 orchestrator（此时 cdp_registrar 已用覆盖后的 config 构造）
    from app.orchestrator import Pipeline
    orch = Pipeline(cfg)

    print("=== 阶段 1+2+3 / 取邮箱 → 取代理 → 新建 env → 注册 → OTP → 入库 ===")
    rep = orch.register_only(region=region, worker=0)

    print()
    print("=== 演示结果 ===")
    print(f"  ok          = {rep.ok}")
    print(f"  stage       = {rep.stage}")
    print(f"  message     = {rep.message}")
    print(f"  account_id  = {rep.account_id}")
    print(f"  email       = {rep.email}")
    print(f"  captcha     = {rep.captcha_provider}")
    if rep.account_id:
        from app.db import session_scope
        from app.models import Account
        with session_scope() as s:
            a = s.get(Account, rep.account_id)
            if a:
                print(f"  state       = {a.state}")
                print(f"  password    = {a.password}")
                print(f"  username    = {a.username}")
                print(f"  register_ip = {a.register_ip}")
                print(f"  created_at  = {a.created_at}")

    # 报告 env_id（让用户知道去哪看浏览器）
    if hasattr(orch, "cdp_registrar") and orch.cdp_registrar:
        # register_only 没回 env_id，从最近 event_log payload 找
        from app.db import session_scope
        from app.models import EventLog
        with session_scope() as s:
            ev = s.query(EventLog).filter(
                EventLog.account_id == rep.account_id,
                EventLog.stage == "register",
            ).order_by(EventLog.id.desc()).first()
            if ev and ev.payload:
                try:
                    p = json.loads(ev.payload)
                    if "env_id" in p:
                        print(f"  bitbrowser env_id = {p['env_id']}")
                except Exception:
                    pass

    print()
    if rep.ok:
        print("[PASS] 注册+OTP+入库 全段通过。浏览器 env 仍开着，可去 BitBrowser 查看。")
        print("       下一步 device flow + token 抓取需要 kill Qoder 客户端，请单独确认。")
        return 0
    print(f"[FAIL] stage={rep.stage} msg={rep.message}")
    return 1


if __name__ == "__main__":
    import traceback
    try:
        code = main()
        print(f"\n[exit {code}]", flush=True)
        raise SystemExit(code)
    except SystemExit:
        raise
    except BaseException:
        traceback.print_exc()
        raise SystemExit(2)
