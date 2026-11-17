"""领取阶段：启动客户端 → 登录 → 抓 Token → 核对额度。

基于实测的客户端结构（Qoder 0.4.9）：

- 数据目录：``%APPDATA%\\Qoder``
- Token 明文存于 ``SharedClientCache/cache/machine_token.json``
  → **不需要解密、不需要点 UI**
- 客户端默认开 CDP 调试端口，位置在 ``DevToolsActivePort``
  → 可以直接 ``connect_over_cdp`` 接管

因此领取流程简化为：
  1. 启动 Qoder.exe
  2. 等 ``machine_token.json`` 出现且 ``updateAt`` 变新（说明登录生效）
  3. 读出 token 入库
  4. 可选：用 token 调额度接口核对积分

登录动作三种驱动方式，按可用性降级：
  a. 已登录态直接继承（最常见：上一个号退出后新号登录）
  b. CDP 接管 UI 自动填邮箱密码（需 UI 选择器标定）
  c. pywinauto 操作原生窗口（兜底）
"""

from __future__ import annotations

import dataclasses
import json
import os
import re
import subprocess
import time
from datetime import datetime, timedelta
from pathlib import Path

import requests

from app.claim.token_harvester import (
    QoderToken,
    QoderTokenHarvester,
    client_status as qoder_client_status,
    qoder_data_dir,
    qoder_install_dir,
)


@dataclasses.dataclass(slots=True)
class ClaimResult:
    ok: bool
    credits: int = 0
    plan: str = ""
    expire_at: datetime | None = None
    token: dict | None = None
    error: str = ""
    raw: dict | None = None
    token_source: str = ""


class Claimer:
    """领取器。

    参数含义：
    - ``client_path``：Qoder.exe 路径；留空自动探测
    - ``account_data_dir``：客户端数据目录；留空用 ``%APPDATA%\\Qoder``
    - ``wait_credit_timeout``：启动后等 token 出现的秒数
    - ``credit_probe_url``：额度接口（抓包得到才填）；不填则只存 token
    - ``login_mode``：``inherit`` 继承已登录态 / ``cdp`` 走 CDP 自动登录
    - ``relogin_timeout``：等新 token 出现的最长秒数
    """

    def __init__(
        self,
        client_path: str = "",
        account_data_dir: str | Path = "",
        wait_credit_timeout: int = 180,
        capture_token: bool = True,
        credit_probe_url: str | None = None,
        credit_field: str = "credits",
        plan_field: str = "plan",
        expire_field: str = "expireAt",
        remote_debug_port: int = 0,
        login_mode: str = "inherit",
        relogin_timeout: int = 120,
    ) -> None:
        if client_path:
            self.client_path = str(client_path)
            self.client_source = "config"
        else:
            from app.claim.client_path import find_client

            picked, chosen, _cands = find_client()
            self.client_path = str(picked) if picked else ""
            self.client_source = chosen.source if chosen else "not-found"
        self.data_dir = Path(account_data_dir) if account_data_dir else qoder_data_dir()
        self.timeout = wait_credit_timeout
        self.capture_token = capture_token
        self.credit_probe_url = credit_probe_url
        self.credit_field = credit_field
        self.plan_field = plan_field
        self.expire_field = expire_field
        self.debug_port = remote_debug_port
        self.login_mode = login_mode
        self.relogin_timeout = relogin_timeout
        self.harvester = QoderTokenHarvester(self.data_dir)
        self._proc: subprocess.Popen | None = None

    # ---------------------------------------------------------- 客户端控制
    def is_running(self) -> bool:
        if os.name != "nt":
            try:
                out = subprocess.run(["pgrep", "-f", "Qoder"], capture_output=True,
                                     text=True, timeout=10)
                return out.returncode == 0
            except Exception:
                return False
        try:
            # 中文 Windows tasklist 输出 GBK；默认 utf-8 解码会让 stdout 变 None
            out = subprocess.run(["tasklist", "/FI", "IMAGENAME eq Qoder.exe", "/NH"],
                                 capture_output=True, text=True, timeout=15,
                                 encoding="gbk", errors="replace")
            return "Qoder.exe" in (out.stdout or "")
        except Exception:
            return False

    def kill(self, timeout: int = 20) -> bool:
        """结束所有 Qoder 进程（含 helper）。"""
        if self._proc is not None:
            try:
                self._proc.terminate()
                self._proc.wait(timeout=10)
            except Exception:
                pass
            self._proc = None

        if os.name == "nt":
            for name in ("Qoder.exe", "Qoder Helper.exe"):
                try:
                    subprocess.run(["taskkill", "/F", "/IM", name, "/T"],
                                   capture_output=True, text=True, timeout=15)
                except Exception:
                    pass
        else:
            try:
                subprocess.run(["pkill", "-f", "Qoder"], capture_output=True, timeout=15)
            except Exception:
                pass

        for _ in range(timeout):
            if not self.is_running():
                return True
            time.sleep(1)
        return not self.is_running()

    def launch(self, remote_debug: bool = False) -> bool:
        exe = Path(self.client_path)
        if not exe.exists():
            return False
        args = [str(exe)]
        if remote_debug:
            port = self.debug_port or 9222
            args.append(f"--remote-debugging-port={port}")
        try:
            from app.claim.client_path import clean_env

            # 必须剔除 ELECTRON_RUN_AS_NODE：否则 Electron 会以 node 身份跑，
            # 无参数时读 stdin 到 EOF 直接退 0，看起来像"启动后立刻退出"。
            self._proc = subprocess.Popen(
                args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                cwd=str(exe.parent), env=clean_env(),
            )
            return True
        except Exception:
            self._proc = None
            return False

    def wait_started(self, timeout: int = 60) -> bool:
        """等客户端起来（DevToolsActivePort 出现或进程存在）。"""
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self.harvester.read_devtools_port() or self.is_running():
                return True
            time.sleep(1)
        return False

    # ---------------------------------------------------------- Token
    def snapshot_token(self) -> QoderToken | None:
        """取当前 token 快照（用于判断是否换了号）。

        0.3.x：读 ``machine_token.json`` 明文；
        0.4.x：DPAPI 解 ``auth.v1.dat``（走 ``harvest`` 自动降级，不要直接调
        ``harvest_from_file``，否则 0.4.x 永远拿不到 token）。
        """
        return self.harvester.harvest()

    def wait_new_token(self, old_token: str = "",
                       timeout: int | None = None) -> QoderToken | None:
        """等 token 变化（说明新账号登录生效）。

        必须走 ``harvest()`` 全链路降级（machine_token → safe_storage → CDP → leveldb）：
        0.4.x 客户端的 token 在 ``auth.v1.dat``（DPAPI 解密），而
        ``harvest_from_file`` 只认 0.3.x 的明文 JSON —— 直接调它在 0.4.x 上
        永远拿不到 token，整个领取流程会卡在「未抓到 token」。
        """
        timeout = timeout if timeout is not None else self.relogin_timeout
        deadline = time.time() + timeout
        while time.time() < deadline:
            tok = self.harvester.harvest()
            if tok and tok.token != old_token:
                return tok
            time.sleep(2)
        # 超时也返回当前 token（可能是同号重登）
        return self.harvester.harvest()

    # ---------------------------------------------------------- 额度核对
    def probe_credits(self, token: QoderToken | None) -> tuple[int, str, datetime | None]:
        """调额度接口核对积分。未配置接口时返回零值（不影响入库）。"""
        if not self.credit_probe_url or not token:
            return 0, "", None
        headers = {
            "Authorization": f"Bearer {token.token}",
            "Content-Type": "application/json",
            "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                           "AppleWebKit/537.36 (KHTML, like Gecko) "
                           "Chrome/131.0.0.0 Safari/537.36"),
        }
        try:
            r = requests.get(self.credit_probe_url, headers=headers, timeout=20)
            data = r.json()
        except Exception:
            return 0, "", None

        # 兼容几种常见嵌套
        node = data
        for key in ("data", "result", "quota"):
            if isinstance(node, dict) and key in node and isinstance(node[key], dict):
                node = node[key]

        credits = 0
        for k in (self.credit_field, "credits", "balance", "remain", "total"):
            v = node.get(k) if isinstance(node, dict) else None
            if isinstance(v, (int, float)):
                credits = int(v)
                break
        plan = str(node.get(self.plan_field, "") or node.get("planName", "")
                   or node.get("plan", "")) if isinstance(node, dict) else ""
        expire_raw = node.get(self.expire_field) if isinstance(node, dict) else None
        expire = None
        if expire_raw:
            raw = str(expire_raw).strip()
            # 注意必须完整匹配：之前用 raw[:19] 截断，带 Z / +08:00 的
            # ISO 串会被 naive 格式“看起来匹配”截命中，时区信息被丢掉。
            for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
                try:
                    expire = datetime.strptime(raw, fmt)
                    break
                except ValueError:
                    continue
        if expire is None and expire_raw:
            # 带时区/UTC 格式：ISO 8601（Z / +08:00）→ RFC 1123（HTTP 接口
            # 常见的 GMT 格式）。之前只试 naive strptime，遇到这两种会
            # 静默落到 now+14d 兜底，到期预警全错。
            raw = str(expire_raw).strip()
            dt = None
            try:
                dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
            except ValueError:
                try:
                    from email.utils import parsedate_to_datetime

                    dt = parsedate_to_datetime(raw)
                except Exception:  # noqa: BLE001
                    dt = None
            if dt is not None:
                if dt.tzinfo is not None:  # 入库统一 naive 本地时间
                    dt = dt.astimezone().replace(tzinfo=None)
                expire = dt
        return credits, plan, expire

    # ---------------------------------------------------------- 主流程
    def perform(
        self,
        email_addr: str,
        password: str,
        ui_login=None,
    ) -> ClaimResult:
        """完整领取流程。

        ``ui_login`` 可选回调：``fn(client) -> bool``，用于自定义登录动作。
        """
        exe = Path(self.client_path)
        if not exe.exists():
            return ClaimResult(False, error=f"客户端不存在：{exe}")

        # 1. 先看是否已有有效登录态
        before = self.snapshot_token()
        old_token_value = before.token if before else ""

        # 2. 客户端没跑就启动
        started_here = False
        if not self.is_running():
            if not self.launch(remote_debug=self.login_mode == "cdp"):
                return ClaimResult(False, error="客户端启动失败")
            started_here = True
            if not self.wait_started(self.timeout):
                self.kill()
                return ClaimResult(False, error="客户端启动后无响应")

        try:
            token: QoderToken | None = None

            # 3. 走自定义登录（若有）
            if ui_login is not None:
                try:
                    ui_login(self)
                except Exception:
                    pass
                token = self.wait_new_token(old_token_value)
            else:
                # 继承模式：直接用现有 token；若与旧号相同则等待新号登录
                token = self.snapshot_token()
                if token and old_token_value and token.token == old_token_value:
                    # 同号，说明还没换号，等一会
                    token = self.wait_new_token(old_token_value, timeout=15)
                elif token is None:
                    token = self.wait_new_token(old_token_value)

            if token is None:
                return ClaimResult(False, error="未抓到 token（客户端可能未登录）")

            # 4. 核对额度
            credits, plan, expire = self.probe_credits(token)
            if expire is None:
                expire = datetime.now() + timedelta(days=14)

            return ClaimResult(
                ok=True,
                credits=credits,
                plan=plan or "pro_trial",
                expire_at=expire,
                token=(token.as_dict() if self.capture_token else None),
                token_source=token.source,
                raw={"device_id": token.device_id, "version": token.version,
                     "update_dt": token.update_dt.isoformat() if token.update_dt else ""},
                error="" if credits or self.credit_probe_url else "credits_unknown",
            )
        finally:
            # 5. 收尾：默认关掉客户端，下轮重置前必须关
            if started_here or self.login_mode == "inherit":
                self.kill()

    # ---------------------------------------------------------- 状态
    def status(self) -> dict:
        st = qoder_client_status(self.data_dir)
        st["client_path"] = self.client_path
        st["client_exists"] = Path(self.client_path).exists()
        st["running"] = self.is_running()
        return st


# ------------------------------------------------------------ 额度兜底
def apply_credits_fallback(
    cres: "ClaimResult",
    login_state: dict,
    cfg,
    env_name: str = "",
    region: str = "us",
) -> str:
    """perform 没读到额度时的兜底，cli ``claim:run`` 与 orchestrator 领取段共用。

    顺序：① 登录会话里 usage 页已读到（``login_state["credits"]``）→ 直接用；
    ② ``read_credits`` 再开比特浏览器窗口读一次账号页。

    返回实际用到的来源：``""``（本就有额度）/ ``"login-session"`` /
    ``"account-page"``；credits 仍是 0 不算失败，可能只是权益还没到账
    （账号页仍在提示「去客户端领取」）。读到账号页时会把 ``cred.as_dict()``
    放进 ``login_state["_page"]`` 供调用方打印。
    """
    if cres.credits > 0:
        return ""
    if login_state.get("credits"):
        cres.credits = int(login_state["credits"])
        cres.plan = login_state.get("plan") or cres.plan
        return "login-session"

    from app.claim.credit_page import read_credits

    cred = read_credits(cfg, env_name=env_name or "qoder_login", region=region)
    login_state["_page"] = cred.as_dict()
    if cred.source.startswith("usage-page"):
        cres.credits = cred.credits
        if cred.plan and (not cres.plan or cres.plan == "pro_trial"):
            cres.plan = cred.plan
    return "account-page"
