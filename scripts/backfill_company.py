"""存量报告回灌企业资料库：读 var/output/ 已生成报告 JSON，按新版规则写入公司档案库并出现在企业资料列表。

规则与在线生成路径完全同口径：字段级合并（非空覆盖）、触点记录（客户市场调研）、
首次信息来源只写一次；新出现的客户，首次创建时间按其最早报告的实际时间回填（非编造，
即该数据首次进入系统的时间点）。

用法:
  python3 scripts/backfill_company.py                 # 演练：只打印将写入什么，不动库
  python3 scripts/backfill_company.py --apply         # 真正写库（可重复执行，幂等合并）
  python3 scripts/backfill_company.py 好莱客 --apply  # 按名称包含过滤
  python3 scripts/backfill_company.py --db 路径 ...    # 指定库文件（默认 var/data/company_info.db）
"""
import glob
import json
import re
import sys
from pathlib import Path
from collections import defaultdict

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import company_cache as cc

TS_RE = re.compile(r"_(\d{8})_(\d{6})\.json$")


def reports_by_brand(only=None):
    """每个 brand 的报告 JSON，按时间从旧到新；只认标准时间戳结尾的正式报告。"""
    groups = defaultdict(list)
    for f in sorted(glob.glob(str(ROOT / "var" / "output" / "市场调研_*.json"))):
        m = TS_RE.search(Path(f).name)
        if not m:
            continue
        d = json.loads(Path(f).read_text(encoding="utf-8"))
        brand = d.get("brand") or Path(f).name.split("_", 1)[1].rsplit("_", 1)[0]
        if only and not any(o in brand for o in only):
            continue
        d8, t6 = m.group(1), m.group(2)
        ts = f"{d8[:4]}-{d8[4:6]}-{d8[6:]} {t6[:2]}:{t6[2:4]}"
        groups[brand].append((ts, f))
    return groups


def payload_of(d: dict) -> dict:
    """画像信息字段 + 组织图种子节点补法人/创始人/信用代码（与生成路径同款合并）。"""
    info = dict((d.get("mr_brief") or {}).get("info") or {})
    org = d.get("mr_org") if isinstance(d.get("mr_org"), dict) else {}
    seed = next((n for n in (org.get("nodes") or []) if n.get("id") == org.get("seed_key")), {})
    for f in ("legal_person", "founder", "credit_code"):
        if seed.get(f) and not info.get(f):
            info[f] = seed[f]
    return info


def run_backfill(apply=False, only=None):
    """核心逻辑，供 CLI 与网页「JSON数据补录入库」共用。
    apply=False 演练不动库；True 幂等合并写入。返回结构化明细。"""
    rows = []
    for brand, files in reports_by_brand(only).items():
        for i, (ts, f) in enumerate(files):  # 旧→新重放，新报告字段覆盖旧值
            d = json.loads(Path(f).read_text(encoding="utf-8"))
            full = ((d.get("mr_brief") or {}).get("info") or {}).get("full_name") or brand
            payload = payload_of(d)
            exists = cc.get_info(full)[0] is not None
            first_of_brand = i == 0
            if apply:
                cc.upsert_info(full, payload, credit_code=payload.get("credit_code"),
                               origin="客户市场调研", created=ts if first_of_brand else None)
            rows.append({"name": full, "fields": len(payload), "report": Path(f).name,
                         "ts": ts, "new": (not exists) and first_of_brand})
    return rows


def main():
    args = [a for a in sys.argv[1:]]
    apply = "--apply" in args
    db_arg = args[args.index("--db") + 1] if "--db" in args else None
    if db_arg:
        cc.DB_PATH = db_arg
    only = [a for a in args if not a.startswith("--") and a != (db_arg or "\0")]
    rows = run_backfill(apply, only)
    for r in rows:
        tag = f"新建（创建时间={r['ts']}）" if r["new"] else "已有行合并"
        print(f"{'[写入]' if apply else '[演练]'} {r['name']}: {r['fields']}字段 · {r['report'][-15:-5]} · {tag}")
    new = sum(1 for r in rows if r["new"])
    print(f"\n共 {len(rows)} 次写入，其中新建客户 {new} 家。" + ("" if apply else "（演练模式未动库，加 --apply 生效）"))


if __name__ == "__main__":
    main()
