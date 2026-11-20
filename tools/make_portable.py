# -*- coding: utf-8 -*-
"""打包 qoder_register 为便携包，可拷到别的 Windows 机器直接跑。

产物：dist/qoder_register/  （再压 zip 同目录）

便携包内容：
- app/ web/ tools/ config/ .pylibs/ requirements.txt
- data/mail_list.txt（邮箱清单，config 改为相对路径引用）
- python-3.12.10-amd64.exe（setup.bat 检测无 Python 时静默安装）
- setup.bat / start.bat / README_便携版.txt

机器相关路径自动改写（只改包内副本，不动本机配置）：
- mail.outlook_file  → data/mail_list.txt（原 Desktop/3.txt 拷入包内）
- claim.client_path  → 留空（Claimer 自动探测目标机的 Qoder 安装位置）

不打包：data/（库/日志/产物/备份）、.wheels、vendor、docs、tests、__pycache__。
"""
import re
import shutil
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DIST = ROOT / "dist"
PKG = DIST / "qoder_register"
MAIL_SRC = Path("C:/Users/Administrator/Desktop/3.txt")
PY_INSTALLER = ROOT / "data" / "python-3.12.10-amd64.exe"

COPY_DIRS = ["app", "web", "tools", "config", ".pylibs"]
EXCLUDE_DIRS = {"__pycache__", "_tmp", "captcha_samples", "captcha_diag",
                "piece_exp", "puzzle_css", "verify_reset", "aliyun_params"}


def copy_tree(src: Path, dst: Path) -> None:
    for item in src.rglob("*"):
        if any(part in EXCLUDE_DIRS for part in item.relative_to(src).parts):
            continue
        if item.is_dir():
            continue
        rel = item.relative_to(src)
        target = dst / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(item, target)


def patch_config(cfg: Path) -> None:
    text = cfg.read_text(encoding="utf-8")
    # 邮箱清单 → 包内相对路径
    text = re.sub(
        r'(?m)^(\s*outlook_file:\s*).*$', r'\1"data/mail_list.txt"', text)
    # 客户端路径 → 留空自动探测（目标机用户名/安装位置可能不同）
    text = re.sub(
        r'(?m)^(\s*client_path:\s*).*$', r'\1""', text)
    cfg.write_text(text, encoding="utf-8")


README = """Qoder Register 便携版（Windows x64）
====================================

一、目标机器需要先装好（不在这包里）：
1. 比特浏览器客户端，登录好账号，打开客户端（默认本地 API 54345 端口）
2. Qoder 桌面客户端（https://qoder.com/download 装好，不用登录）
   - 若安装位置不是默认路径，运行后到前端面板「客户端路径」里指定

二、首次使用：
1. 双击 setup.bat —— 检测并静默安装 Python 3.12.10（约 1 分钟，只装一次）
2. 双击 start.bat —— 启动控制台，浏览器自动打开 http://127.0.0.1:8877
3. 控制台页面上可改邮箱清单、并发、代理等配置

三、自带内容说明：
- data/mail_list.txt  邮箱清单（格式：email----密码----clientId----refreshToken，一行一个）
- config/secrets.yaml 打码平台密钥（2Captcha/超级鹰）——包里含真实密钥，勿外传
- data/  运行时自动生成（数据库/日志/产物），换机不带旧数据即全新开始

四、常见问题：
- 提示「请先运行 setup.bat」→ Python 没装或不在 PATH，重跑 setup.bat
- 打不开控制台 → 检查比特浏览器是否在跑（http://127.0.0.1:54345）
- 中文乱码 → start.bat 已设 PYTHONIOENCODING=utf-8，用 cmd 而非老式终端
"""

SETUP_BAT = r"""@echo off
chcp 65001 >nul
cd /d %~dp0
where python >nul 2>nul
if %errorlevel%==0 (
  python -c "import sys; sys.exit(0 if sys.version_info[:2]==(3,12) else 1)" >nul 2>nul
  if %errorlevel%==0 (
    echo Python 3.12 已就绪，无需安装。
    pause
    exit /b 0
  )
  echo 检测到非 3.12 版本的 Python，本包依赖需要 3.12，开始安装...
)
if not exist "python-3.12.10-amd64.exe" (
  echo 缺少 python-3.12.10-amd64.exe，请从原机器 data 目录拷来再运行。
  pause
  exit /b 1
)
echo 静默安装 Python 3.12.10（约 1 分钟，请稍候）...
python-3.12.10-amd64.exe /quiet InstallAllUsers=0 PrependPath=1 Include_test=0
if %errorlevel%==0 (
  echo 安装完成。请【关闭本窗口并新开一个窗口】再运行 start.bat（刷新 PATH）。
) else (
  echo 安装失败，退出码 %errorlevel%。请手动双击 python-3.12.10-amd64.exe 安装。
)
pause
"""

START_BAT = r"""@echo off
chcp 65001 >nul
cd /d %~dp0
set "PYTHONPATH=%~dp0;%~dp0.pylibs"
set PYTHONIOENCODING=utf-8
where python >nul 2>nul
if not %errorlevel%==0 (
  echo 未检测到 Python，请先运行 setup.bat
  pause
  exit /b 1
)
echo 启动控制台 http://127.0.0.1:8877 （Ctrl+C 停止）
start "" http://127.0.0.1:8877
python -m app.cli web --port 8877
pause
"""


def main() -> None:
    if PKG.exists():
        shutil.rmtree(PKG)
    PKG.mkdir(parents=True)

    for d in COPY_DIRS:
        print(f"copy {d}/ ...")
        copy_tree(ROOT / d, PKG / d)
    shutil.copy2(ROOT / "requirements.txt", PKG / "requirements.txt")

    # 邮箱清单
    (PKG / "data").mkdir()
    if MAIL_SRC.exists():
        shutil.copy2(MAIL_SRC, PKG / "data" / "mail_list.txt")
        print("mail list copied -> data/mail_list.txt")
    else:
        print(f"警告：邮箱清单 {MAIL_SRC} 不存在，包内 data/mail_list.txt 需手动放入")

    # Python 安装器（27MB，目标机无 Python 时用）
    if PY_INSTALLER.exists():
        shutil.copy2(PY_INSTALLER, PKG / "python-3.12.10-amd64.exe")
    else:
        print("警告：缺 python-3.12.10-amd64.exe，setup.bat 无法自动装 Python")

    patch_config(PKG / "config" / "config.yaml")

    (PKG / "setup.bat").write_text(SETUP_BAT, encoding="gbk")
    (PKG / "start.bat").write_text(START_BAT, encoding="gbk")
    (PKG / "README_便携版.txt").write_text(README, encoding="utf-8")

    zip_path = DIST / "qoder_register_portable.zip"
    print("zipping ...")
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for f in PKG.rglob("*"):
            if f.is_file():
                zf.write(f, f.relative_to(DIST))
    size_mb = zip_path.stat().st_size / 1024 / 1024
    print(f"完成：{zip_path}（{size_mb:.1f} MB）")
    print(f"目录版：{PKG}")


if __name__ == "__main__":
    sys.exit(main())
