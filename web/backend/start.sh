#!/bin/bash
# NWW-V12 Web Backend 启动脚本
# 解决本地代理(http_proxy)拦截 localhost 请求导致 404 的问题
#
# 代码热载策略（2026-11-06，见 plan.md 的 C-RUNNER@1）：
#   默认关闭 --reload。uvicorn 的 reload 会监视 app/ 下的 *.py，任何保存都会
#   terminate 后端子进程，从而中断正在运行的写作/重写任务（daemon 线程随进程死）。
#   需要开发调试时显式设置 NWW_RELOAD=1，或使用 ./start_dev_reload.sh。

set -e

# 绕过本地代理，确保 localhost 请求直达后端
# 外部已配置 NO_PROXY/no_proxy 时沿用外部值（launchd 部署只豁免回环地址，
# 保留远程 LLM 端点走环境代理的行为）；未配置时退化为全直连。
export NO_PROXY="${NO_PROXY:-*}"
export no_proxy="${no_proxy:-*}"

# 必须设置的环境变量
export NWW_DEV_WEAK_SECRET="${NWW_DEV_WEAK_SECRET:-1}"
export NWW_DATA_PLANE="${NWW_DATA_PLANE:-1}"
export NWW_MCP_ENABLED="${NWW_MCP_ENABLED:-1}"
# 功能开关（默认开启，按需置 0 关闭）
export NWW_MODEL_CONFIG_ENABLED="${NWW_MODEL_CONFIG_ENABLED:-1}"
export NWW_MVP_ENABLED="${NWW_MVP_ENABLED:-1}"
export NWW_AGENT_ENABLED="${NWW_AGENT_ENABLED:-1}"
export NWW_AGENTS_ENABLED="${NWW_AGENTS_ENABLED:-1}"
# 流水线监督器（2026-10-25）：目标驱动自动派单 + 收件箱督促（NWW_SUPERVISOR_INTERVAL 可调 tick 秒数）
export NWW_SUPERVISOR_ENABLED="${NWW_SUPERVISOR_ENABLED:-1}"
# 整体规划硬约束（NRL）：shadow=注入节奏规划但不阻断；enforce=阻断
export NWW_NRL_MODE="${NWW_NRL_MODE:-shadow}"
# 代码热载开关：默认 0（关闭），置 1 恢复旧的热载调试行为
export NWW_RELOAD="${NWW_RELOAD:-0}"

# 质量审计器（2026-11-06 核查后拍板）：均为 advisory —— 只写审计记录，不改正文、
# 不改 stages 的 ok、不阻断提交；需要回滚时置 0。
#   NWW_TAIL_GATE=1（默认开）章尾动力类型轮换审计：最近 3 章章尾是否连续同类型、
#     类型分布、未分类率（防套路化/追读疲劳）。脚本真实可执行，零 LLM、秒级。
#   NWW_CAUSAL_AUDIT=0（默认关，勿开）跨章因果审计目前是半成品：
#     scripts/nww_causal_audit.py 只有 audit_chapter() 函数，无 __main__/CLI，
#     pipeline 以子进程方式调用时输出为空；且内部 commitments/state_changes 是
#     硬编码示例数据（"沈砚在值房抄录灾异档"）。开启只会得到「因果审计失败」
#     或假绿灯。需先补齐 CLI + 真实 ledger 读取后再默认开。
export NWW_TAIL_GATE="${NWW_TAIL_GATE:-1}"
export NWW_CAUSAL_AUDIT="${NWW_CAUSAL_AUDIT:-0}"

HOST="${HOST:-127.0.0.1}"
PORT="${PORT:-8000}"

cd "$(dirname "$0")"

echo "启动 NWW-V12 后端..."
echo "  地址: http://${HOST}:${PORT}"
echo "  NWW_DATA_PLANE=${NWW_DATA_PLANE}"
echo "  NWW_MCP_ENABLED=${NWW_MCP_ENABLED}"
echo "  NO_PROXY=${NO_PROXY}"
echo "  NWW_RELOAD=${NWW_RELOAD}"

# 开发模式：显式开启热载，且只监视 app/ 单一目录
if [ "$NWW_RELOAD" = "1" ]; then
  echo "⚠️ 开发模式：热载已开启（监视 app/）。写作任务运行期间保存 .py 会重启后端并中断任务。"
  exec uvicorn app.main:app --host "$HOST" --port "$PORT" --reload --reload-dir app
fi

# 生产/日常模式：不启用热载
exec uvicorn app.main:app --host "$HOST" --port "$PORT"
