"""Qoder 注册端到端 —— 纯 CDP 版（不用 Playwright）。

    # 用已启动的比特浏览器环境（推荐：环境里已配好代理）
    python tools/run_signup_cdp.py --env-id e9f6dcb46bfc45b489e94a6d04a6f787 --index 0

    # 直接用已有 CDP 端点
    python tools/run_signup_cdp.py --cdp http://127.0.0.1:53888 --index 0

    # 只走到滑块，不消耗验证码
    python tools/run_signup_cdp.py --env-id ... --index 0 --stop-at captcha

    # 不出图，只看流程骨架
    python tools/run_signup_cdp.py --env-id ... --index 0 --dry-nav
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import secrets  # noqa: E402
from app.mail.outlook_oauth import OutlookMailbox, load_accounts  # noqa: E402
from app.register.cdp_page import CDPPage, new_page  # noqa: E402
from app.register.qoder_signup_cdp import QoderSignupCDP, wait_code_sync  # noqa: E402

MAIL_FILE = "D:/Backup/Downloads/outlook.txt"
IMAP_PROXY = "http://127.0.0.1:7897"


def log_setup(verbose: bool) -> logging.Logger:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)-5s %(message)s", datefmt="%H:%M:%S")
    return logging.getLogger("run-signup-cdp")


def resolve_endpoint(env_id: str, cdp: str) -> tuple[str, object]:
    """返回 (CDP 端点, 比特浏览器客户端或 None)。"""
    if cdp:
        return cdp, None
    from app.register.bitbrowser import BitBrowserClient

    client = BitBrowserClient()
    if not client.health():
        raise SystemExit("比特浏览器本地 API 不通（54345）")
    if not env_id:
        raise SystemExit("要么 --env-id，要么 --cdp")
    env = client.get_env(env_id)
    print(f"环境 {env.id} {env.name} proxyMethod={env.proxy_method} "
          f"{env.host}:{env.port}")
    launch = client.open_env(env_id)
    print(f"已启动 CDP={launch.cdp_http} pid={launch.pid}")
    return launch.cdp_http, client


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--env-id", default=secrets.get("bitbrowser.env_id", ""))
    ap.add_argument("--cdp", default="")
    ap.add_argument("--index", type=int, default=0, help="用清单里第几个邮箱")
    ap.add_argument("--mail-file",
                    default=secrets.get("mail.outlook_file", MAIL_FILE))
    ap.add_argument("--stop-at", default="", choices=["", "captcha"])
    ap.add_argument("--captcha-max", type=int, default=6)
    ap.add_argument("--captcha-key",
                    default=os.environ.get("TWOCAPTCHA_KEY")
                    or secrets.get("captcha.twocaptcha_key", ""),
                    help="2Captcha API Key（env > config/secrets.yaml）")
    ap.add_argument("--no-paid", action="store_true", help="禁用 2Captcha")
    ap.add_argument("--code-timeout", type=int, default=240)
    ap.add_argument("--keep-open", action="store_true")
    ap.add_argument("--new-tab", action="store_true", default=True,
                    help="新开标签页跑（默认）")
    ap.add_argument("--verbose", action="store_true")
    args = ap.parse_args()

    secrets.export_env()
    log = log_setup(args.verbose)

    accs = load_accounts(args.mail_file)
    if not accs:
        print(f"没读到邮箱: {args.mail_file}")
        return 2
    if args.index >= len(accs):
        print(f"index {args.index} 超出范围（共 {len(accs)}）")
        return 2
    acc = accs[args.index]
    print(f"=== 目标邮箱: {acc.email} ===")

    # 先验邮箱可用 + 拍基线（必须在提交表单之前）
    box = OutlookMailbox(acc, proxy=IMAP_PROXY)
    baseline: set[str] = set()
    connected = False
    last_err: Exception | None = None
    # token 只刷一次：每次重试都刷 token 会加重微软侧限流
    try:
        box.refresh_access_token()
        print(f"token 已刷新（有效期 {box.token.expires_at:%H:%M:%S}）"
              if getattr(box.token, "expires_at", None) else "token 已刷新")
    except Exception as exc:
        print(f"token 刷新失败: {type(exc).__name__}: {exc}")

    # 3 次走代理隧道，最后 1 次直连兜底，间隔递增给微软限流留时间
    plan = [(IMAP_PROXY, "代理")] * 3 + [(None, "直连")]
    for attempt, (px, label) in enumerate(plan, 1):
        try:
            box.proxy = px
            box.connect()
            box.select("INBOX")
            baseline = {u.decode() if isinstance(u, bytes) else str(u)
                        for u in box.list_uids()}
            connected = True
            print(f"邮箱 OK（第 {attempt} 次尝试/{label}），"
                  f"INBOX 基线 {len(baseline)} 封")
            break
        except Exception as exc:
            last_err = exc
            print(f"邮箱连接失败（{attempt}/{len(plan)} {label}）: "
                  f"{type(exc).__name__}: {exc}")
            try:
                box.close()
            except Exception:
                pass
            time.sleep(min(30, 8 * attempt))
    if not connected:
        print(f"邮箱不可用: {type(last_err).__name__}: {last_err}")
        return 2

    endpoint, client = resolve_endpoint(args.env_id, args.cdp)
    page = None
    created_env = bool(client and args.env_id)
    try:
        page = new_page(endpoint, "about:blank") if args.new_tab \
            else CDPPage(endpoint)
        print(f"CDP 已接管: {endpoint}")

        def code_provider() -> str:
            print(f"等验证码邮件（最多 {args.code_timeout}s）...")
            code = wait_code_sync(box, timeout=args.code_timeout, poll=4,
                                  baseline_uids=baseline)
            print(f"取到验证码: {code or '(超时)'}")
            return code

        # 2Captcha 作为主识别路径（用户指定，自研不参与决策）
        paid = None
        if not args.no_paid:
            if not args.captcha_key:
                print("缺 2Captcha Key：设 TWOCAPTCHA_KEY 或传 --captcha-key")
                return 2
            from app.net.twocaptcha_dragdrop import TwoCaptchaDragDrop

            paid = TwoCaptchaDragDrop(args.captcha_key, proxy=IMAP_PROXY)
            try:
                print(f"2Captcha 余额: ${paid.balance():.4f}")
            except Exception as exc:
                # 余额查不到不影响识别，只提示（网络抖动很常见）
                print(f"2Captcha 余额查询失败（继续跑）: "
                      f"{type(exc).__name__}: {exc}")

        flow = QoderSignupCDP(
            page, artifacts_dir=str(ROOT / "data" / "artifacts"),
            human=True, logger=log, captcha_max_attempts=args.captcha_max,
            proxy=IMAP_PROXY, paid_client=paid, prefer_paid=paid is not None)
        rep = flow.run(acc.email, code_provider=code_provider,
                       stop_before=args.stop_at)

        print("\n=== 阶段 ===")
        for st in rep.stages:
            print(f"  {'PASS' if st.ok else 'FAIL'}  {st.name:14s} "
                  f"{st.elapsed:6.1f}s  {st.detail}")
        print("\n=== 结果 ===")
        if rep.result:
            print(json.dumps({
                "ok": rep.result.ok, "stage": rep.result.stage,
                "detail": rep.result.detail, "email": rep.result.email,
                "password": rep.result.password,
                "name": f"{rep.result.first_name} {rep.result.last_name}",
            }, ensure_ascii=False, indent=2))
        if rep.artifacts:
            print("产物:")
            for a in rep.artifacts:
                print(f"  {a}")
        if rep.otp_debug:
            print("\nOTP 卡点现场（节选）:")
            print(json.dumps({
                "url": rep.otp_debug.get("url"),
                "inputs": rep.otp_debug.get("inputs"),
                "buttons": rep.otp_debug.get("allButtons"),
            }, ensure_ascii=False, indent=2)[:2000])
        return 0 if rep.ok else 1
    finally:
        try:
            if page:
                page.close()
        except Exception:
            pass
        if client and created_env and not args.keep_open:
            try:
                client.close_env(args.env_id)
                print(f"已关闭环境 {args.env_id}")
            except Exception:
                pass
        box.close()


if __name__ == "__main__":
    raise SystemExit(main())
