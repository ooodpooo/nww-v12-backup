#!/bin/bash

# MCP SuperAssistant 本地代理服务器启动脚本
# 连接本地 NWW MCP 服务器

set -e

echo "🚀 启动 MCP SuperAssistant 本地代理服务器..."
echo ""

# 检查配置文件
CONFIG_FILE="$(dirname "$0")/mcpconfig.json"
if [ ! -f "$CONFIG_FILE" ]; then
    echo "❌ 找不到配置文件: $CONFIG_FILE"
    exit 1
fi

echo "📋 配置文件: $CONFIG_FILE"
echo "🔗 连接本地 MCP: python3 local_mcp_server.py"
echo ""
echo "✅ 启动后，请在 Chrome 扩展中配置："
echo "   Server URL: http://localhost:3006"
echo ""
echo "💡 然后打开 Google AI Studio 开始使用"
echo "   https://aistudio.google.com/app/prompts/new_chat"
echo ""

# 启动代理服务器
npx @srbhptl39/mcp-superassistant-proxy@latest --config "$CONFIG_FILE"
