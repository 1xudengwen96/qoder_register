@echo off
REM Qoder 重置工具 GUI 启动脚本 - Windows 版本
chcp 65001 >nul

echo ==================================================
echo 启动 Qoder 重置工具 GUI (Windows)
echo ==================================================

REM 检查Python是否可用
python --version >nul 2>&1
if %errorlevel% neq 0 (
    echo ❌ 错误: 未找到 python
    echo 请安装 Python 后重试
    echo 可以从以下位置下载安装:
    echo 1. 从 https://www.python.org 下载安装
    echo 2. 从 Microsoft Store 安装 Python
    pause
    exit /b 1
)

REM 检查PyQt5是否可用
python -c "import PyQt5" 2>nul
if %errorlevel% neq 0 (
    echo ❌ 错误: PyQt5 不可用
    echo 正在尝试自动安装 PyQt5...
    python -m pip install PyQt5
    if %errorlevel% neq 0 (
        echo ❌ PyQt5 安装失败
        echo 请手动运行: python -m pip install PyQt5
        pause
        exit /b 1
    )
    echo ✅ PyQt5 安装成功
)

REM 检查GUI文件是否存在
if not exist "qoder_reset_gui.py" (
    echo ❌ 错误: 未找到 qoder_reset_gui.py
    echo 请确保在正确的目录中运行此脚本
    pause
    exit /b 1
)

echo ✅ 环境检查通过
echo 正在启动图形界面...
echo.

echo 正在启动GUI应用程序...

REM 启动GUI
if exist "qoder_reset_gui.py" (
    echo 启动 Qoder Reset GUI...
    python qoder_reset_gui.py
) else (
    echo ❌ 错误: 未找到GUI程序文件
    pause
    exit /b 1
)

echo.
echo GUI 已关闭
pause