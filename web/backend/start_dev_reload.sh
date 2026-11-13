#!/bin/bash
# 仅开发调试用，勿在写作时使用。
#
# 该脚本以 NWW_RELOAD=1 启动后端：uvicorn 会监视 web/backend/app/ 下的 *.py，
# 任一文件保存都会重启后端进程，并中断正在运行的写作/重写任务。
# 日常运行与写作场景请直接使用 ./start.sh（默认关闭热载）。
#
# 用法：./start_dev_reload.sh            # 默认 127.0.0.1:8000
#       PORT=8010 ./start_dev_reload.sh  # 其它参数/环境变量与 start.sh 一致

set -e

cd "$(dirname "$0")"

NWW_RELOAD=1 exec ./start.sh "$@"
