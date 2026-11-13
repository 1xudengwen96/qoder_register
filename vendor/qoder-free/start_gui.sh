#!/bin/bash

# Qoder 重置工具 GUI 启动脚本

echo "=================================================="
echo "启动 Qoder 重置工具 GUI"
echo "=================================================="

# 检查Python3是否可用
if ! command -v python3 &> /dev/null; then
    echo "❌ 错误: 未找到 python3"
    echo "请安装 Python 3 后重试"
    echo "可以通过以下方式安装:"
    echo "1. 从 https://www.python.org 下载安装"
    echo "2. 使用 Homebrew: brew install python3"
    exit 1
fi

# 检查tkinter是否可用
python3 -c "import tkinter" 2>/dev/null
if [ $? -ne 0 ]; then
    echo "❌ 错误: tkinter 不可用"
    echo "请确保安装了完整的 Python 3，包括 tkinter 模块"
    echo "在 macOS 上，可能需要安装 python-tk:"
    echo "brew install python-tk"
    exit 1
fi

# 检查是否有 pythonw（推荐用于GUI应用）
PYTHON_CMD="python3"
if command -v pythonw &> /dev/null; then
    PYTHON_CMD="pythonw"
    echo "✅ 使用 pythonw 启动GUI应用"
else
    echo "ℹ️  使用 python3 启动GUI应用"
fi

# 检查GUI文件是否存在
if [ ! -f "qoder_reset_gui.py" ]; then
    echo "❌ 错误: 未找到 qoder_reset_gui.py"
    echo "请确保在正确的目录中运行此脚本"
    exit 1
fi

echo "✅ 环境检查通过"
echo "正在启动图形界面..."
echo

# 设置环境变量以抑制 Tk 弃用警告
export TK_SILENCE_DEPRECATION=1

echo "正在启动GUI应用程序..."

# 启动GUI
if [ -f "qoder_reset_gui.py" ]; then
    echo "启动 Qoder Reset GUI..."
    $PYTHON_CMD qoder_reset_gui.py
else
    echo "❌ 错误: 未找到GUI程序文件"
    exit 1
fi

echo
echo "GUI 已关闭"
