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
    if env.status == 1:
        client.close_env(env_id)
        time.sleep(2)
    # 每次启动都是干净新窗口：单页启动、不恢复标签、不开工作台
    try:
        client.prepare_fresh_launch(env_id, "about:blank")
    except Exception as exc:
        print(f"设定干净启动失败（继续跑）: {type(exc).__name__}: {exc}")
    launch = client.open_env(env_id)
    print(f"已启动 CDP={launch.cdp_http} pid={launch.pid}")
    return launch.cdp_http, client


def build_relay_provider():
    """按配置组装 iCloud 隐藏地址 + QQ 聚合箱的收信通道。"""
    from app.config import get, load_config, to_plain
    from app.mail.relay_qq import ICloudRelayQQProvider, RelayMatchRules

    cfg = load_config()
    ic = get(cfg, "mail.icloud_relay")
    if not ic:
        raise SystemExit("配置里没有 mail.icloud_relay")
    agg = to_plain(get(ic, "aggregator_imap", {}) or {})
    rules = RelayMatchRules.from_config(to_plain(get(ic, "match", {}) or {}) or {})
    return ICloudRelayQQProvider(
        relay_addresses_file=get(ic, "relay_addresses_file", ""),
        imap_host=str(agg.get("host") or "imap.qq.com"),
        imap_port=int(agg.get("port") or 993),
        imap_user=str(agg.get("user") or ""),
        imap_password=str(agg.get("password") or ""),
        folder=str(agg.get("folder") or "INBOX"),
        imap_id=str(agg.get("imap_id") or ""),
        quota_total=int(get(ic, "quota_total", 500)),
        rules=rules,
    )


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--env-id", default=secrets.get("bitbrowser.env_id", ""))
    ap.add_argument("--cdp", default="")
    ap.add_argument("--mail-source", default="outlook",
                    choices=["outlook", "relay"],
                    help="outlook=清单邮箱；relay=iCloud 隐藏地址(QQ 聚合箱收码)")
    ap.add_argument("--relay-address", default="",
                    help="指定隐藏地址（不给就从 data/icloud_relay.txt 池里取）")
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

    box = None
    relay = None
    baseline: set[str] = set()

    if args.mail_source == "relay":
        relay = build_relay_provider()
        pool = relay.identities()
        print(f"隐藏地址池: {len(pool)} 个 ({relay.pool.path})")
        if args.relay_address:
            target_email = args.relay_address.strip()
        else:
            got = relay.acquire(1)
            if not got:
                print("地址池为空：把 data/icloud_relay.txt 里的占位行换成真地址")
                return 2
            target_email = got[0].address
        print(f"=== 目标隐藏地址: {target_email} ===")
        print(f"聚合箱: {relay.imap_user or '(未填)'} @ "
              f"{relay.imap_host}:{relay.imap_port}/{relay.folder}")
        try:
            with relay.inbox() as ib:
                print(f"聚合箱连接 OK，共 {len(ib.uid_list())} 封")
        except Exception as exc:
            print(f"聚合箱连接失败: {type(exc).__name__}: {exc}")
            print("检查 QQ 邮箱 IMAP 是否开启、密码是否为授权码")
            return 2
    else:
        accs = load_accounts(args.mail_file)
        if not accs:
            print(f"没读到邮箱: {args.mail_file}")
            return 2
        if args.index >= len(accs):
            print(f"index {args.index} 超出范围（共 {len(accs)}）")
            return 2
        acc = accs[args.index]
        target_email = acc.email
        print(f"=== 目标邮箱: {target_email} ===")

    # 先验邮箱可用 + 拍基线（必须在提交表单之前）
    if relay is None:
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
                    if box:
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
            if relay is not None:
                from app.mail.base import MailIdentity
                print(f"等聚合箱收码（最多 {args.code_timeout}s）...")
                code = relay.wait_code(
                    MailIdentity(address=target_email,
                                provider="icloud_relay"),
                    timeout=args.code_timeout, poll_interval=6)
                print(f"取到验证码: {code or '(超时)'}")
                return code
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
        rep = flow.run(target_email, code_provider=code_provider,
                       stop_before=args.stop_at)

        # 注册成功必须落库，否则账号就丢了
        if rep.ok and rep.result:
            from app.register.store import save_registered

            px = ""
            try:
                _env = BitBrowserClient().get_env(args.env_id)
                px = f"{_env.host}:{_env.port}"
            except Exception:
                pass
            saved = save_registered(
                email=rep.result.email, password=rep.result.password,
                first=rep.result.first_name, last=rep.result.last_name,
                env_id=args.env_id, proxy=px,
                source=("icloud_relay" if relay else "outlook"))
            print(f"\n已保存账号 -> {saved['jsonl']}  (db_id={saved['db_id']})")
            print(f"           -> {saved['txt']}")

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
                "toast": rep.otp_debug.get("toast"),
                "network": rep.otp_debug.get("network"),
            }, ensure_ascii=False, indent=2)[:3000])
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
        if box:
            box.close()


if __name__ == "__main__":
    raise SystemExit(main())
