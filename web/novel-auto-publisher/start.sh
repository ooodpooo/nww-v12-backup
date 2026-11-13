#!/bin/bash
# =============================================================================
# 自动发布服务 · 一键依赖安装与启动脚本
# =============================================================================

set -e
DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$DIR"

# 自动定位 Python 环境（优先使用 miniconda，其次系统 python3）
if [ -f "/opt/miniconda3/bin/python" ]; then
    PY="/opt/miniconda3/bin/python"
    PIP="/opt/miniconda3/bin/pip"
elif [ -f "$HOME/miniconda3/bin/python" ]; then
    PY="$HOME/miniconda3/bin/python"
    PIP="$HOME/miniconda3/bin/pip"
else
    PY="python3"
    PIP="pip3"
fi

echo "[1/3] 检测 Python 环境: $PY"

# 检查 aiosqlite 是否已安装，若缺失则自动补全安装
if ! "$PY" -c "import aiosqlite, playwright" 2>/dev/null; then
    echo "[2/3] 检测到必要依赖缺失，正在自动安装依赖包..."
    "$PIP" install -r requirements.txt
    "$PY" -m playwright install chromium || true
else
    echo "[2/3] 核心依赖已满足，无需重复安装"
fi

echo "[3/3] 启动自动发布服务 (8012 端口)..."
"$PY" run.py
