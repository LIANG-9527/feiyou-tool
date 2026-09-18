#!/bin/bash
# 安全重启服务（P5 启动卫生，2026-09-17 定版）：
# 重启前检查运行中/排队中分析任务，有则拒绝（--force 越过——需人工确认放弃在跑任务）。
# 用法: ./scripts/restart.sh [--force]
cd "$(dirname "$0")/.." || exit 1
if [[ "$1" != "--force" ]]; then
    BUSY=$(grep -lE '"status": *"(running|pending)"' var/tasks/*.json 2>/dev/null)
    if [ -n "$BUSY" ]; then
        echo "拒绝重启：以下任务记录处于运行/排队中，杀掉服务会把它们打成中断（幽灵记录）："
        for f in $BUSY; do
            python3 -c "import json;d=json.load(open('$f'));print('  -',d.get('brand'),d.get('status'),d.get('method'))" 2>/dev/null || echo "  - $f"
        done
        echo "请等任务跑完再重启；确认放弃用: ./scripts/restart.sh --force"
        exit 1
    fi
fi
PID=$(lsof -nP -iTCP:8777 -sTCP:LISTEN -t | head -1)
[ -n "$PID" ] && { kill "$PID"; sleep 2; }
./run.sh --mode server &
sleep 3
curl -s -o /dev/null -w "重启后服务健康: %{http_code}\n" http://127.0.0.1:8777/health
