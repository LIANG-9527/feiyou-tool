"""补历史任务的方法归属：tasks/*.json 里 method 缺失/为 null 的，按内容推断后回写。
用法：在项目根目录执行  python3 scripts/patch_task_method.py
幂等：只动缺 method 的文件；已有值的跳过。运行前先停服务或运行后重启服务。
"""
import json
import glob
import sys

MR = "customer_market_research"   # 客户市场调研
MINE = "geo_query_mining"         # GEO搜索问句挖掘

def infer(d: dict) -> tuple[str, str]:
    """返回 (方法, 依据)。"""
    text = json.dumps(d, ensure_ascii=False)
    if '"step": "mr_' in text or "市场调研_" in text or "情报简报" in text:
        return MR, "进度含调研步骤/报告文件名前缀"
    if "问句挖掘_" in text:
        return MINE, "报告文件名前缀"
    steps = {p.get("step") for p in d.get("progress_log") or []}
    if steps & {"企业档案", "组织图", "简报机检", "画像合并"}:
        return MR, "画像管线步骤"
    return MINE, "无调研痕迹，按早期单方法默认"  # 调研方法上线前的任务只有问句挖掘

def main():
    fixed = skipped = 0
    for p in sorted(glob.glob(str(Path(__file__).resolve().parent.parent / "var" / "tasks" / "*.json"))):
        try:
            d = json.loads(open(p, encoding="utf-8").read())
        except (OSError, ValueError) as e:
            print(f"!! 读取失败跳过 {p}: {e}"); continue
        if d.get("method"):
            skipped += 1; continue
        m, why = infer(d)
        d["method"] = m
        open(p, "w", encoding="utf-8").write(json.dumps(d, ensure_ascii=False, indent=2))
        fixed += 1
        print(f"{p} -> {m} ({why}) | {d.get('brand','')[:20]} | {d.get('status')}")
    print(f"\n完成：补写 {fixed} 条，已有值跳过 {skipped} 条。重启服务生效。")
    return 0

if __name__ == "__main__":
    sys.exit(main())
