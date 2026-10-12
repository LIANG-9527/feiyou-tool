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
# 冒烟检查（2026-09-19）：重启后核心接口必须返回合法形状，异常大声报错不静默
SMOKE_FAIL=0
# 登录门禁（2026-09-24）：.env 配了账号则先登录拿会话，冒烟请求带 Cookie 走；未配置=门禁关闭，行为同旧
AUTH_COOKIE=$(python3 -c "import sys; sys.path.insert(0,'scripts'); import _auth; v=_auth.login('http://127.0.0.1:8777'); print(v or '')" 2>/dev/null)
CK=(-sf); [ -n "$AUTH_COOKIE" ] && CK=(-sf -H "Cookie: geo_session=$AUTH_COOKIE")
curl -sf http://127.0.0.1:8777/health >/dev/null || { echo "⚠️ 冒烟失败: /health 无响应"; SMOKE_FAIL=1; }
BC=$(curl "${CK[@]}" "http://127.0.0.1:8777/api/brand/check?name=%E5%A5%BD%E8%8E%B1%E5%AE%A2" 2>/dev/null)
echo "$BC" | grep -q '"exists"' || { echo "⚠️ 冒烟失败: 名称核查返回形状异常 → $BC"; SMOKE_FAIL=1; }
curl "${CK[@]}" -o /dev/null http://127.0.0.1:8777/mining || { echo "⚠️ 冒烟失败: /mining 页面异常"; SMOKE_FAIL=1; }
[ "$SMOKE_FAIL" = "1" ] && echo "═══ 冒烟检查未通过，服务带病运行，请排查 ═══" || echo "冒烟检查通过（/health、名称核查形状、/mining 页面）"
curl -s -o /dev/null -w "重启后服务健康: %{http_code}\n" http://127.0.0.1:8777/health
