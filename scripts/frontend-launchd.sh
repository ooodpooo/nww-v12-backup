#!/bin/bash
# =============================================================================
# 前端 Vite (5173) 的 launchd 托管启动器
#
# 背景：后端 8010 早由 com.nww.local-backend 托管（KeepAlive 自愈、开机自启），
#       但前端一直靠手工 `nohup npm run dev &` —— 重启电脑后不会回来，且 nohup
#       只挡 SIGHUP、挡不住 SIGTERM（关终端窗口即被杀）。本脚本 + plist 补上这一环。
#
# 由 com.nww.local-frontend 调用，不要手动执行（手动执行请用 ./nww start）。
# =============================================================================

set -u

FRONTEND_DIR="/Users/oodpoo/NWW-V12/web/frontend"

# 依次挑选可用的 node 22.22.2（与 nww 主脚本保持同一套优先级）
NODE_BIN=""
for cand in \
  "/Users/oodpoo/.workbuddy/binaries/node/versions/22.22.2-3/bin" \
  "/Users/oodpoo/.nvm/versions/node/v22.22.2/bin" \
  "/usr/local/bin" \
  "/opt/homebrew/bin"
do
  if [ -x "$cand/node" ]; then NODE_BIN="$cand"; break; fi
done

if [ -z "$NODE_BIN" ]; then
  echo "[frontend-launchd] 找不到可用的 node，退出" >&2
  exit 1
fi

export PATH="$NODE_BIN:$PATH"
# 收紧 vite 的目录扫描范围，降低 CPU/文件句柄占用
export WATCH_IGNORE_DEFAULT="node_modules,.git,dist"

cd "$FRONTEND_DIR" || exit 1

echo "[frontend-launchd] node=$("$NODE_BIN/node" -v) npm=$(command -v npm)"

# --strictPort：端口被占时直接失败退出（交给 launchd 的 KeepAlive 重试），
# 而不是像默认那样静默漂到 5174 —— 那会造出「5173 是僵尸旧代码、新实例在 5174」的陷阱。
exec npm run dev -- --strictPort
