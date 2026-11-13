#!/bin/bash
# 启动 Gemini 网页版专线 OpenAI 兼容代理（独立旁路进程，不影响 NWW 后端 8000）。
# 用法：./start-gemini-proxy.sh
# 前置：./launch_chrome_debug.sh 已运行且浏览器已登录 aistudio.google.com
# CLI 工具侧配置 base_url=http://127.0.0.1:8787/v1（api_key 填任意非空串）。

set -e
cd "$(dirname "$0")"

# 绕过系统代理拦截 localhost（与 web/backend/start.sh 同策略）
export NO_PROXY="${NO_PROXY:-*}"
export no_proxy="${no_proxy:-*}"

HOST="${GEMINI_PROXY_HOST:-127.0.0.1}"
PORT="${GEMINI_PROXY_PORT:-8787}"

echo "启动 Gemini Chrome Bridge 代理..."
echo "  地址: http://${HOST}:${PORT}/v1/chat/completions"
echo "  前置: Chrome CDP 9222 已监听（./launch_chrome_debug.sh）"

exec python3 gemini_chrome_proxy.py
