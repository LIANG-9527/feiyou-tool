#!/bin/bash
# 改动合入前的机器闸（2026-09-19）：语法编译 + pyright 静态检查（基线对比，零新增错误才放行）
# 用法: bash scripts/check.sh   （退出码非0 = 有新增错误，禁止合入/重启）
cd "$(dirname "$0")/.." || exit 1
PY="/Users/lmz/Library/Application Support/TRAE SOLO CN/ModularData/ai-agent/vm/tools/bin/python3"

echo "── ① 语法编译检查（全部 .py）"
FAIL=0
while IFS= read -r f; do
  "$PY" -m py_compile "$f" 2>/dev/null || { echo "  ✗ 语法错误: $f"; FAIL=1; }
done < <(find . -name "*.py" -not -path "./var/*" -not -path "./dist/*" -not -path "./.zcode/*" -not -path "*/__pycache__/*")
[ "$FAIL" = "0" ] && echo "  ✓ 全部通过"

echo "── ② pyright 静态检查（核心7文件，基线对比）"
CORE="main.py company_cache.py agent.py task_manager.py mining_report_render.py profile_pipeline.py business_api_client.py"
pyright $CORE --outputjson > /tmp/pyright_now.json 2>/dev/null
"$PY" - <<'PYEOF'
import json, sys
base = json.load(open('scripts/pyright-baseline.json'))
d = json.load(open('/tmp/pyright_now.json'))
now = {}
for x in d['generalDiagnostics']:
    if x['severity'] == 'error':
        now[x['file'].split('/')[-1]] = now.get(x['file'].split('/')[-1], 0) + 1
fail = False
for f in sorted(set(base) | set(now)):
    b, n = base.get(f, 0), now.get(f, 0)
    mark = '✓' if n <= b else '✗'
    if n > b:
        fail = True
        print(f'  {mark} {f}: {b} → {n} （新增 {n-b} 个错误，禁止合入）')
    else:
        print(f'  {mark} {f}: {n}/{b}')
sys.exit(1 if fail else 0)
PYEOF
[ $? -ne 0 ] && FAIL=1

if [ "$FAIL" = "1" ]; then
  echo "═══ 检查未通过：先修复新增错误再合入 ═══"
  exit 1
fi
echo "═══ 检查通过 ═══"
