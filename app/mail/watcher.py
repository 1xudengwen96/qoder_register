"""共用收信进程：outlook/hotmail 常驻 IMAP 长连接 + 验证码命中事件落库。

为什么常驻（2026-11-19 用户设计）：旧链路每轮注册临时刷 OAuth + TLS 握手 +
全箱基线扫描，慢且黑盒。常驻进程在自检/启动时拉起，为 CSV 里每个账号维持
一条 IMAP 长连接，每 3s 一次轻量 UID SEARCH，只 fetch 新信；命中 Qoder
验证码即写 ``mail_code_events``，注册流程的取码器只等事件（1s 粒度）。
资源账：每账号一条长连接（几 MB 内存）+ 每几秒一次 SEARCH，远低于每轮重建。

连接每 30 分钟主动重建一次（OAuth access token 约 1h 过期）；异常 5s 退避
重连。INBOX 与 Junk 都扫（Qoder 验证邮件实测会进垃圾箱）。
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
PID_PATH = ROOT / "data" / ".mail_watch.pid"
LOG_PATH = ROOT / "data" / "logs" / "mail_watch.log"
POLL = 3.0
RECONNECT_EVERY = 1800.0

log = logging.getLogger("mail-watch")


def _pid_alive(pid: int) -> bool:
    if not pid:
        return False
    if os.name == "nt":
        try:
            import ctypes
            h = ctypes.windll.kernel32.OpenProcess(0x1000, False, pid)
            if not h:
                return False
            ctypes.windll.kernel32.CloseHandle(h)
            return True
        except Exception:  # noqa: BLE001
            return False
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def _read_pid() -> int:
    try:
        return int(json.loads(PID_PATH.read_text(encoding="utf-8")).get("pid", 0))
    except (OSError, ValueError):
        return 0


def watcher_alive() -> bool:
    return _pid_alive(_read_pid())


def covers(address: str) -> bool:
    """该地址是否在常驻进程的清单里（不在则取码器走旧链路）。"""
    from app.config import load_config, get
    from app.mail.hotmail import HotmailProvider

    cfg = load_config()
    path = get(cfg, "mail.hotmail.credentials_file", "data/hotmail_accounts.csv")
    rows = HotmailProvider(ROOT / path if not Path(path).is_absolute()
                           else path).rows
    return address.strip().lower() in {r["email"].lower() for r in rows}


def ensure_watcher() -> int:
    """已在跑返回 pid；否则 detached 拉起一个，等它就绪（最多 8s）。"""
    if watcher_alive():
        return _read_pid()
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join(
        [str(ROOT), str(ROOT / ".pylibs"), env.get("PYTHONPATH", "")])
    env["PYTHONIOENCODING"] = "utf-8"
    flags = 0
    if os.name == "nt":
        flags = 0x00000008 | 0x00000200   # DETACHED_PROCESS | NEW_PROCESS_GROUP
    with LOG_PATH.open("ab") as fh:
        subprocess.Popen(
            [sys.executable, "-m", "app.cli", "mail:watch"],
            cwd=str(ROOT), env=env, stdout=fh, stderr=fh,
            stdin=subprocess.DEVNULL, creationflags=flags)
    for _ in range(16):
        time.sleep(0.5)
        if watcher_alive():
            return _read_pid()
    return 0


def stop_watcher() -> int:
    pid = _read_pid()
    if pid and _pid_alive(pid):
        subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"],
                       capture_output=True)
    try:
        PID_PATH.unlink()
    except OSError:
        pass
    return pid


# ------------------------------------------------------------ 单账号长连接
def _watch_account(row: dict, stop: threading.Event) -> None:
    from app.db import session_scope
    from app.mail.code_extract import extract_code, is_qoder_mail
    from app.mail.outlook_file import _to_message
    from app.mail.outlook_oauth import MailAccount, OutlookMailbox
    from app.models import MailCodeEvent

    addr = row["email"].strip().lower()
    acc = MailAccount(email=row["email"], password=row.get("password") or "",
                      client_id=row.get("client_id") or "",
                      refresh_token=row.get("refresh_token") or "")
    if not acc.has_oauth:
        log.warning("%s 缺 OAuth 凭证，跳过（Outlook 已禁 Basic）", addr)
        return
    while not stop.is_set():
        box = None
        try:
            box = OutlookMailbox(acc, host=row.get("imap_host")
                                 or "outlook.office365.com")
            box.refresh_access_token()
            box.connect()
            seen: set[str] = set()
            for folder in ("INBOX", "Junk"):
                try:
                    box.select(folder)
                    seen |= {u.decode() if isinstance(u, bytes) else str(u)
                             for u in box.list_uids()}
                except Exception:  # noqa: BLE001
                    continue
            log.info("%s 长连接就绪（基线 %d 封）", addr, len(seen))
            born = time.time()
            while not stop.is_set() and time.time() - born < RECONNECT_EVERY:
                for folder in ("INBOX", "Junk"):
                    try:
                        box.select(folder)
                        uids = box.list_uids()
                    except Exception:  # noqa: BLE001
                        continue
                    for u in uids:
                        key = u.decode() if isinstance(u, bytes) else str(u)
                        if key in seen:
                            continue
                        seen.add(key)
                        try:
                            msg = box.fetch(u)
                        except Exception:  # noqa: BLE001
                            continue
                        if msg is None:
                            continue
                        norm = _to_message(msg, folder)
                        if not is_qoder_mail(norm.sender, norm.subject,
                                             norm.body_text):
                            continue
                        code = extract_code(norm.body_html or norm.body_text)
                        if not code:
                            code = extract_code(
                                f"{norm.subject}\n{norm.body_html}")
                        if not code:
                            continue
                        with session_scope() as s:
                            dup = (s.query(MailCodeEvent)
                                   .filter(MailCodeEvent.address == addr,
                                           MailCodeEvent.uid == key).first())
                            if dup:
                                continue
                            s.add(MailCodeEvent(
                                address=addr, uid=key, folder=folder,
                                code=code, ts=time.time()))
                        log.info("%s 命中验证码 code=%s folder=%s uid=%s",
                                 addr, code, folder, key)
                stop.wait(POLL)
        except Exception as exc:  # noqa: BLE001
            log.warning("%s 收信异常 %s: %s（5s 后重连）",
                        addr, type(exc).__name__, exc)
        finally:
            if box is not None:
                try:
                    box.close()
                except Exception:  # noqa: BLE001
                    pass
        stop.wait(5.0)


# ------------------------------------------------------------ 主进程
def run_watcher() -> None:
    from app.config import load_config, get
    from app.db import init_engine, create_all
    from app.mail.hotmail import HotmailProvider

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
        handlers=[logging.FileHandler(LOG_PATH, encoding="utf-8"),
                  logging.StreamHandler()])
    cfg = load_config()
    init_engine(get(cfg, "storage.url", "sqlite:///data/qoder.db"))
    create_all()
    PID_PATH.parent.mkdir(parents=True, exist_ok=True)
    PID_PATH.write_text(json.dumps({"pid": os.getpid(), "ts": time.time()}),
                        encoding="utf-8")
    path = get(cfg, "mail.hotmail.credentials_file", "data/hotmail_accounts.csv")
    rows = HotmailProvider(ROOT / path if not Path(path).is_absolute()
                           else path).rows
    oauth_rows = [r for r in rows if r.get("client_id") and r.get("refresh_token")]
    log.info("常驻收信启动 pid=%d 账号 %d（OAuth %d）",
             os.getpid(), len(rows), len(oauth_rows))
    stop = threading.Event()
    threads = [threading.Thread(target=_watch_account, args=(r, stop),
                                daemon=True, name=f"watch-{r['email']}")
               for r in oauth_rows]
    for t in threads:
        t.start()
    try:
        while True:
            time.sleep(10)
            PID_PATH.write_text(
                json.dumps({"pid": os.getpid(), "ts": time.time()}),
                encoding="utf-8")
    except KeyboardInterrupt:
        stop.set()