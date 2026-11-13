"""上线前就绪检查：把"能不能开跑"的每一项逐条核一遍。

不猜、不假设，能测的就真测。输出三类结论：
  READY   已就绪，无需操作
  STUB    代码框架在，但需要你提供给真实值（选择器、凭证、路径）
  MISSING 缺资源，跑不起来

用法：
    python tools/readiness_check.py
    python tools/readiness_check.py --verbose
"""

from __future__ import annotations

import argparse
import importlib
import json
import os
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

READY, STUB, MISSING = "READY", "STUB", "MISSING"
_BOLD = {"READY": "OK  ", "STUB": "TODO", "MISSING": "GAP "}
_rows: list[tuple[str, str, str, str]] = []


def add(cat: str, name: str, status: str, detail: str = "") -> None:
    _rows.append((cat, name, status, detail))


# ============================================================ 1. 依赖
def check_deps() -> None:
    import importlib.util

    need = {
        "sqlalchemy": "数据库 ORM",
        "yaml": "配置解析",
        "requests": "HTTP",
        "playwright": "浏览器自动化",
        "ddddocr": "本地 OCR",
    }
    optional = {
        "pywinauto": "Windows 客户端 UI 自动化（领取阶段可能用到）",
        "PIL": "打码自检",
    }
    for mod, desc in need.items():
        ok = importlib.util.find_spec(mod) is not None
        add("依赖", f"{mod} ({desc})", READY if ok else MISSING,
            "" if ok else "pip install " + mod)

    for mod, desc in optional.items():
        ok = importlib.util.find_spec(mod) is not None
        add("依赖", f"{mod} ({desc})", READY if ok else STUB,
            "" if ok else "可选，需要时再装")

    # chromium 内核
    try:
        from playwright.sync_api import sync_playwright
        with sync_playwright() as pw:
            path = pw.chromium.executable_path
        exists = Path(path).exists() if path else False
        add("依赖", "playwright chromium 内核", READY if exists else MISSING,
            str(path) if exists else "python -m playwright install chromium")
    except Exception as exc:
        add("依赖", "playwright chromium 内核", MISSING, str(exc)[:80])


# ============================================================ 2. 配置
def check_config() -> None:
    cfg_path = ROOT / "config" / "config.yaml"
    ex_path = ROOT / "config" / "config.example.yaml"
    add("配置", "config/config.yaml", READY if cfg_path.exists() else MISSING,
        "" if cfg_path.exists() else "copy config\\config.example.yaml config\\config.yaml")
    if not cfg_path.exists():
        return

    sys.path.insert(0, str(ROOT))
    from app.config import get, load_config
    try:
        cfg = load_config(str(cfg_path))
    except Exception as exc:
        add("配置", "配置可解析", MISSING, str(exc)[:100])
        return
    add("配置", "配置可解析", READY, str(cfg_path))

    # 数据库
    db_url = get(cfg, "storage.url", "")
    add("配置", "数据库 URL", READY if db_url else MISSING, db_url)

    # 邮箱凭证
    mains = get(cfg, "mail.outlook_tag.main_accounts", None) or []
    filled = 0
    for m in mains:
        pw = get(m, "password", "") if not isinstance(m, dict) else m.get("password", "")
        if pw and not str(pw).startswith("env:"):
            filled += 1
    if not mains:
        add("配置", "Outlook 主号", MISSING, "配置里没有 main_accounts")
    elif filled:
        add("配置", "Outlook 主号", READY, f"{filled}/{len(mains)} 个已填密码")
    else:
        add("配置", "Outlook 主号", STUB,
            f"{len(mains)} 个主号，密码是 env: 变量但未设置")

    agg_pw = get(cfg, "mail.icloud_relay.aggregator_imap.password", "")
    if agg_pw and not str(agg_pw).startswith("env:"):
        add("配置", "iCloud 聚合邮箱", READY, get(cfg, "mail.icloud_relay.aggregator_imap.user", ""))
    else:
        add("配置", "iCloud 聚合邮箱", STUB, "需要填 app 专用密码并设环境变量")

    # 打码
    keys = []
    for name in ("yescaptcha", "2captcha", "capsolver"):
        k = get(cfg, f"captcha.providers.{name}.api_key", "") or ""
        if k and not str(k).startswith("env:"):
            keys.append(name)
        elif k and os.environ.get(str(k).replace("env:", "")):
            keys.append(name)
    add("配置", "打码平台 Key", READY if keys else STUB,
        f"已配置: {','.join(keys)}" if keys else
        "只有本地 OCR（仅能过图片验证码；遇到 reCAPTCHA/Turnstile 会失败）")

    # 客户端路径
    client = get(cfg, "claim.client_path", "")
    ce = Path(client) if client else None
    add("配置", "Qoder 客户端路径", READY if (ce and ce.exists()) else STUB,
        client if (ce and ce.exists()) else f"配置为 {client}，文件不存在或需确认")

    # 指纹后端
    backend = str(get(cfg, "browser.backend", ""))
    add("配置", "指纹浏览器后端", READY if backend else STUB, backend)


# ============================================================ 3. 外部资源
def check_resources() -> None:
    # 代理
    pf = ROOT / "data" / "proxies.txt"
    n = 0
    if pf.exists():
        for line in pf.read_text(encoding="utf-8", errors="ignore").splitlines():
            s = line.split("#", 1)[0].strip()
            if s and not s.startswith("//"):
                n += 1
    add("资源", "住宅代理", READY if n else MISSING,
        f"{n} 个" if n else "data/proxies.txt 为空，注册必失败")

    # Hotmail
    hf = ROOT / "data" / "hotmail_accounts.csv"
    hn = 0
    if hf.exists():
        lines = [x for x in hf.read_text(encoding="utf-8-sig", errors="ignore").splitlines()
                 if x.strip() and not x.startswith("email,")]
        hn = len(lines)
    add("资源", "Hotmail 主号", READY if hn else MISSING,
        f"{hn} 个" if hn else "data/hotmail_accounts.csv 为空")

    # iCloud
    icf = ROOT / "data" / "icloud_relay.txt"
    icn = 0
    if icf.exists():
        for line in icf.read_text(encoding="utf-8", errors="ignore").splitlines():
            s = line.split("#", 1)[0].strip()
            if s and "@privaterelay.appleid.com" in s:
                icn += 1
    add("资源", "iCloud 隐藏地址", STUB if not icn else READY,
        f"{icn} 个" if icn else "0 个（iCloud+ 后台生成后导入）")

    # 数据库
    from app.config import get, load_config
    try:
        cfg = load_config(str(ROOT / "config" / "config.yaml"))
        url = get(cfg, "storage.url", "sqlite:///data/qoder.db")
        dbp = ROOT / url.replace("sqlite:///", "").lstrip("/")
        add("资源", "数据库文件", READY if dbp.exists() else STUB,
            str(dbp) + ("" if dbp.exists() else "（未初始化，跑 init-db）"))
    except Exception:
        add("资源", "数据库文件", STUB, "配置未就绪")


# ============================================================ 4. 比特浏览器
def check_bitbrowser() -> None:
    try:
        from app.register.bitbrowser import BitBrowserClient
    except Exception as exc:
        add("比特浏览器", "模块导入", MISSING, str(exc)[:80])
        return
    add("比特浏览器", "模块导入", READY, "app/register/bitbrowser.py")

    c = BitBrowserClient()
    port = c.is_port_open()
    add("比特浏览器", "客户端运行（端口 54345）", READY if port else MISSING,
        "" if port else "启动比特浏览器客户端")
    if not port:
        return

    health = c.health()
    add("比特浏览器", "本地 API 健康", READY if health else MISSING, "/health")

    try:
        _, total = c.list_envs(page=0, page_size=1)
        add("比特浏览器", "环境列表可读", READY, f"现有 {total} 个环境")
    except Exception as exc:
        add("比特浏览器", "环境列表可读", MISSING, str(exc)[:80])
        return

    # 真测一次启动
    try:
        from app.register.bitbrowser import (
            LaunchRestrictedError,
            build_fingerprint,
        )
        fp = build_fingerprint(ua=("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                                   "AppleWebKit/537.36 (KHTML, like Gecko) "
                                   "Chrome/131.0.0.0 Safari/537.36"))
        env = c.create_env(name=f"readiness_{os.getpid()}", fingerprint=fp,
                           remark="readiness-check")
        try:
            lr = c.open_env(env.id)
            ver = c.probe_cdp(lr.cdp_http)
            add("比特浏览器", "窗口启动（RPA 并发）", READY,
                f"pid={lr.pid} CDP={'可达' if ver else '不可达'}")
            c.close_env(env.id)
        except LaunchRestrictedError as exc:
            add("比特浏览器", "窗口启动（RPA 并发）", MISSING,
                f"code=11018 RPA 并发为 0，需开通套餐")
        finally:
            c.delete_env(env.id)
    except Exception as exc:
        add("比特浏览器", "窗口启动（RPA 并发）", MISSING, str(exc)[:100])


# ============================================================ 5. 代码桩位
def check_stubs() -> None:
    reg = ROOT / "app" / "register" / "browser_registrar.py"
    src = reg.read_text(encoding="utf-8") if reg.exists() else ""
    # 找选择器是否还是通用兜底
    generic = "input[type='email'], input[name='email']" in src
    add("桩位", "Qoder 注册页选择器", STUB if generic else READY,
        "用的是通用兜底选择器，需按真实 DOM 标定" if generic else "已标定")

    # siteKey
    from app.config import get, load_config
    try:
        cfg = load_config(str(ROOT / "config" / "config.yaml"))
        keys = {
            "recaptcha": get(cfg, "register.recaptcha_site_key", ""),
            "hcaptcha": get(cfg, "register.hcaptcha_site_key", ""),
            "turnstile": get(cfg, "register.turnstile_site_key", ""),
        }
        filled = [k for k, v in keys.items() if v]
        add("桩位", "验证码 siteKey", READY if filled else STUB,
            f"已填 {filled}" if filled else "三个都空，遇到验证码时无法自动求解")
    except Exception:
        add("桩位", "验证码 siteKey", STUB, "配置未就绪")

    cl = ROOT / "app" / "claim" / "claimer.py"
    csrc = cl.read_text(encoding="utf-8") if cl.exists() else ""
    has_probe = "credit_probe_url" in csrc
    add("桩位", "领取阶段额度接口", STUB,
        "代码有钩子，但 credit_probe_url 需抓包填真实地址")

    token_keys = ("accessToken", "refreshToken", "authorization")
    add("桩位", "客户端 Token 位置", STUB,
        "代码扫 %APPDATA%\\Qoder 与 CDP；若客户端加密存储需额外标定")


# ============================================================ 6. 功能自测
def check_selftest() -> None:
    import subprocess

    tests = [
        ("单元测试", [sys.executable, "-m", "unittest", "discover", "-s", "tests"]),
    ]
    for name, cmd in tests:
        try:
            r = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True, timeout=180)
            ok = r.returncode == 0
            m = re.search(r"Ran (\d+) tests", r.stderr or r.stdout or "")
            add("自测", name, READY if ok else MISSING,
                f"{m.group(1)} 个用例，{'全过' if ok else '有失败'}")
        except Exception as exc:
            add("自测", name, MISSING, str(exc)[:80])


# ============================================================ 主流程
def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--verbose", action="store_true")
    ap.add_argument("--skip-network", action="store_true", help="跳过比特浏览器启动测试")
    args = ap.parse_args()

    print("=== 上线前就绪检查 ===\n")
    check_deps()
    check_config()
    check_resources()
    if not args.skip_network:
        check_bitbrowser()
    check_stubs()
    check_selftest()

    cats: dict[str, list] = {}
    for row in _rows:
        cats.setdefault(row[0], []).append(row)

    for cat, rows in cats.items():
        print(f"\n--- {cat} ---")
        w = max(len(r[1]) for r in rows) + 2
        for _, name, status, detail in rows:
            print(f"[{_BOLD[status]}] {name:<{w}} {detail}")

    total = len(_rows)
    ready = sum(1 for r in _rows if r[2] == READY)
    stub = sum(1 for r in _rows if r[2] == STUB)
    missing = sum(1 for r in _rows if r[2] == MISSING)

    print(f"\n=== 汇总：{ready}/{total} 就绪，{stub} 待填，{missing} 缺失 ===")

    if missing:
        print("\n必须先解决（否则跑不起来）：")
        for _, name, status, detail in _rows:
            if status == MISSING:
                print(f"  - {name}: {detail}")

    if stub:
        print("\n需要你提供真实值（不填也能跑，但会失败或降级）：")
        for _, name, status, detail in _rows:
            if status == STUB:
                print(f"  - {name}: {detail}")

    return 0 if missing == 0 else 2


if __name__ == "__main__":
    raise SystemExit(main())
