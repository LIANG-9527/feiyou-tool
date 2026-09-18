"""存量官网诊断报告一次性回补企业绑定（SPEC: docs/specs/SPEC-audit-company-bind.md 修订②）。

对 var/output/audit/ 下未绑定的诊断报告执行与在线诊断相同的绑定流程：
站内全称提取（读快照正文）→ 工商精准核验 → 唯一才绑 + 工商字段入库；
英文名/多候选落 company_candidates 供人工选定。

用法：
  python3.12 scripts/backfill_audit_company.py            # 预览（不改任何文件）
  python3.12 scripts/backfill_audit_company.py --apply    # 实际写回报告 JSON 并入库
"""
import asyncio
import json
import re
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))


async def main(apply: bool):
    from audit_company_bind import bind_report, check_runtime_deps, extract_companies, snapshot_texts
    if apply:
        # 启动即自检：缺库时页脚渲染/首页重抓跑不成，历史上被静默误报成"站内没有"
        missing = check_runtime_deps()
        if missing:
            print(f"[环境缺库，拒绝回补] 缺：{'、'.join(missing)}")
            print("缺库会把'工具不在场'误报成'站内没有公司名'。请换用与服务器一致的解释器，")
            print("或装齐 playwright/requests/httpx 后重跑。")
            sys.exit(1)
    from business_api_client import get_business_api_client
    from company_cache import upsert_info

    audit_dir = BASE / "var" / "output" / "audit"
    api = get_business_api_client()
    files = [f for f in sorted(audit_dir.glob("*.json"))
             if re.search(r"_\d{8}_\d{6}\.json$", f.name)]
    for f in files:
        try:
            d = json.loads(f.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if "site_brand" not in d:
            continue  # 非诊断数据文件跳过
        if d.get("company_full_name"):
            print(f"[跳过] {f.name} 已绑定 → {d['company_full_name']}")
            continue
        if apply:
            r = await bind_report(f, api, upsert_info)
            tag = {"bound": "已绑定", "candidates": "落候选(待人工)",
                   "unchanged": "未提取到公司名(站内确实没有)",
                   "fallback_failed": "现场取页脚失败(环境/网络问题，并非站内没有)",
                   "skip": "跳过", "error": "出错"}[r.get("action", "error")]
            print(f"[{tag}] {f.name}"
                  + (f" → {r['company']}" if r.get("company") else "")
                  + (f" 候选: {r['candidates']}" if r.get("candidates") else "")
                  + (f" ({r.get('why')})" if r.get("why") else ""))
        else:
            cand = extract_companies(snapshot_texts(audit_dir, f.name))
            print(f"[预览] {f.name} 站内候选 cn={cand['cn']} en={cand['en']}")
    if not apply:
        print("\n（预览模式：未改任何文件。加 --apply 执行绑定写回）")


if __name__ == "__main__":
    asyncio.run(main("--apply" in sys.argv))
