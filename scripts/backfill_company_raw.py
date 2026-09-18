"""存量企业档案一次性补齐工商全量字段：逐家按信用代码/全称查工商详情接口，
全量原始字段字段级合并入库（API 给什么存什么，空值不覆盖）。

用法：
  python3.12 scripts/backfill_company_raw.py            # 预览
  python3.12 scripts/backfill_company_raw.py --apply    # 实际入库
"""
import asyncio
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))


async def main(apply: bool):
    from business_api_client import get_business_api_client, BusinessAPIClient
    from company_cache import list_companies, get_company_by_id, upsert_info

    api = get_business_api_client()
    for row in list_companies():
        rid, name = row["id"], row["name"]
        info, _, _ = get_company_by_id(rid)
        info = info or {}
        code = info.get("credit_code")
        try:
            detail = (await api.get_company_detail_by_credit_code(code, use_cache=False) if code
                      else await api.get_company_detail_by_name(name, use_cache=False))
        except Exception as e:
            print(f"[失败] {name}: {type(e).__name__}")
            continue
        raw = BusinessAPIClient.extract_raw_fields(detail or {})
        raw.update(BusinessAPIClient.extract_layer1_fields(detail or {}))
        missing = [k for k in raw if k not in info]
        print(f"[{'补' if apply else '预览'}] {name}: 接口字段 {len(raw)} 个，库中缺失 {len(missing)} 个"
              + (f"：{missing}" if missing else ""))
        if apply and raw:
            upsert_info(name, raw, credit_code=raw.get("credit_code"), origin="工商全量回灌")


if __name__ == "__main__":
    asyncio.run(main("--apply" in sys.argv))
