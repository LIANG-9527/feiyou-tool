#!/bin/bash
# UI 冒烟（浏览器交互层，2026-09-19）：真实浏览器走名称核查三弹窗流程，
# 专门钉死"识别中抢点"时序用例。改 main.py 内嵌弹窗/前端 JS 后必跑。
# 用法: bash scripts/ui-smoke.sh        （需服务已在 8777 运行；退出码非0=有流程没过）
cd "$(dirname "$0")/.." || exit 1
PY="/Users/lmz/Library/Application Support/TRAE SOLO CN/ModularData/ai-agent/vm/tools/bin/python3"
exec "$PY" scripts/ui_smoke.py "$@"
