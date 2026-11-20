# -*- coding: utf-8 -*-
"""便携包启动器：中文提示全在这（.bat 纯 ASCII 防乱码）。

- 校验 Python 3.12
- 起 web 控制台；若异常退出（退出码非 0），自动跑模块自检
  portable_diag.py 定位是哪个原生依赖崩溃（硬崩不打 traceback，
  只能靠逐个 import 二分）
"""
import subprocess
import sys
import threading
import time
import webbrowser
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PORT = 8877


def open_browser_later() -> None:
    time.sleep(10)
    try:
        webbrowser.open(f"http://127.0.0.1:{PORT}")
    except Exception:  # noqa: BLE001
        pass


def main() -> int:
    if sys.version_info[:2] != (3, 12):
        print(f"当前 Python 是 {sys.version_info[0]}.{sys.version_info[1]}，"
              "本包依赖需要 3.12。")
        print("请先运行 setup.bat 安装 3.12；装完后【关闭所有窗口、新开窗口】"
              "再运行 start.bat（刷新 PATH）。")
        return 1
    print(f"Python：{sys.executable}")
    print("正在启动控制台（首次约 10 秒）…")
    print(f"服务就绪后浏览器会自动打开 http://127.0.0.1:{PORT}")
    print("停止服务：关闭本窗口或按 Ctrl+C")
    threading.Thread(target=open_browser_later, daemon=True).start()
    rc = subprocess.call(
        [sys.executable, "-m", "app.cli", "web", "--port", str(PORT)],
        cwd=ROOT)
    print(f"控制台退出码：{rc}")
    if rc != 0:
        print("控制台异常退出，开始逐模块自检（找崩溃的依赖）…")
        diag = ROOT / "tools" / "portable_diag.py"
        subprocess.call([sys.executable, str(diag)], cwd=ROOT)
    return rc


if __name__ == "__main__":
    sys.exit(main())
