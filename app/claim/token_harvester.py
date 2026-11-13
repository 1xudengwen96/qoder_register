"""Qoder 客户端 Token 抓取（基于实测的本地存储结构）。

实测数据（Qoder 0.4.9，Windows）
--------------------------------
数据目录：``%APPDATA%\\Qoder``（注意不是 Local，是 Roaming）

关键文件：

1. ``SharedClientCache/cache/machine_token.json``  ← **明文 token**
   实测内容：

       {"code":"49efc37e00c7bff3b2",
        "hostname":"PC-20251124AVPW",
        "id":"32303833-3130-422d-b664-34773a35642d",
        "os":"x86_64_windows",
        "token":"P1gAoJlEHAD8zHOYi_022kZP1F-...",
        "type":"431d9d9f91e7d5b7a6",
        "updateAt":1794294237781,
        "version":"1.34.0"}

   ``token`` 字段是明文，直接用，**不需要解密**。
   ``id`` 是与机器绑定的 UUID，``type`` 固定，``code`` 类似设备指纹。

2. ``DevToolsActivePort``  ← 客户端默认开了 CDP 调试端口
   实测内容：

       9228
       /devtools/browser/112dcbf1-5233-4d87-b034-71a3af0b60eb

   第一行是端口，第二行是 browser ws 路径。
   说明可以直接 ``connect_over_cdp("http://127.0.0.1:9228")`` 接管客户端 UI，
   **完全不用 pywinauto 点按钮**。

3. ``Local Storage/leveldb/``  ← 需要时用 plyvel 或 leveldb 库读

4. ``machineid``  ← 设备标识（重置目标）

抓取策略（按可靠性排序）
------------------------
1. 读 ``machine_token.json`` —— 最快最稳，明文
2. 连 ``DevToolsActivePort`` 里的 CDP 端口，从 localStorage / Network 取
3. 扫 leveldb 目录（需要额外依赖）
4. 从 ``Storage/`` 目录读（若存在）

前提：客户端必须已登录过一次。
"""

from __future__ import annotations

import dataclasses
import json
import os
import re
import time
from datetime import datetime
from pathlib import Path


# ============================================================ 路径
def qoder_data_dir() -> Path:
    """Qoder 数据目录（Windows 是 Roaming，不是 Local）。"""
    if os.name == "nt":
        return Path(os.environ.get("APPDATA", Path.home() / "AppData/Roaming")) / "Qoder"
    home = Path.home()
    if os.uname().sysname == "Darwin":  # type: ignore[attr-defined]
        return home / "Library" / "Application Support" / "Qoder"
    return home / ".config" / "Qoder"


def qoder_install_dir() -> Path | None:
    """Qoder 安装目录（找 Qoder.exe）。"""
    if os.name != "nt":
        return None
    cands = [
        Path(os.environ.get("LOCALAPPDATA", "")) / "Programs" / "Qoder",
        Path("C:/Program Files/Qoder"),
        Path("C:/Program Files (x86)/Qoder"),
        Path(os.environ.get("LOCALAPPDATA", "")) / "Qoder",
    ]
    for c in cands:
        if (c / "Qoder.exe").exists():
            return c
    return None


# ============================================================ 数据类
@dataclasses.dataclass(slots=True)
class QoderToken:
    token: str
    device_id: str = ""          # machine_token.json 的 id
    code: str = ""               # 设备指纹码
    token_type: str = ""         # machine_token.json 的 type
    hostname: str = ""
    os_name: str = ""
    version: str = ""
    update_at: int = 0
    source: str = ""             # machine_token / cdp / leveldb
    raw: dict = dataclasses.field(default_factory=dict)

    @property
    def update_dt(self) -> datetime | None:
        if not self.update_at:
            return None
        return datetime.fromtimestamp(self.update_at / 1000)

    def is_fresh(self, max_age_hours: int = 24) -> bool:
        dt = self.update_dt
        if dt is None:
            return False
        return (datetime.now() - dt).total_seconds() < max_age_hours * 3600

    def as_dict(self) -> dict:
        d = dataclasses.asdict(self)
        d["update_dt"] = self.update_dt.isoformat(sep=" ", timespec="seconds") \
            if self.update_dt else None
        return d


@dataclasses.dataclass(slots=True)
class DevToolsInfo:
    port: int
    ws_path: str
    pid: int = 0

    @property
    def cdp_url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    @property
    def ws_url(self) -> str:
        return f"ws://127.0.0.1:{self.port}{self.ws_path}"


# ============================================================ 抓取器
class QoderTokenHarvester:
    """从 Qoder 客户端本地存储抓 token。"""

    TOKEN_FILE = Path("SharedClientCache/cache/machine_token.json")
    DEVTOOLS_FILE = Path("DevToolsActivePort")
    MACHINE_ID_FILE = Path("machineid")

    def __init__(self, data_dir: str | Path | None = None) -> None:
        self.data_dir = Path(data_dir) if data_dir else qoder_data_dir()

    # ---------------------------------------------------------- 1. 主路径
    def harvest_from_file(self) -> QoderToken | None:
        """读 ``machine_token.json``。实测明文，不需要解密。"""
        p = self.data_dir / self.TOKEN_FILE
        if not p.exists():
            return None
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            return None
        tok = data.get("token")
        if not tok:
            return None
        return QoderToken(
            token=tok,
            device_id=str(data.get("id", "")),
            code=str(data.get("code", "")),
            token_type=str(data.get("type", "")),
            hostname=str(data.get("hostname", "")),
            os_name=str(data.get("os", "")),
            version=str(data.get("version", "")),
            update_at=int(data.get("updateAt", 0) or 0),
            source="machine_token",
            raw=data,
        )

    # ---------------------------------------------------------- 2. CDP
    def read_devtools_port(self) -> DevToolsInfo | None:
        """读 ``DevToolsActivePort``：第一行端口，第二行 ws 路径。"""
        p = self.data_dir / self.DEVTOOLS_FILE
        if not p.exists():
            return None
        try:
            lines = [x.strip() for x in p.read_text(encoding="utf-8").splitlines() if x.strip()]
        except Exception:
            return None
        if len(lines) < 2 or not lines[0].isdigit():
            return None
        return DevToolsInfo(port=int(lines[0]), ws_path=lines[1])

    def cdp_alive(self, info: DevToolsInfo, timeout: int = 3) -> bool:
        import urllib.request
        try:
            with urllib.request.urlopen(info.cdp_url + "/json/version", timeout=timeout) as r:
                return r.status == 200
        except Exception:
            return False

    def harvest_from_cdp(self) -> QoderToken | None:
        """连客户端 CDP，从 localStorage 里翻 token。

        适用场景：``machine_token.json`` 被清或格式变了，
        但客户端仍开着调试端口。
        """
        info = self.read_devtools_port()
        if info is None or not self.cdp_alive(info):
            return None
        try:
            from playwright.sync_api import sync_playwright
        except ImportError:
            return None

        try:
            with sync_playwright() as pw:
                browser = pw.chromium.connect_over_cdp(info.cdp_url)
                for ctx in browser.contexts:
                    for page in ctx.pages:
                        try:
                            found = page.evaluate("""() => {
                                const out = {};
                                for (let i = 0; i < localStorage.length; i++) {
                                    const k = localStorage.key(i);
                                    const v = localStorage.getItem(k);
                                    if (k && v && /token|auth|session/i.test(k)) {
                                        out[k] = v;
                                    }
                                }
                                return out;
                            }""")
                        except Exception:
                            continue
                        for k, v in (found or {}).items():
                            m = re.search(r'"(?:token|accessToken)"\s*:\s*"([^"]{20,})"', str(v))
                            token = m.group(1) if m else (v if len(str(v)) > 20 and "{" not in str(v) else "")
                            if token:
                                return QoderToken(token=token, source="cdp",
                                                  raw={"key": k, "device": info.port})
                browser.close()
        except Exception:
            return None
        return None

    # ---------------------------------------------------------- 3. leveldb
    def harvest_from_leveldb(self) -> QoderToken | None:
        """扫 leveldb 目录里的 token 字符串（需要 plyvel，可选）。"""
        d = self.data_dir / "Local Storage" / "leveldb"
        if not d.exists():
            return None
        # 先做无依赖的二进制扫描
        pat = re.compile(rb'"(?:token|accessToken)"\s*:\s*"([A-Za-z0-9_\-\.]{24,})"')
        for f in d.glob("*.ldb"):
            try:
                data = f.read_bytes()
            except Exception:
                continue
            m = pat.search(data)
            if m:
                return QoderToken(token=m.group(1).decode("utf-8", "ignore"),
                                  source="leveldb", raw={"file": f.name})
        for f in d.glob("*.log"):
            try:
                data = f.read_bytes()
            except Exception:
                continue
            m = pat.search(data)
            if m:
                return QoderToken(token=m.group(1).decode("utf-8", "ignore"),
                                  source="leveldb", raw={"file": f.name})
        return None

    # ---------------------------------------------------------- 汇总
    def harvest(self, prefer_fresh: bool = True) -> QoderToken | None:
        """按可靠性依次尝试。"""
        candidates: list[QoderToken] = []
        for fn in (self.harvest_from_file, self.harvest_from_cdp,
                   self.harvest_from_leveldb):
            try:
                t = fn()
            except Exception:
                t = None
            if t:
                candidates.append(t)
        if not candidates:
            return None
        if prefer_fresh:
            fresh = [c for c in candidates if c.is_fresh()]
            if fresh:
                candidates = fresh
        # 明文文件优先，其次 CDP
        order = {"machine_token": 0, "cdp": 1, "leveldb": 2}
        candidates.sort(key=lambda c: order.get(c.source, 9))
        return candidates[0]

    # ---------------------------------------------------------- 设备信息
    def read_machine_id(self) -> str:
        p = self.data_dir / self.MACHINE_ID_FILE
        try:
            return p.read_text(encoding="utf-8").strip() if p.exists() else ""
        except Exception:
            return ""

    def status(self) -> dict:
        """客户端状态自检。"""
        info = self.read_devtools_port()
        tok = self.harvest_from_file()
        return {
            "data_dir": str(self.data_dir),
            "data_dir_exists": self.data_dir.exists(),
            "install_dir": str(qoder_install_dir() or ""),
            "machine_id": self.read_machine_id(),
            "token_file_exists": (self.data_dir / self.TOKEN_FILE).exists(),
            "token_found": tok is not None,
            "token_fresh": tok.is_fresh() if tok else False,
            "token_update": tok.update_dt.isoformat(sep=" ", timespec="seconds")
                            if tok and tok.update_dt else "",
            "devtools_port": info.port if info else None,
            "devtools_alive": self.cdp_alive(info) if info else False,
            "logged_in": tok is not None,
        }


# ============================================================ 便捷函数
def harvest_token(data_dir: str | Path | None = None) -> QoderToken | None:
    return QoderTokenHarvester(data_dir).harvest()


def client_status(data_dir: str | Path | None = None) -> dict:
    return QoderTokenHarvester(data_dir).status()
