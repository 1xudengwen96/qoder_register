"""设备登录：把客户端复制的 device 链接丢进注册用的比特浏览器完成确认。

设计要求（用户明确）：从注册到抓 token、领积分完成之前，这个浏览器不能关。

    python tools/device_login.py --email medley.address_0m@icloud.com \
        --password 'xxx' [--link 剪贴板] [--env-name qoder_acct_5826]
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import secrets  # noqa: E402
from app.config import load_config  # noqa: E402
from app.net.proxy import parse_line  # noqa: E402
from app.register.bitbrowser import BitBrowserClient  # noqa: E402


def clipboard() -> str:
    try:
        p = subprocess.run(["powershell", "-NoProfile", "-Command",
                            "Get-Clipboard -Raw"], capture_output=True,
                           text=True, timeout=15, encoding="utf-8",
                           errors="replace")
        return (p.stdout or "").strip()
    except Exception:
        return ""


def save_client_token(email: str, token, db_id: int | None = None) -> None:
    """把 machine_token 落库（token_json 换来源）。"""
    import datetime as dt

    from sqlalchemy import create_engine, select
    from sqlalchemy.orm import sessionmaker

    from app.models import Account, AccountState, Base

    payload = {
        "kind": "client_machine_token",
        "token": token.token,
        "device_id": token.device_id,
        "token_type": token.token_type,
        "source": token.source,
        "captured_at": dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }
    engine = create_engine(f"sqlite:///{ROOT / 'data' / 'qoder.db'}")
    Base.metadata.create_all(engine)
    with sessionmaker(bind=engine)() as s:
        row = s.execute(select(Account).where(
            Account.email == email)).scalar_one_or_none()
        if row is None:
            print(f"库里没有 {email}，跳过写库")
            return
        row.token_json = json.dumps(payload, ensure_ascii=False)
        state = getattr(AccountState, "CLAIMED", None)
        if state is not None:
            row.state = state
        row.claimed_at = dt.datetime.now()
        s.commit()
        print(f"已写入 account#{row.id} token_json（客户端 token）")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--email", default="")
    ap.add_argument("--password", default="")
    ap.add_argument("--link", default="")
    ap.add_argument("--env-name", default="")
    ap.add_argument("--env-id", default=secrets.get("bitbrowser.env_id", ""))
    ap.add_argument("--timeout", type=int, default=240)
    args = ap.parse_args()

    email = args.email
    password = args.password
    if not email:
        # 从 accounts.txt 取第一个
        p = ROOT / "data" / "accounts.txt"
        if p.exists():
            first = p.read_text(encoding="utf-8").splitlines()[0]
            parts = first.split("----")
            email, password = parts[0], parts[1]
    print(f"账号: {email}")

    link = args.link or clipboard()
    if "qoder.com" not in link:
        print(f"剪贴板里没有登录链接: {link[:80]}")
        return 2
    print(f"链接: {link[:120]}…")

    client = BitBrowserClient()
    env = client.get_env(args.env_id)
    env_name = args.env_name or env.name
    proxy_raw = (f"http://{env.proxy_user}:{env.proxy_password}@"
                 f"{env.host}:{env.port}") if env.host else ""
    px = parse_line(proxy_raw) if proxy_raw else None
    print(f"复用环境: {env.id} ({env_name}) 代理: {env.host}:{env.port}")

    from app.claim import ui_login

    cfg = load_config()
    out = ui_login.drive_login(
        cfg, link, email, password,
        env_name=env_name, region="", timeout=args.timeout,
        artifacts_dir=str(ROOT / "data" / "artifacts"), proxy=px,
        close_when_done=False,          # 关键：浏览器不关
    )
    print("登录结果:", json.dumps(out.as_dict(), ensure_ascii=False))

    if not out.ok:
        print("登录未完成，看 artifacts 里的截图")
        return 1

    time.sleep(3)
    from app.claim.token_harvester import harvest_token

    tok = harvest_token()
    if not tok:
        print("客户端还没写出 machine_token，再等 5 秒")
        time.sleep(5)
        tok = harvest_token()
    if tok:
        print(f"拿到客户端 token: {tok.token[:16]}… source={tok.source}")
        save_client_token(email, tok, out.env_id if hasattr(out, "env_id") else None)
        return 0
    print("没抓到 token（客户端可能还没落盘）")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
