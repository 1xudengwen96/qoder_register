"""客户端可执行文件自动探测：不依赖任何写死的机器路径。

为什么要单独一层
----------------
Qoder 在 Windows 上至少有四种落法，都是实测见过的：

1. ``%LOCALAPPDATA%\\Qoder\\Qoder Launcher\\Qoder Launcher.exe``
   —— 启动器，内部再拉起 ``.qoder-versions\\<ver>\\Qoder.exe``（真正写
   ``%APPDATA%\\Qoder\\SharedClientCache\\cache\\machine_token.json`` 的那个）
2. ``%LOCALAPPDATA%\\Programs\\Qoder\\Qoder.exe``
   —— 同目录的 Electron 本体（也可直接跑）
3. ``%LOCALAPPDATA%\\Programs\\Qoder IDE\\Qoder IDE.exe`` / ``E:\\Qoder IDE\\...``
   —— VS Code 系的编辑器，属于另一个产品
4. 协议处理器注册表项
   ``HKCU\\Software\\Classes\\qoder\\shell\\open\\command``
   —— 系统里"谁处理 qoder:// 链接"，最权威，但要防着被别的产品抢占

所以顺序是：**显式覆盖 → 注册表 → 已知布局 → 浅扫盘符**，每个候选打分，
选分最高的；同时把整张候选表打出来，出问题能一眼看出为什么选它。

``ELECTRON_RUN_AS_NODE`` 的坑
-----------------------------
本机环境里如果有 ``ELECTRON_RUN_AS_NODE=1``，所有 Electron 程序都会以 node 身份
运行：``--version`` 打印 ``v24.x``，无参数时读 stdin 到 EOF 直接退 0。
启动时必须把该变量从子进程环境里剔除，否则"客户端启动后立刻退出"会查半天。
"""

from __future__ import annotations

import dataclasses
import os
import platform
import re
from pathlib import Path

ELECTRON_NODE_FLAG = "ELECTRON_RUN_AS_NODE"

# qoder:// 与 qoder-ide:// 两个协议都可能注册
PROTOCOLS = ("qoder", "qoder-ide", "qoder-cn")

# 明显不是 GUI 主程序的文件名
BAD_NAME_RE = re.compile(
    r"(uninstall|unins\d*|elevate|runtime-info|crashpad|updater|update\.exe|"
    r"node\.exe|helper\.exe|ffmpeg|chrome_proxy)", re.I)


@dataclasses.dataclass(slots=True)
class Candidate:
    path: Path
    source: str          # override / registry:qoder / uninstall / layout:xxx / scan
    score: int = 0
    notes: list[str] = dataclasses.field(default_factory=list)

    @property
    def exists(self) -> bool:
        return self.path.is_file()

    def as_dict(self) -> dict:
        return {"path": str(self.path), "source": self.source,
                "score": self.score, "exists": self.exists,
                "notes": self.notes}


# ------------------------------------------------------------ 环境清理
def clean_env(env: dict[str, str] | None = None) -> dict[str, str]:
    """去掉 ELECTRON_RUN_AS_NODE，否则 Electron 会当 node 跑。"""
    out = dict(os.environ if env is None else env)
    out.pop(ELECTRON_NODE_FLAG, None)
    return out


# ------------------------------------------------------------ 打分
def _has_electron_payload(exe: Path) -> bool:
    """同目录有 resources/app(.asar) 或 *.pak —— 说明是 Electron 应用本体。"""
    d = exe.parent
    if (d / "resources" / "app").exists() or (d / "resources" / "app.asar").exists():
        return True
    return any(d.glob("*.pak"))


def _is_version_payload(exe: Path) -> bool:
    """`.qoder-versions/0.5.7/Qoder.exe` 这种是版本载荷，优先用 launcher。"""
    parts = {p.lower() for p in exe.parts}
    return ".qoder-versions" in parts


def score_candidate(exe: Path) -> tuple[int, list[str]]:
    score, notes = 0, []
    name = exe.name.lower()
    if BAD_NAME_RE.search(name):
        return -100, ["疑似非主程序"]
    size_mb = 0
    try:
        size_mb = exe.stat().st_size / 1048576
    except OSError:
        return -100, ["无法读取文件"]

    is_launcher = "launcher" in name
    if size_mb > 50:
        score += 20
        notes.append(f"{size_mb:.0f}MB")
    elif is_launcher:
        # 启动器本来就是几十 KB 的壳，体积小不代表有问题
        score += 10
        notes.append(f"启动器 {size_mb:.2f}MB")
    elif size_mb < 5:
        return -100, [f"体积过小 {size_mb:.1f}MB"]

    if _has_electron_payload(exe):
        score += 30
        notes.append("Electron 载荷齐备")
    if is_launcher:
        score += 40
        notes.append("启动器入口")
    if _is_version_payload(exe):
        score -= 15
        notes.append("版本载荷(优先用 launcher)")
    if "ide" in name:
        score -= 5
        notes.append("IDE 系产品")
    return score, notes


# ------------------------------------------------------------ 注册表
def _registry_command(protocol: str) -> str:
    """读 HKCU/HKLM 的协议处理器命令行，失败返回空串。"""
    if os.name != "nt":
        return ""
    try:
        import winreg
    except ImportError:
        return ""
    paths = [
        (winreg.HKEY_CURRENT_USER, rf"Software\Classes\{protocol}\shell\open\command"),
        (winreg.HKEY_LOCAL_MACHINE, rf"Software\Classes\{protocol}\shell\open\command"),
    ]
    for root, sub in paths:
        try:
            with winreg.OpenKey(root, sub) as k:
                val, _ = winreg.QueryValueEx(k, "")
                if val:
                    return str(val)
        except OSError:
            continue
    return ""


_EXE_IN_CMD_RE = re.compile(r'"([^"]+\.exe)"|(\S+\.exe)', re.I)


def exe_from_command(cmd: str) -> Path | None:
    """从 `"E:\\Qoder IDE\\Qoder IDE.exe" --open-url -- "%1"` 里抠出 exe。"""
    if not cmd:
        return None
    m = _EXE_IN_CMD_RE.search(cmd)
    if not m:
        return None
    raw = m.group(1) or m.group(2)
    raw = raw.strip().strip('"')
    try:
        return Path(raw)
    except Exception:  # noqa: BLE001
        return None


def _uninstall_candidates() -> list[Candidate]:
    if os.name != "nt":
        return []
    try:
        import winreg
    except ImportError:
        return []

    out: list[Candidate] = []
    roots = [
        (winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\CurrentVersion\Uninstall"),
        (winreg.HKEY_LOCAL_MACHINE, r"Software\Microsoft\Windows\CurrentVersion\Uninstall"),
        (winreg.HKEY_LOCAL_MACHINE,
         r"Software\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall"),
    ]
    for root, sub in roots:
        try:
            with winreg.OpenKey(root, sub) as k:
                count = winreg.QueryInfoKey(k)[0]
                for i in range(count):
                    try:
                        name = winreg.EnumKey(k, i)
                        with winreg.OpenKey(k, name) as sk:
                            try:
                                disp = str(winreg.QueryValueEx(sk, "DisplayName")[0])
                            except OSError:
                                continue
                            if "qoder" not in disp.lower():
                                continue
                            for val in ("DisplayIcon", "InstallLocation"):
                                try:
                                    raw = str(winreg.QueryValueEx(sk, val)[0])
                                except OSError:
                                    continue
                                p = exe_from_command(raw) or Path(raw)
                                if p.suffix.lower() == ".exe":
                                    out.append(Candidate(p, f"uninstall:{disp[:24]}"))
                                elif p.is_dir():
                                    for child in p.glob("*Qoder*.exe"):
                                        out.append(Candidate(child, f"uninstall-dir:{disp[:20]}"))
                    except OSError:
                        continue
        except OSError:
            continue
    return out


# ------------------------------------------------------------ 已知布局
def _layout_candidates() -> list[Candidate]:
    out: list[Candidate] = []
    home = Path.home()

    if os.name == "nt":
        local = Path(os.environ.get("LOCALAPPDATA", home / "AppData/Local"))
        roaming = Path(os.environ.get("APPDATA", home / "AppData/Roaming"))
        pf = Path(os.environ.get("PROGRAMFILES", r"C:\Program Files"))
        pf86 = Path(os.environ.get("PROGRAMFILES(X86)", r"C:\Program Files (x86)"))

        explicit = [
            local / "Qoder" / "Qoder Launcher" / "Qoder Launcher.exe",
            local / "Programs" / "Qoder" / "Qoder Launcher.exe",
            local / "Programs" / "Qoder" / "Qoder.exe",
            local / "Programs" / "Qoder IDE" / "Qoder IDE.exe",
            pf / "Qoder" / "Qoder.exe",
            pf86 / "Qoder" / "Qoder.exe",
            pf / "Qoder IDE" / "Qoder IDE.exe",
            pf86 / "Qoder IDE" / "Qoder IDE.exe",
            roaming / "Qoder" / "Qoder.exe",
        ]
        for p in explicit:
            out.append(Candidate(p, "layout"))

        # .qoder-versions/<ver>/Qoder.exe，取版本号最大的
        vroot = local / "Programs" / "Qoder" / ".qoder-versions"
        if vroot.is_dir():
            vers = []
            for d in vroot.iterdir():
                if not d.is_dir():
                    continue
                m = re.match(r"(\d+(?:\.\d+)*)", d.name)
                if m:
                    vers.append((tuple(int(x) for x in m.group(1).split(".")), d))
            for _, d in sorted(vers, reverse=True):
                exe = d / "Qoder.exe"
                if exe.exists():
                    out.append(Candidate(exe, "layout:versions"))
                    break

        # Programs 下任何 Qoder* 目录
        prog = local / "Programs"
        if prog.is_dir():
            for d in prog.glob("*Qoder*"):
                if d.is_dir():
                    for exe in list(d.glob("*.exe"))[:4]:
                        out.append(Candidate(exe, "layout:programs-glob"))
        return out

    if platform.system() == "Darwin":
        apps = [Path("/Applications/Qoder.app/Contents/MacOS/Qoder"),
                home / "Applications/Qoder.app/Contents/MacOS/Qoder",
                Path("/Applications/Qoder IDE.app/Contents/MacOS/Qoder IDE")]
        for p in apps:
            out.append(Candidate(p, "layout:mac"))
        return out

    for p in (Path("/usr/bin/qoder"), Path("/usr/local/bin/qoder"),
              Path("/opt/Qoder/qoder"), home / ".local/bin/qoder"):
        out.append(Candidate(p, "layout:linux"))
    return out


# ------------------------------------------------------------ 盘符浅扫
def _scan_roots(max_depth: int = 3) -> list[Candidate]:
    """盘符根下找 *Qoder* 目录（Windows 专用，深度受限，避免全盘扫描）。"""
    if os.name != "nt":
        return []
    out: list[Candidate] = []
    for letter in "CDEFGH":
        root = Path(f"{letter}:\\")
        if not root.exists():
            continue
        try:
            for d in root.iterdir():
                if not d.is_dir() or "qoder" not in d.name.lower():
                    continue
                for exe in d.glob("*Qoder*.exe"):
                    out.append(Candidate(exe, "scan"))
                if max_depth > 1:
                    for sub in d.iterdir():
                        if sub.is_dir():
                            for exe in sub.glob("*.exe"):
                                if "qoder" in exe.name.lower():
                                    out.append(Candidate(exe, "scan"))
        except (OSError, PermissionError):
            continue
    return out


# ------------------------------------------------------------ 主入口
def candidates(override: str = "", include_scan: bool = True) -> list[Candidate]:
    """按优先级收集全部候选，去重后按分数降序。"""
    out: list[Candidate] = []
    if override:
        out.append(Candidate(Path(override), "override"))

    if os.name == "win32":
        cli = os.environ.get("QODER_CLIENT_PATH", "")
        if cli:
            out.append(Candidate(Path(cli), "env"))

    for proto in PROTOCOLS:
        p = exe_from_command(_registry_command(proto))
        if p:
            out.append(Candidate(p, f"registry:{proto}"))
        if os.name != "win32":
            break

    out.extend(_uninstall_candidates())
    out.extend(_layout_candidates())
    if include_scan:
        out.extend(_scan_roots())

    seen: set[str] = set()
    ranked: list[Candidate] = []
    for c in out:
        key = str(c.path).lower()
        if key in seen:
            continue
        seen.add(key)
        c.score, c.notes = score_candidate(c.path) if c.exists else (-100, ["不存在"])
        if c.source.startswith(("override", "env")):
            c.score += 1000
        elif c.source.startswith("registry"):
            c.score += 200
        ranked.append(c)
    return sorted(ranked, key=lambda x: x.score, reverse=True)


def find_client(override: str = "", include_scan: bool = True) -> tuple[Path | None, Candidate | None, list[Candidate]]:
    """返回 (选中路径, 候选信息, 全部候选)。找不到则 (None, None, 候选表)。"""
    cands = candidates(override=override, include_scan=include_scan)
    for c in cands:
        if c.exists and c.score > 0:
            return c.path, c, cands
    return None, None, cands


def describe(override: str = "") -> str:
    """给 CLI 打印用。"""
    path, chosen, cands = find_client(override=override)
    lines = []
    if chosen:
        lines.append(f"选中: {path}")
        lines.append(f"      来源={chosen.source} 分数={chosen.score} {' '.join(chosen.notes)}")
    else:
        lines.append("选中: 未找到任何可用客户端")
    lines.append("")
    lines.append("候选表:")
    for c in cands[:15]:
        flag = "OK " if c.exists and c.score > 0 else "-- "
        lines.append(f"  {flag}{c.score:5d}  {c.source:24s} {c.path}")
        if c.notes:
            lines.append(f"              {' / '.join(c.notes)}")
    return "\n".join(lines)
