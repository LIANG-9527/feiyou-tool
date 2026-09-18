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

# server 模式启动卫生（2026-09-17 提速修复）：
# 1) 端口已有监听 → 打印警告并退出，防止新旧实例切换混乱
# 2) 服务日志一律追加写 var/log/server.out，禁止截断丢历史日志
if [[ "$*" == *"--mode server"* ]]; then
    PORT=8777
    if [[ "$*" == *"--port "* ]]; then
        PORT="$(echo "$*" | sed -n 's/.*--port[ =]\([0-9][0-9]*\).*/\1/p')"
    fi
    if lsof -nP -iTCP:"$PORT" -sTCP:LISTEN >/dev/null 2>&1; then
        echo "警告: 端口 $PORT 已有进程在监听（旧服务实例仍在运行）。"
        echo "请先停掉旧实例再启动，避免新旧切换混乱。本次启动取消。"
        exit 1
    fi
    mkdir -p var/log
    exec $CAFF "$PYTHON" main.py "$@" >> var/log/server.out 2>&1
fi

exec $CAFF "$PYTHON" main.py "$@"
