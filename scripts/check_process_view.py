# 生成过程悬浮视图自检：python3 scripts/check_process_view.py
import sys, json, logging
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
logging.disable(logging.CRITICAL)
import main

logs = main._task_log_index()
hit = 0
for jf in sorted((Path(__file__).resolve().parent.parent / "var" / "output").glob("市场调研_*.json")):
    if jf.stem.endswith(("_deck", "_deckv2", "_check", "_brief")) or ".bak" in jf.name:
        continue
    data = json.loads(jf.read_text(encoding="utf-8"))
    pv = main._build_process_view(data, logs.get(jf.stem + ".html"))
    if pv:
        hit += 1
        assert pv["nodes"] and pv["total"], pv
        for n in pv["nodes"]:
            assert n["rows"], n
            print(f"{jf.name[:28]}… {n['title']}: {len(n['rows'])}行")
assert main._build_process_view({"brand": "x"}) is None
print(f"OK: {hit} 份报告有生成过程数据，空数据回退 None 正确")
