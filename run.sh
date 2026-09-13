#!/bin/bash
# GEO Agent 启动脚本
# 用法:
#   ./run.sh --brand "好莱客"                 # CLI单次执行
#   ./run.sh --brand "好莱客" --product "全屋定制"
#   ./run.sh --mode server                     # 启动API服务
#   ./run.sh --mode server --port 9000         # 指定端口

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PYTHON="/Users/lmz/Library/Application Support/TRAE SOLO CN/ModularData/ai-agent/vm/tools/bin/python3"

if [ ! -f "$PYTHON" ]; then
    echo "错误: 找不到 Python 3.10"
    echo "请尝试: python3 main.py"
    exit 1
fi

cd "$SCRIPT_DIR"
# macOS 上防止空闲睡眠(caffeinate)；Linux 服务器没有该命令，直接运行
CAFF=""
if [[ "$(uname)" == "Darwin" ]]; then CAFF="caffeinate -is"; fi
exec $CAFF "$PYTHON" main.py "$@"
