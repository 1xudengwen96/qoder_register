"""Web 控制台后端：配置读写 + 状态 + 启动机器人 + 日志流。

零第三方依赖（标准库 http.server），与 CLI 同进程模型：
- GET  /                 → web/index.html 静态前端
- GET  /api/config       → 当前可编辑配置（密码脱敏）
- POST /api/config       → 写回 config.yaml（文本级键值替换，保留注释；先备份）
- GET  /api/status       → 账号/邮箱池/代理/打码余额/比特健康
- POST /api/run          → 启动并发注册机器人（workers 并发数 + target 注册目标截止）
- GET  /api/run/tail     → 运行日志增量（字节偏移）+ 进程状态
- POST /api/run/stop     → 停止机器人（杀进程树）
- POST /api/check/captcha→ 2Captcha 连通性自检（直连）
"""
from __future__ import annotations

import csv
import json
import os
import re
import shutil
import subprocess
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Lock

ROOT = Path(__file__).resolve().parent.parent
WEB_DIR = ROOT / "web"
RUN_LOG = ROOT / "data" / "webui" / "run.log"
CFG_PATH = ROOT / "config" / "config.yaml"

from app.config import load_config, get, to_plain  # noqa: E402

# ------------------------------------------------------------- 配置读写
# 可编辑字段：前端路径 → config.yaml 中的定位（顶层键或嵌套段内键）
EDITABLE = {
    "region": {"scope": "top", "key": "region"},
    "mail.provider": {"scope": "mail", "key": "provider"},
    "mail.imap_proxy": {"scope": "mail", "key": "imap_proxy"},
    "proxy.enabled": {"scope": "proxy", "key": "enabled"},
    "imap.host": {"scope": "aggregator_imap", "key": "host"},
    "imap.port": {"scope": "aggregator_imap", "key": "port"},
    "imap.user": {"scope": "aggregator_imap", "key": "user"},
    "imap.password": {"scope": "aggregator_imap", "key": "password"},
    "imap.folder": {"scope": "aggregator_imap", "key": "folder"},
    "bb.port": {"scope": "bitbrowser", "key": "port"},
    "pool.size": {"scope": "env_pool", "key": "size"},
    "device.identity_mode": {"scope": "device", "key": "identity_mode"},
}

# 敏感字段：GET 脱敏；POST 提交掩码/空串 = 不修改
SECRET_KEYS = {"imap.password", "mail.imap_proxy"}
MASK = "••••••••"


def _yaml_scalar(value) -> str:
    """把 Python 值格式化成 yaml 标量（够用级别：str/int/bool）。"""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    s = str(value)
    if s == "" or re.search(r"""[#:\s{}[\],&*'"]""", s):
        return json.dumps(s, ensure_ascii=False)
    return s


def _keep_comment(raw: str, new_value: str) -> str:
    """把旧值换成新值，但保留行尾注释和原有对齐空格（raw = 原 `key:` 行中值的部分）。"""
    m = re.search(r"(\s*)#", raw)
    if m:
        return new_value + m.group(1) + raw[m.end() - 1:]
    return new_value


def _replace_in_block(text: str, scope: str, key: str, raw_value: str) -> str:
    """在 yaml 文本里做键值替换，保留全部注释。

    scope="top"        → 顶层任意缩进的 `key:` 行
    scope="mail"       → `mail:` 段内的 `  key:` 行（2 空格）
    scope="aggregator_imap" → 嵌套段（任意缩进 4/6 空格都认）
    """
    if scope == "top":
        pat = re.compile(rf"^({key}:\s*)(.*)$", re.M)
    else:
        # 先定位 scope 段头，再在其子块内替换第一个匹配
        head = re.search(rf"^(\s*){scope}:\s*$", text, re.M)
        if not head:
            head = re.search(rf"^(\s*){scope}:\s*#.*$", text, re.M)
        if not head:
            raise KeyError(f"config.yaml 找不到段 {scope}:")
        base_indent = head.group(1)
        start = head.end()
        # 段块 = 到下一个更浅缩进（或顶层）的 key: 行为止
        tail = re.search(r"^(?=[^\s#])", text[start:], re.M)
        block = text[start:start + (tail.start() if tail else len(text[start:]))]
        sub_pat = re.compile(rf"^({base_indent} +{key}:\s*)(.*)$", re.M)
        m = sub_pat.search(block)
        if not m:
            raise KeyError(f"{scope}: 段内找不到键 {key}")
        new_block = (block[:m.start(2)] + _keep_comment(m.group(2), raw_value)
                     + block[m.end(2):])
        return text[:start] + new_block + text[start + len(block):]
    m = pat.search(text)
    if not m:
        raise KeyError(f"config.yaml 找不到顶层键 {key}:")
    return text[:m.start(2)] + _keep_comment(m.group(2), raw_value) + text[m.end(2):]


def read_config_fields() -> dict:
    cfg = load_config()
    agg = to_plain(get(cfg, "mail.icloud_relay.aggregator_imap", {})) or {}
    out = {
        "region": get(cfg, "region", "sg"),
        "mail.provider": get(cfg, "mail.provider", ""),
        "mail.imap_proxy": get(cfg, "mail.imap_proxy", "") or "",
        "proxy.enabled": bool(get(cfg, "proxy.enabled", True)),
        "imap.host": agg.get("host", ""),
        "imap.port": agg.get("port", 993),
        "imap.user": agg.get("user", ""),
        "imap.password": agg.get("password", ""),
        "imap.folder": agg.get("folder", "INBOX"),
        "bb.port": int(get(cfg, "browser.bitbrowser.port", 54345) or 54345),
        "pool.size": int(get(cfg, "browser.env_pool.size", 4) or 4),
        "device.identity_mode": (
            "browser" if str(get(cfg, "device.identity_mode", "") or "").lower()
            in ("browser", "match", "fingerprint_browser") else "random"),
    }
    for k in SECRET_KEYS:
        if out.get(k):
            out[k] = MASK
    return out


def write_config_fields(patch: dict) -> dict:
    """写回 config.yaml：只改提交的非掩码字段，保留注释，自动备份。"""
    applied, skipped = [], []
    changed = {}
    text = CFG_PATH.read_text(encoding="utf-8")
    for path, spec in EDITABLE.items():
        if path not in patch:
            continue
        val = patch[path]
        if path in SECRET_KEYS and (val == MASK or val == ""):
            skipped.append(path)
            continue
        if path == "bb.port":
            try:
                val = int(val)
            except (TypeError, ValueError):
                skipped.append(f"{path}（端口须为整数）")
                continue
            if not 1 <= val <= 65535:
                skipped.append(f"{path}（端口须 1-65535）")
                continue
        if path == "pool.size":
            try:
                val = int(val)
            except (TypeError, ValueError):
                skipped.append(f"{path}（窗口数须为整数）")
                continue
            if not 1 <= val <= 500:
                skipped.append(f"{path}（窗口数须 1-500）")
                continue
        if path == "device.identity_mode":
            val = str(val).strip().lower()
            if val in ("match", "fingerprint_browser"):
                val = "browser"
            if val not in ("random", "browser"):
                skipped.append(f"{path}（须 random/browser）")
                continue
        try:
            text = _replace_in_block(text, spec["scope"], spec["key"],
                                     _yaml_scalar(val))
            applied.append(path)
            changed[path] = val
        except KeyError as exc:
            skipped.append(f"{path}（{exc}）")
    if applied:
        bak = ROOT / "data" / "webui" / f"config.bak.{int(time.time())}.yaml"
        bak.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(CFG_PATH, bak)
        CFG_PATH.write_text(text, encoding="utf-8")
    return {"applied": applied, "skipped": skipped, "backup": str(bak) if applied else ""}


# ------------------------------------------------------------- 状态聚合
# 注册成功口径：走到 registered 及之后任一存活状态都算成果
SUCCESS_STATES = ("registered", "verified", "claimed", "stock", "sold")


def _provider_file_avail(taken: set[str]) -> set[str]:
    """当前邮箱源文件里的可用地址集（有效行 - 已占用）。

    控制台「导入邮箱池」只写文件不写 DB（hotmail CSV / outlook 清单），
    启动门槛 mail_pool 若只数 DB POOL 会误报「邮箱池已空」拦住启动
    （2026-11-19 异机实测：导入 17 个仍提示空）。
    返回地址集合：与 DB POOL 取并集计数，避免同一批地址在文件+DB 双计。
    """
    cfg = load_config()
    only = str(get(cfg, "mail.provider", "") or "").strip()
    if only == "outlook_file":
        path = get(cfg, "mail.outlook_file", "") or ""
    else:
        path = get(cfg, "mail.hotmail.credentials_file",
                   "data/hotmail_accounts.csv")
    p = Path(path) if path else None
    if p is not None and not p.is_absolute():
        p = ROOT / p
    if p is None or not p.exists():
        return set()
    out: set[str] = set()
    # 与生产 HotmailProvider._load 同步：hotmail 链只收微软域，
    # 非微软域计入会误导启动门槛（面板有货、机器人取号被过滤报空）
    hints = None
    if only not in ("outlook_file", "outlook_tag"):
        from app.mail.hotmail import HOTMAIL_HINTS
        hints = HOTMAIL_HINTS
    for line in p.read_text(encoding="utf-8-sig", errors="replace").splitlines():
        r = _pool_row("mail", line.strip())
        if not r or r[0] in taken:
            continue
        if hints and not any(h in r[0] for h in hints):
            continue
        out.add(r[0])
    return out


def _stat_accounts() -> dict:
    from app.db import create_all, init_engine, session_scope
    from app.models import (Account, AccountState, MailIdentity as MailRow,
                            MailStatus)
    init_engine(f"sqlite:///{ROOT / 'data' / 'qoder.db'}".replace("\\", "/"))
    create_all()
    with session_scope() as s:
        total = s.query(Account).count()
        ok = (s.query(Account)
              .filter(Account.state.in_([AccountState(v) for v in SUCCESS_STATES]))
              .count())
        pool_rows = (s.query(MailRow).filter(MailRow.status == MailStatus.POOL)
                     .all())
        pool = len(pool_rows)
        pending = s.query(MailRow).filter(MailRow.status == MailStatus.PENDING).count()
        used = s.query(MailRow).filter(MailRow.status == MailStatus.USED).count()
        taken = {row.address.lower() for row in s.query(MailRow)
                 .filter(MailRow.status != MailStatus.POOL).all()}
    # 文件池：面板导入的邮箱在 provider 文件里不在 DB；与 DB POOL 取并集，
    # 同一批地址不会双计，异机「导入 17 个仍报空」的误拦不再发生
    file_addrs = _provider_file_avail(taken)
    db_pool_addrs = {row.address.lower() for row in pool_rows}
    mail_pool = len(db_pool_addrs | file_addrs)
    return {"accounts_total": total, "accounts_registered": ok,
            "mail_pool": mail_pool, "mail_pending": pending,
            "mail_used": used}


def _stat_proxy() -> dict:
    ip = ROOT / "data" / "ip.txt"
    lines = [l for l in ip.read_text(encoding="utf-8").splitlines()
             if l.strip()] if ip.exists() else []
    return {"ip_file_remaining": len(lines)}


def _stat_captcha() -> dict:
    import requests
    from app.secrets import get as sec_get
    key = str(sec_get("captcha.twocaptcha_key", "") or "")
    t0 = time.time()
    try:
        r = requests.get("https://2captcha.com/res.php",
                         params={"key": key, "action": "getbalance", "json": 1},
                         timeout=12)
        d = r.json()
        return {"balance": float(d.get("request", 0)) if d.get("status") else None,
                "latency": round(time.time() - t0, 1), "ok": bool(d.get("status"))}
    except Exception:
        return {"balance": None, "latency": round(time.time() - t0, 1), "ok": False}


def _stat_bitbrowser() -> dict:
    import requests

    from app.register.bitbrowser import resolve_base
    try:
        r = requests.post(resolve_base() + "/health", json={}, timeout=4)
        ok = bool(r.json().get("success"))
    except Exception:
        ok = False
    return {"ok": ok}


# ------------------------------------------------------------ 数据池批量管理
POOL_KINDS = ("mail", "relay", "proxy", "ip")


def _pool_path(kind: str) -> Path:
    cfg = load_config()
    if kind == "mail":
        # 与生产 HotmailProvider 同一文件（mail.hotmail.credentials_file，CSV）
        return ROOT / str(get(cfg, "mail.hotmail.credentials_file",
                              "data/hotmail_accounts.csv"))
    if kind == "relay":
        return ROOT / str(get(cfg, "mail.icloud_relay.relay_addresses_file",
                              "data/icloud_relay.txt"))
    if kind == "proxy":
        return ROOT / str(get(cfg, "proxy.file", "data/proxies.txt"))
    return ROOT / "data" / "ip.txt"   # acquire_for_browser 消耗式 IP 池


def _mask(s: str) -> str:
    s = str(s or "")
    return (s[:2] + "••••" + s[-2:]) if len(s) > 6 else "••••••••"


def _mail_fields(s: str):
    """邮箱凭证双格式解析：---- 分隔（outlook 清单）/ CSV（hotmail 清单）。

    返回统一字段 dict；无效行（含 CSV 表头）返回 None。
    """
    if "----" in s:
        from app.mail.outlook_oauth import parse_line
        a = parse_line(s)
        if a is None or not a.password:
            return None
        return {"email": a.email.strip(), "password": a.password,
                "imap_host": "outlook.office365.com", "region": "",
                "client_id": a.client_id, "refresh_token": a.refresh_token,
                "rec": a.recovery_email, "rec_pass": a.recovery_password,
                "oauth": bool(a.has_oauth)}
    parts = [x.strip() for x in next(csv.reader([s]))]
    if len(parts) < 2 or "@" not in parts[0] or not parts[1]:
        return None
    if parts[0].lower() == "email":      # CSV 表头不是账号
        return None
    return {"email": parts[0], "password": parts[1],
            "imap_host": (parts[2] if len(parts) > 2 and parts[2]
                          else "outlook.office365.com"),
            "region": parts[3] if len(parts) > 3 else "",
            "client_id": parts[4] if len(parts) > 4 else "",
            "refresh_token": parts[5] if len(parts) > 5 else "",
            "rec": "", "rec_pass": "",
            "oauth": bool(len(parts) > 5 and parts[4] and parts[5])}


def _mail_canon(s: str, csv_mode: bool) -> str | None:
    """把导入行归一成目标文件格式：CSV 文件存 CSV，---- 文件存 ----。

    已是目标格式的行原样保留（不丢 region / OAuth 等额外列）。
    """
    f = _mail_fields(s)
    if f is None:
        return None
    if csv_mode:
        if "----" not in s:
            return s
        # 扩展列保留 OAuth 凭证；生产 DictReader 按列名读，多余列自动忽略
        return ",".join([f["email"], f["password"], f["imap_host"],
                         f["region"], f["client_id"], f["refresh_token"]])
    if "----" in s:
        return s
    cols = [f["email"], f["password"]]
    if f["client_id"] or f["refresh_token"]:
        cols += [f["client_id"], f["refresh_token"]]
    return "----".join(cols)


MAIL_CSV_HEADER = "email,password,imap_host,region,client_id,refresh_token"


def _pool_finalize(kind: str, lines: list[str]) -> list[str]:
    """CSV 邮箱清单始终保留表头，保证 csv.DictReader 生产读取不错位。"""
    if kind == "mail" and _pool_path(kind).suffix.lower() == ".csv":
        if not lines or lines[0].split(",")[0].strip().lower() != "email":
            lines = [MAIL_CSV_HEADER] + lines
    return lines


def _pool_row(kind: str, line: str):
    """解析一行为 (key, 展示行)；格式不认识返回 None。校验复用生产解析器。"""
    s = line.strip()
    if not s or s.startswith(("#", ";", "//")):
        return None
    if kind == "mail":
        f = _mail_fields(s)
        if f is None:
            return None
        key = f["email"].lower()
        return key, {
            "key": key, "title": f["email"],
            "meta": _mask(f["password"]),
            "tag": "OAuth" if f["oauth"] else "IMAP",
            "ok": True,
        }
    if kind == "relay":
        if "@" not in s or " " in s:
            return None
        return s.lower(), {"key": s.lower(), "title": s, "meta": "隐藏地址",
                           "tag": "relay", "ok": True}
    # proxy / ip 都用代理四格式解析器
    from app.net.proxy import parse_line as parse_proxy
    px = parse_proxy(s)
    if px is None:
        return None
    if kind == "proxy":
        return px.raw, {"key": px.raw, "title": f"{px.host}:{px.port}",
                        "meta": _mask(px.password or ""),
                        "tag": (px.region or "any").upper(), "ok": True}
    return px.raw, {"key": px.raw, "title": f"{px.host}:{px.port}",
                    "meta": px.username or "", "tag": "ip池", "ok": True}


def _pool_raw_lines(kind: str) -> list[str]:
    p = _pool_path(kind)
    if not p.exists():
        return []
    return [l for l in p.read_text(encoding="utf-8-sig", errors="replace").splitlines()
            if l.strip()]


def _pool_write(kind: str, lines: list[str]) -> None:
    p = _pool_path(kind)
    p.parent.mkdir(parents=True, exist_ok=True)
    if p.exists():
        bak = ROOT / "data" / "webui" / f"pool_{kind}.bak"
        bak.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(p, bak)
    tmp = p.with_suffix(p.suffix + ".tmp")
    tmp.write_text("\n".join(lines) + ("\n" if lines else ""), encoding="utf-8")
    tmp.replace(p)


def pool_summary() -> dict:
    out = {}
    for kind in POOL_KINDS:
        rows = [r for r in (_pool_row(kind, l) for l in _pool_raw_lines(kind)) if r]
        p = _pool_path(kind)
        try:
            disp = str(p.relative_to(ROOT)).replace("\\", "/")
        except ValueError:
            disp = str(p)
        info = {"count": len(rows), "path": disp}
        if kind == "mail":
            info["usable"] = sum(1 for _, r in rows if r["ok"])
        if kind == "proxy":
            reg: dict[str, int] = {}
            for _, r in rows:
                reg[r["tag"]] = reg.get(r["tag"], 0) + 1
            info["regions"] = reg
        out[kind] = info
    return out


def bb_pool_status() -> dict:
    """比特连接 + 窗口池状态（指挥台展示）。

    连接信息（port/总窗数/运行清单）始终返回；池未启用/比特离线降级不报错。
    全量列表走 5s 短缓存：多 tab 轮询/连发请求共享快照，
    避免高频 /browser/list 触发比特限流安全机制。
    """
    cfg = load_config()
    from app.register.bitbrowser import BitBrowserClient, resolve_base

    base = resolve_base(cfg)
    out = {"enabled": bool(get(cfg, "browser.env_pool.enabled", True)),
           "port": int(base.rsplit(":", 1)[-1]), "base": base}
    try:
        client = BitBrowserClient(base)
        if not client.is_port_open():
            return {**out, "online": False}
        items = _bb_envs(client)
        prefix = str(get(cfg, "browser.env_pool.prefix", "qpool")) + "_"
        run_envs = [{"name": e.name, "pool": e.name.startswith(prefix)}
                    for e in items if e.status]
        out.update(online=True, env_total=len(items),
                   running_total=len(run_envs), running_envs=run_envs)
        if out["enabled"]:
            from app.register.bb_pool import build_env_pool
            out.update(build_env_pool(cfg, client).status(items=items))
        else:
            out.update(size=int(get(cfg, "browser.env_pool.size", 4)),
                       pool_total=0, running=0, pool=[])
        return out
    except Exception as exc:  # noqa: BLE001
        return {**out, "online": False, "error": str(exc)}


def bb_pool_prepare(size: int) -> dict:
    """开工前预创建：写池大小 → 补齐 N 个**直连**窗口（指纹已建好）。

    后续任务按模式取用：直连任务直接用不配 IP；IP 模式任务
    在 acquire 时才写入代理。同步执行（建窗每个约 1-2s）。
    """
    if not 1 <= int(size) <= 500:
        return {"ok": False, "error": "窗口数须 1-500"}
    w = write_config_fields({"pool.size": int(size)})
    if "pool.size" not in w["applied"]:
        return {"ok": False, "error": "池大小写入失败：" + "; ".join(w["skipped"])}
    cfg = load_config()
    from app.register.bitbrowser import BitBrowserClient, resolve_base
    try:
        client = BitBrowserClient(resolve_base(cfg))
        if not client.is_port_open():
            return {"ok": False, "error": "比特浏览器未连接，先启动客户端"}
        from app.register.bb_pool import build_env_pool
        pool = build_env_pool(cfg, client)
        if pool is None:
            return {"ok": False, "error": "窗口池已关闭（env_pool.enabled=false）"}
        rep = pool.prepare()
        _BB_CACHE.update(ts=0.0)   # 建窗后失效短缓存，状态立刻可见
        return {"ok": True, **rep, "size": pool.size}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": str(exc)}


def bb_check(port: int | None = None) -> dict:
    """比特连通性自检（不写配置）：指定端口或当前配置端口。"""
    from app.register.bitbrowser import BitBrowserClient, resolve_base

    base = f"http://127.0.0.1:{int(port)}" if port else resolve_base()
    t0 = time.time()
    try:
        client = BitBrowserClient(base)
        if not client.is_port_open():
            return {"ok": False, "error": "端口未监听"}
        items = _bb_envs(client)
        return {"ok": True, "port": int(base.rsplit(":", 1)[-1]),
                "latency": round(time.time() - t0, 1), "env_total": len(items)}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": str(exc)}


_BB_CACHE = {"base": "", "ts": 0.0, "items": None}
_BB_CACHE_LOCK = Lock()


def _bb_envs(client, ttl: float = 5.0) -> list:
    """全量窗口列表短缓存（持锁拉取：并发请求排队后共享快照）。"""
    with _BB_CACHE_LOCK:
        now = time.time()
        if (_BB_CACHE["items"] is not None
                and _BB_CACHE["base"] == client.base
                and now - _BB_CACHE["ts"] < ttl):
            return _BB_CACHE["items"]
        items = client.list_all_envs(page_size=200)
        _BB_CACHE.update(base=client.base, ts=now, items=items)
        return items


def pool_list(kind: str, q: str = "") -> dict:
    rows = [r for r in (_pool_row(kind, l) for l in _pool_raw_lines(kind)) if r]
    if q:
        ql = q.lower()
        rows = [(k, r) for k, r in rows
                if ql in k or ql in r["title"].lower() or ql in r["tag"].lower()]
    return {"kind": kind, "total": len(rows), "rows": [r for _, r in rows]}


def pool_import(kind: str, text: str, mode: str = "append") -> dict:
    base = _pool_raw_lines(kind) if mode == "append" else []
    csv_mode = kind == "mail" and _pool_path(kind).suffix.lower() == ".csv"
    seen = set()
    for l in base:
        r = _pool_row(kind, l)
        if r:
            seen.add(r[0])
    added, dup, bad_lines = [], 0, []
    for i, raw in enumerate((text or "").splitlines(), 1):
        s = raw.strip()
        if not s or s.startswith(("#", ";", "//")):
            continue
        r = _pool_row(kind, s)
        if r is None:
            bad_lines.append(i)
        elif r[0] in seen:
            dup += 1
        else:
            seen.add(r[0])
            added.append(_mail_canon(s, csv_mode) if kind == "mail" else s)
    lines = _pool_finalize(kind, base + added)
    _pool_write(kind, lines)
    total = sum(1 for l in lines if _pool_row(kind, l))
    return {"added": len(added), "dup": dup, "bad": len(bad_lines),
            "bad_lines": bad_lines[:20], "total": total}


def pool_delete(kind: str, keys: list[str]) -> dict:
    ks = set(keys or [])
    kept = []
    for l in _pool_raw_lines(kind):
        r = _pool_row(kind, l)
        if r is None or r[0] not in ks:
            kept.append(l)
    kept = _pool_finalize(kind, kept)
    _pool_write(kind, kept)
    return {"total": sum(1 for l in kept if _pool_row(kind, l))}


def pool_clear(kind: str) -> dict:
    _pool_write(kind, _pool_finalize(kind, []))
    return {"total": 0}


# ------------------------------------------------------------ 成功成果·导出交付
def _output_dir() -> Path:
    return ROOT / "data" / "output"


def _exports_dir() -> Path:
    return _output_dir() / "exports"


RESULT_FILE_RE = re.compile(r"^results_\d{8}_\d{6}\.(txt|csv|jsonl)$")


def _rel_disp(p: Path) -> str:
    """项目内路径显示为相对（可移植），项目外（如测试临时目录）显示绝对。"""
    try:
        return str(p.relative_to(ROOT)).replace("\\", "/")
    except ValueError:
        return str(p)


def _result_rows(full: bool = False) -> list[dict]:
    """成功账号列表；full=True 带明文密码/token（仅导出用），否则脱敏。"""
    from app.db import create_all, init_engine, session_scope
    from app.models import Account, AccountState
    from app.stock.txt_export import _dt, _state, _token_fields
    init_engine(f"sqlite:///{ROOT / 'data' / 'qoder.db'}".replace("\\", "/"))
    create_all()
    with session_scope() as s:
        rows = (s.query(Account)
                .filter(Account.state.in_([AccountState(v) for v in SUCCESS_STATES]))
                .order_by(Account.created_at.desc()).all())
        out = []
        for a in rows:
            tf = _token_fields(a.token_json)
            row = {"email": a.email or "", "username": a.username or "",
                   "state": _state(a.state), "credits": int(a.credits or 0),
                   "plan": a.plan or "", "expire": _dt(a.trial_expire_at),
                   "ip": a.register_ip or "", "created": _dt(a.created_at),
                   "has_token": bool(tf.get("token"))}
            if full:
                row.update(password=a.password or "", token=tf.get("token", ""),
                           device_id=tf.get("device_id", ""))
            else:
                row["meta"] = _mask(a.password or "")
            out.append(row)
    return out


def results_summary() -> dict:
    from app.db import create_all, init_engine, session_scope
    from app.models import Account
    from app.stock.txt_export import _state
    rows = _result_rows()
    init_engine(f"sqlite:///{ROOT / 'data' / 'qoder.db'}".replace("\\", "/"))
    create_all()
    with session_scope() as s:
        all_rows = s.query(Account).all()
    states: dict[str, int] = {}
    for a in all_rows:
        k = _state(a.state)
        states[k] = states.get(k, 0) + 1
    return {"success": len(rows),
            "credits": sum(int(a.credits or 0) for a in all_rows),
            "tokens": sum(1 for a in all_rows if a.token_json),
            "states": states, "rows": rows}


def results_export(fmt: str) -> dict:
    """成功成果快照导出到 data/output/exports/，返回具体位置。"""
    if fmt not in ("txt", "csv", "jsonl"):
        raise ValueError("fmt 必须是 txt/csv/jsonl")
    from app.stock.txt_export import SEP
    rows = _result_rows(full=True)
    d = _exports_dir()
    d.mkdir(parents=True, exist_ok=True)
    name = f"results_{time.strftime('%Y%m%d_%H%M%S')}.{fmt}"
    p = d / name
    cols = ["email", "password", "username", "state", "credits", "plan",
            "expire", "ip", "created", "token", "device_id"]
    if fmt == "txt":
        lines = ["# Qoder 成功成果（自动生成）",
                 "# 格式：" + SEP.join(cols)]
        lines += [SEP.join(str(r[c]) for c in cols) for r in rows]
        p.write_text("\n".join(lines) + "\n", encoding="utf-8-sig")
    elif fmt == "csv":
        with p.open("w", encoding="utf-8-sig", newline="") as fh:
            w = csv.writer(fh)
            w.writerow(cols)
            for r in rows:
                w.writerow([r[c] for c in cols])
    else:
        p.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows),
                     encoding="utf-8")
    return {"filename": name, "path": str(p),
            "rel": _rel_disp(p),
            "count": len(rows), "bytes": p.stat().st_size,
            "url": f"/api/results/file?f={name}"}


def results_export_all() -> dict:
    """库存三件套（accounts/tokens/summary）全量重写到 data/output/。"""
    from app.db import create_all, init_engine, session_scope
    from app.stock.txt_export import export_all
    init_engine(f"sqlite:///{ROOT / 'data' / 'qoder.db'}".replace("\\", "/"))
    create_all()
    with session_scope() as s:
        rep = export_all(s, _output_dir())
    return rep.as_dict()


# ------------------------------------------------------------- 运行器
PROGRESS_PATH = ROOT / "data" / ".run_progress.json"
RUN_PREFS = ROOT / "data" / "webui" / "run_prefs.json"


def read_run_prefs() -> dict:
    """指挥台保存的启动参数（并发/目标）；页面与 API 启动共用。"""
    try:
        d = json.loads(RUN_PREFS.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        d = {}
    return {"workers": min(8, max(1, int(d.get("workers", 3) or 3))),
            "target": min(999, max(1, int(d.get("target", 10) or 10)))}


def write_run_prefs(workers, target) -> dict:
    try:
        workers = min(8, max(1, int(workers)))
        target = min(999, max(1, int(target)))
    except (TypeError, ValueError):
        return {"ok": False, "error": "并发/目标须为整数"}
    RUN_PREFS.parent.mkdir(parents=True, exist_ok=True)
    RUN_PREFS.write_text(json.dumps({"workers": workers, "target": target}),
                         encoding="utf-8")
    return {"ok": True, "workers": workers, "target": target}


class Runner:
    """管理并发注册机器人子进程（全局单例）。

    架构：注册 Worker × N 并行 + 领取串行；target 个注册成功即自动截止。
    子进程写 PROGRESS_PATH 实时快照，tail 一并返回给前端渲染。
    """

    def __init__(self) -> None:
        self.proc: subprocess.Popen | None = None
        self.lock = Lock()
        self.mode = ""
        self.started_at = 0.0
        self.workers = 0
        self.target = 0

    def start(self, workers: int | None = None, target: int | None = None,
              region: str | None = None,
              proxy_enabled: bool | None = None) -> dict:
        with self.lock:
            if self.proc and self.proc.poll() is None:
                return {"ok": False, "error": "机器人已在运行"}
            # 常驻收信进程：outlook/hotmail 长连接保持在线（幂等拉起）
            try:
                from app.mail import watcher
                watcher.ensure_watcher()
            except Exception as exc:  # noqa: BLE001
                logging.getLogger("webui").warning(
                    "常驻收信拉起失败（回退旧收信链路）：%s", exc)
            # 未显式传参时用指挥台保存的参数（页面/API 启动一致）
            prefs = read_run_prefs()
            workers = min(8, max(1, int(workers or prefs["workers"])))
            target = min(999, max(1, int(target or prefs["target"])))
            # 启动时固化网络模式：直连→池窗口不配 IP；代理→acquire 时写入。
            # 写回 config.yaml，子进程（run:concurrent）读到的就是本次选择。
            net = ""
            if proxy_enabled is not None:
                w = write_config_fields({"proxy.enabled": bool(proxy_enabled)})
                if "proxy.enabled" not in w["applied"]:
                    return {"ok": False,
                            "error": "网络模式写入失败：" + "; ".join(w["skipped"])}
                net = "代理 IP" if proxy_enabled else "直连"
            RUN_LOG.parent.mkdir(parents=True, exist_ok=True)
            RUN_LOG.write_text("", encoding="utf-8")
            cmd = [sys.executable, "-m", "app.cli", "run:concurrent",
                   "--workers", str(workers), "--total", str(target)]
            if region:
                cmd += ["--region", region]
            self.mode = (f"并发 ×{workers} · 目标 {target}"
                         + (f" · {net}" if net else ""))
            self.workers, self.target = workers, target
            # 初始快照：前端立即看到 0/target，不用等子进程第一笔
            try:
                PROGRESS_PATH.write_text(json.dumps({
                    "ts": time.time(), "target": target, "workers": workers,
                    "registered": 0, "register_failed": 0, "claimed": 0,
                    "claim_failed": 0, "credits": 0, "in_flight": 0, "pending": 0,
                    "finished": False, "stop_reason": "",
                    "worker_states": [
                        {"i": i, "busy": False, "phase": "idle",
                         "ok": 0, "fail": 0, "last": ""}
                        for i in range(workers)],
                }, ensure_ascii=False), encoding="utf-8")
            except Exception:
                pass
            env = {**os.environ,
                   "PYTHONPATH": f"{ROOT};{ROOT / '.pylibs'}",
                   "PYTHONIOENCODING": "utf-8"}
            self.proc = subprocess.Popen(
                cmd, cwd=str(ROOT), env=env,
                stdout=open(RUN_LOG, "ab"), stderr=subprocess.STDOUT)
            self.started_at = time.time()
            return {"ok": True, "pid": self.proc.pid, "mode": self.mode,
                    "workers": workers, "target": target,
                    "net": net, "cmd": " ".join(cmd)}

    def stop(self) -> dict:
        with self.lock:
            if not self.proc or self.proc.poll() is not None:
                return {"ok": True, "note": "没有在跑的进程"}
            pid = self.proc.pid
            try:
                subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"],
                               capture_output=True, timeout=15)
            except Exception:
                pass  # 杀不掉也要尽力回收句柄
            try:
                self.proc.wait(timeout=10)
            except Exception:
                pass
            # 强杀后子进程来不及写终态，这里补上 stopped 标记
            try:
                p = json.loads(PROGRESS_PATH.read_text(encoding="utf-8"))
                p["finished"] = True
                p["stop_reason"] = "stopped"
                PROGRESS_PATH.write_text(json.dumps(p, ensure_ascii=False),
                                         encoding="utf-8")
            except Exception:
                pass
            return {"ok": True, "pid": pid}

    def tail(self, offset: int = 0) -> dict:
        running = bool(self.proc and self.proc.poll() is None)
        try:
            progress = json.loads(PROGRESS_PATH.read_text(encoding="utf-8"))
        except Exception:
            progress = None
        data = b""
        if RUN_LOG.exists():
            size = RUN_LOG.stat().st_size
            if offset < size:
                with RUN_LOG.open("rb") as fh:
                    fh.seek(max(0, offset))
                    data = fh.read()
            return {"running": running, "offset": size, "data": data.decode(
                "utf-8", errors="replace"), "mode": self.mode,
                "pid": self.proc.pid if self.proc else None,
                "workers": self.workers, "target": self.target,
                "progress": progress,
                "exit_code": None if running else (
                    self.proc.returncode if self.proc else None)}
        return {"running": running, "offset": 0, "data": "", "mode": self.mode,
                "pid": None, "workers": self.workers, "target": self.target,
                "progress": progress, "exit_code": None}


RUNNER = Runner()


# ------------------------------------------------------------- HTTP 服务
class Handler(BaseHTTPRequestHandler):
    server_version = "QoderConsole/1.0"

    def log_message(self, fmt, *args):  # 安静
        pass

    def _json(self, obj: dict, code: int = 200) -> None:
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _body(self) -> dict:
        n = int(self.headers.get("Content-Length") or 0)
        if not n:
            return {}
        try:
            return json.loads(self.rfile.read(n).decode("utf-8"))
        except Exception:
            return {}

    def _query(self) -> dict:
        from urllib.parse import parse_qs
        qs = self.path.split("?", 1)[1] if "?" in self.path else ""
        return {k: v[0] for k, v in parse_qs(qs).items()}

    def do_GET(self) -> None:  # noqa: N802
        path = self.path.split("?", 1)[0]
        if path == "/" or path == "/index.html":
            fp = WEB_DIR / "index.html"
            if fp.exists():
                body = fp.read_bytes()
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            self._json({"error": "前端未安装"}, 404)
        elif path == "/api/config":
            self._json(read_config_fields())
        elif path == "/api/status":
            try:
                acc = _stat_accounts()
            except Exception:
                import traceback

                traceback.print_exc()
                acc = {}
            self._json({**acc, **_stat_proxy(),
                        "captcha": _stat_captcha(),
                        "bitbrowser": _stat_bitbrowser()})
        elif path == "/api/run/prefs":
            self._json(read_run_prefs())
        elif path == "/api/run/tail":
            qs = self.path.split("?", 1)[1] if "?" in self.path else ""
            off = 0
            for kv in qs.split("&"):
                if kv.startswith("offset="):
                    try:
                        off = int(kv.split("=", 1)[1])
                    except ValueError:
                        off = 0
            self._json(RUNNER.tail(off))
        elif path == "/api/pools":
            self._json(pool_summary())
        elif path == "/api/bb-pool":
            self._json(bb_pool_status())
        elif path == "/api/pools/list":
            q = self._query()
            kind = q.get("kind", "mail")
            if kind not in POOL_KINDS:
                self._json({"error": "unknown kind"}, 400)
                return
            self._json(pool_list(kind, q.get("q", "")))
        elif path == "/api/pools/export":
            kind = self._query().get("kind", "mail")
            if kind not in POOL_KINDS:
                self._json({"error": "unknown kind"}, 400)
                return
            body = (_pool_path(kind).read_bytes()
                    if _pool_path(kind).exists() else b"")
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Disposition",
                             f'attachment; filename="{kind}_pool.txt"')
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif path == "/api/results":
            self._json(results_summary())
        elif path == "/api/results/file":
            f = self._query().get("f", "")
            if not RESULT_FILE_RE.match(f):
                self._json({"error": "bad filename"}, 400)
                return
            fp = _exports_dir() / f
            if not fp.exists():
                self._json({"error": "not found"}, 404)
                return
            body = fp.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "application/octet-stream")
            self.send_header("Content-Disposition", f'attachment; filename="{f}"')
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif path.startswith("/web/"):
            fp = (WEB_DIR / path[len("/web/"):]).resolve()
            if str(fp).startswith(str(WEB_DIR.resolve())) and fp.exists():
                ctype = {".js": "text/javascript", ".css": "text/css",
                         ".svg": "image/svg+xml", ".html": "text/html",
                         ".png": "image/png", ".ico": "image/x-icon",
                         ".woff2": "font/woff2"}.get(fp.suffix, "application/octet-stream")
                body = fp.read_bytes()
                self.send_response(200)
                self.send_header("Content-Type", f"{ctype}; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            self._json({"error": "not found"}, 404)
        else:
            self._json({"error": "not found"}, 404)

    def do_POST(self) -> None:  # noqa: N802
        path = self.path.split("?", 1)[0]
        body = self._body()
        if path == "/api/config":
            try:
                self._json(write_config_fields(body))
            except Exception as exc:
                self._json({"applied": [], "skipped": [], "error": str(exc)}, 500)
        elif path == "/api/bb-check":
            port = body.get("port")
            if port is not None:
                try:
                    port = int(port)
                except (TypeError, ValueError):
                    self._json({"ok": False, "error": "端口无效"}, 400)
                    return
                if not 1 <= port <= 65535:
                    self._json({"ok": False, "error": "端口须 1-65535"}, 400)
                    return
            self._json(bb_check(port))
        elif path == "/api/bb-pool/prepare":
            try:
                size = int(body.get("size", 4))
            except (TypeError, ValueError):
                self._json({"ok": False, "error": "窗口数须为整数"}, 400)
                return
            self._json(bb_pool_prepare(size))
        elif path == "/api/run":
            if "mode" in body:  # 兼容旧契约：one→1×1，loop→1×count
                workers = 1
                target = int(body.get("count", 1) or 1) if body.get("mode") == "loop" else 1
            else:
                workers = body.get("workers")
                target = body.get("target")
            region = body.get("region") or None
            pe = body.get("proxy_enabled")
            if pe is not None:
                pe = bool(pe)
            self._json(RUNNER.start(workers, target, region, proxy_enabled=pe))
        elif path == "/api/run/stop":
            self._json(RUNNER.stop())
        elif path == "/api/run/prefs":
            self._json(write_run_prefs(body.get("workers"), body.get("target")))
        elif path == "/api/check/captcha":
            self._json(_stat_captcha())
        elif path == "/api/results/export":
            try:
                self._json(results_export(body.get("fmt", "txt")))
            except ValueError as exc:
                self._json({"error": str(exc)}, 400)
        elif path == "/api/results/export_all":
            self._json(results_export_all())
        elif path in ("/api/pools/import", "/api/pools/delete", "/api/pools/clear"):
            kind = body.get("kind", "")
            if kind not in POOL_KINDS:
                self._json({"error": "unknown kind"}, 400)
                return
            if path == "/api/pools/import":
                self._json(pool_import(kind, body.get("text", ""),
                                       body.get("mode", "append")))
            elif path == "/api/pools/delete":
                self._json(pool_delete(kind, body.get("keys", [])))
            else:
                self._json(pool_clear(kind))
        else:
            self._json({"error": "not found"}, 404)


def serve(port: int = 8877) -> None:
    addr = ("127.0.0.1", port)
    httpd = ThreadingHTTPServer(addr, Handler)
    print(f"控制台已启动: http://127.0.0.1:{port}  （Ctrl+C 退出）")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("控制台已退出")


if __name__ == "__main__":
    serve(int(sys.argv[1]) if len(sys.argv) > 1 else 8877)
