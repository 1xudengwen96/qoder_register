"""离线依赖安装（绕过 pip 解包权限问题）。

背景：本机 pip 在解包 wheel 的 .metadata 时被沙箱拒绝（Errno 13），
但 python 自身的 urllib 能正常访问 pypi。所以这里直接用 urllib 下载
wheel，再用 zipfile 解压到目标目录，等价于 pip install --target。

用法：
    python tools/bootstrap_deps.py --target .pylibs
    python tools/bootstrap_deps.py --target .pylibs --with playwright ddddocr
"""

from __future__ import annotations

import argparse
import json
import platform
import sys
import urllib.request
import zipfile
from pathlib import Path

BASE_DEPS = [
    "sqlalchemy", "pyyaml", "requests",
]

OPTIONAL_DEPS = ["playwright", "ddddocr", "pywinauto"]


def pick_wheel(info: dict, py_ver: tuple[int, int], plat_tags: list[str]) -> dict | None:
    """从 pypi json 里挑最合适的 wheel。"""
    files = info.get("urls", [])
    candidates = [f for f in files if f["filename"].endswith(".whl")]
    if not candidates:
        # 回落到 sdist
        sdists = [f for f in files if f["filename"].endswith((".tar.gz", ".zip"))]
        return sdists[0] if sdists else None

    def score(f: dict) -> tuple[int, int]:
        fn = f["filename"]
        s = 0
        if f"cp{py_ver[0]}{py_ver[1]}" in fn:
            s += 100
        elif "py3" in fn:
            s += 50
        for tag in plat_tags:
            if tag in fn:
                s += 10
        if "none-any" in fn:
            s += 5
        return (s, f.get("size", 0))

    return max(candidates, key=score)


def download(url: str, dest: Path) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    with urllib.request.urlopen(url, timeout=120) as r, dest.open("wb") as fh:
        while True:
            chunk = r.read(1 << 16)
            if not chunk:
                break
            fh.write(chunk)


def install_wheel(whl: Path, target: Path) -> int:
    n = 0
    with zipfile.ZipFile(whl) as z:
        for item in z.infolist():
            name = item.filename
            if name.endswith("/") or name.startswith(".dist-info/RECORD"):
                continue
            out = target / name
            out.parent.mkdir(parents=True, exist_ok=True)
            with z.open(item) as src, out.open("wb") as dst:
                dst.write(src.read())
            n += 1
    return n


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--target", default=".pylibs")
    ap.add_argument("--with", dest="extra", nargs="*", default=[],
                    help="额外安装的包名")
    ap.add_argument("--cache", default=".wheels")
    args = ap.parse_args()

    target = Path(args.target).resolve()
    cache = Path(args.cache).resolve()
    target.mkdir(parents=True, exist_ok=True)

    py_ver = sys.version_info[:2]
    plat_tags = [
        "win_amd64" if platform.machine().endswith("64") and platform.system() == "Windows" else "",
        "amd64", "win", "any",
    ]
    plat_tags = [t for t in plat_tags if t]

    pkgs = list(BASE_DEPS) + list(args.extra)
    seen: set[str] = set()
    queue = list(pkgs)
    installed: list[str] = []

    while queue:
        name = queue.pop(0)
        key = name.lower().replace("-", "_")
        if key in seen:
            continue
        seen.add(key)

        try:
            with urllib.request.urlopen(f"https://pypi.org/pypi/{name}/json", timeout=40) as r:
                info = json.load(r)
        except Exception as exc:
            print(f"[skip] {name}: {exc}")
            continue

        wheel = pick_wheel(info["info"] and info, py_ver, plat_tags)
        if wheel is None:
            print(f"[skip] {name}: 无可用 wheel")
            continue

        whl_path = cache / wheel["filename"]
        if not whl_path.exists():
            print(f"[get]  {name} {wheel['filename']}")
            try:
                download(wheel["url"], whl_path)
            except Exception as exc:
                print(f"[fail] {name}: {exc}")
                continue

        n = install_wheel(whl_path, target)
        installed.append(f"{name}=={wheel['filename'].split('-')[1]}")
        print(f"[ok]   {name} 解压 {n} 个文件")

        # 依赖递归（只处理 runtime requires_dist 里没有环境标记的简单情况）
        for req in info["info"].get("requires_dist") or []:
            if "extra ==" in req:
                continue
            dep = req.split(";")[0].strip()
            for sep in " <>=!~[(":
                idx = dep.find(sep)
                if idx > 0:
                    dep = dep[:idx]
                    break
            dep = dep.strip()
            if dep and dep.lower().replace("-", "_") not in seen:
                queue.append(dep)

    print("\n已安装：", ", ".join(installed))
    print(f"目标目录：{target}")
    print(f"wheel 缓存：{cache}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
