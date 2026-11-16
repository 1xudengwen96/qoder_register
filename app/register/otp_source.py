"""验证码邮件提供器：给 CDP 注册流程喂 6 位码。

两条来源，按可用性自动选：

1. **Outlook 清单文件**（``mail.outlook_file`` → ``email----密码----clientId----refreshToken``）
   —— 与 ``tools/run_signup_cdp.py`` 用的是同一份清单，OAuth2 刷 token 后走 IMAP，
   同时扫 ``INBOX`` 和 ``Junk``（Qoder 的验证邮件实测会进垃圾箱）。
2. **邮箱池聚合层**（``app.mail.factory.build_mail_layer``）
   —— 走数据库里的身份池，用 ``wait_for_mail`` + ``extract_code``。

任何一条来源取不到码都返回空串，由上层判失败，不影响流程继续跑别的号。
"""

from __future__ import annotations

import dataclasses
import logging
import time
from pathlib import Path
from typing import Callable

log = logging.getLogger("otp")


@dataclasses.dataclass(slots=True)
class CodeSource:
    """可调用的取码器；``close()`` 由上层在 finally 里调。"""

    provider: Callable[[], str]
    kind: str
    target: str = ""
    _closer: Callable[[], None] | None = None

    def __call__(self) -> str:
        return self.provider()

    def close(self) -> None:
        if self._closer:
            try:
                self._closer()
            except Exception:  # noqa: BLE001
                pass
            self._closer = None


def _find_account(accounts, email: str):
    target = email.strip().lower()
    local = target.split("@")[0]
    for a in accounts:
        if a.email.lower() == target:
            return a
    # +tag 归一化兜底：qxxx+abc@outlook.com 与 qxxx@outlook.com 视为同一主号
    for a in accounts:
        if a.email.lower().split("@")[0] == local.split("+")[0]:
            return a
    return None


def outlook_code_source(
    path: str | Path,
    email: str,
    *,
    timeout: int = 240,
    poll: int = 4,
    imap_proxy: str | None = None,
) -> CodeSource | None:
    """从 Outlook 清单文件造取码器。清单不存在/没有该地址时返回 None。"""
    from app.mail.outlook_oauth import OutlookMailbox, load_accounts
    from app.register.qoder_signup_cdp import wait_code_sync

    accounts = load_accounts(path)
    if not accounts:
        log.warning("邮箱清单为空或不存在：%s", path)
        return None
    acc = _find_account(accounts, email)
    if acc is None:
        log.warning("清单里没有 %s", email)
        return None

    box = OutlookMailbox(acc, proxy=imap_proxy)
    try:
        box.refresh_access_token()
        box.connect()
        box.select("INBOX")
        baseline = {u.decode() if isinstance(u, bytes) else str(u)
                    for u in box.list_uids()}
    except Exception as exc:  # noqa: BLE001
        log.warning("邮箱连接失败 %s：%s: %s", email, type(exc).__name__, exc)
        try:
            box.close()
        except Exception:  # noqa: BLE001
            pass
        return None

    def _provider() -> str:
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                code = wait_code_sync(box, timeout=min(60, timeout), poll=poll,
                                      baseline_uids=baseline)
            except Exception as exc:  # noqa: BLE001
                log.warning("取码异常：%s: %s", type(exc).__name__, exc)
                code = ""
            if code:
                return code
            # IMAP 空闲断连是常态，断了就重连
            try:
                box.connect()
                box.select("INBOX")
            except Exception:  # noqa: BLE001
                time.sleep(poll)
        return ""

    return CodeSource(_provider, kind="outlook_file", target=email,
                      _closer=box.close)


def layer_code_source(
    mail_layer,
    ident,
    *,
    timeout: int = 240,
    poll: int = 6,
) -> CodeSource | None:
    """从邮箱池聚合层造取码器。"""
    if mail_layer is None or ident is None:
        return None
    from app.mail.code_extract import extract_code, is_qoder_mail
    from app.mail.base import MailMessage

    deadline = time.time() + timeout
    seen: set[str] = set()

    def _match(msg: MailMessage) -> bool:
        if msg.uid in seen:
            return False
        return is_qoder_mail(msg.sender, msg.subject, msg.body_text)

    def _provider() -> str:
        while time.time() < deadline:
            try:
                msg = mail_layer.wait_for_mail(
                    ident, timeout=min(60, timeout), match=_match,
                    poll_interval=poll,
                )
            except Exception as exc:  # noqa: BLE001
                log.warning("池子取信异常：%s: %s", type(exc).__name__, exc)
                msg = None
            if msg is None:
                continue
            seen.add(msg.uid)
            code = extract_code(msg.body_html or msg.body_text)
            if not code:
                code = extract_code(f"{msg.subject}\n{msg.body_html}")
            if code:
                return code
        return ""

    return CodeSource(_provider, kind="mail_pool",
                      target=getattr(ident, "address", ""))


def make_code_source(
    cfg=None,
    *,
    email: str = "",
    ident=None,
    mail_layer=None,
    timeout: int = 240,
    imap_proxy: str | None = None,
) -> CodeSource | None:
    """按优先级造取码器：清单文件 > 邮箱池。"""
    path = ""
    try:
        from app import secrets
        path = str(secrets.get("mail.outlook_file", "") or "")
    except Exception:  # noqa: BLE001
        path = ""
    if not path and cfg is not None:
        from app.config import get
        path = str(get(cfg, "mail.outlook_file", "") or "")

    if path and email and Path(path).exists():
        src = outlook_code_source(path, email, timeout=timeout,
                                  imap_proxy=imap_proxy)
        if src:
            return src
    return layer_code_source(mail_layer, ident, timeout=timeout)
