"""一次性回灌：把挖掘/检测两板块的扩字段补进企业数据库（口径与在线写入完全同源）。

- 问句挖掘：读 var/output/问句挖掘_*.json，按 _save_mining_facts 同规则重灌
  （含 话题库 topics、客群 audience_type、旅程 journey 全量）。
- 官网检测：读 var/output/audit/*.json，从报告数据算出 维度分/指纹/后台/
  必检非必检计数/核心问题，合并写回对应 audit_snapshot 行（不清旧字段）。

用法: python3 scripts/backfill_company_data.py            # 预览
      python3 scripts/backfill_company_data.py --apply    # 写库
"""
import json
import re
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))
APPLY = "--apply" in sys.argv


def mine_rows():
    import main  # 复用在线同一实现
    out = []
    for f in sorted((BASE / "var" / "output").glob("问句挖掘_*.json")):
        if not re.search(r"_\d{8}_\d{6}\.json$", f.name):
            continue
        result = json.loads(f.read_text(encoding="utf-8"))
        result.setdefault("method_id", "geo_query_mining")
        out.append((f.name, result, main))
    return out


def audit_extras(d: dict) -> dict:
    """与 site_audit.runner._audit_conclusions 同口径，但吃报告 JSON dict。"""
    cats = [{"name": c.get("name") or c.get("category"), "score": round(c.get("score") or 0, 1),
             "fail": c.get("failed", c.get("fail")), "warn": c.get("warned", c.get("warn")),
             "pass": c.get("passed", c.get("pass"))} for c in d.get("categories") or []]
    scored = [r for r in d.get("results") or [] if r.get("id") not in ("tech-14", "tech-15")]
    core = [r for r in scored if r.get("tier", "core") != "bonus"]
    bonus = [r for r in scored if r.get("tier") == "bonus"]

    def cnt(rs, st):
        return sum(1 for r in rs if r.get("status") == st)
    idx = {r.get("id"): r for r in d.get("results") or []}  # 指纹/后台是诊断项，不在 scored 里

    def concl(iid):
        r = idx.get(iid) or {}
        return {"status": r.get("status", "skip"), "message": str(r.get("message") or "")[:120]}
    issues = [{"name": r.get("name", ""), "message": str(r.get("message") or "")[:120],
               "suggestion": str(r.get("suggestion") or "")[:160]}
              for r in scored if r.get("status") == "fail"]
    return {"categories": cats,
            "core_counts": {"total": len(core), "pass": cnt(core, "pass"),
                            "warn": cnt(core, "warn"), "fail": cnt(core, "fail")},
            "bonus_counts": {"total": len(bonus), "pass": cnt(bonus, "pass"),
                             "warn": cnt(bonus, "warn"), "fail": cnt(bonus, "fail")},
            "fingerprint": concl("tech-14"), "backend": concl("tech-15"), "issues": issues[:20]}


def main_run():
    from company_cache import upsert_mining, _conn
    import main  # _save_mining_facts 同源

    n = 0
    for name, result, _ in mine_rows():
        if APPLY:
            main._save_mining_facts(result)
        print(f"[挖掘] {name[:44]} → topics={result.get('step4_topics') and sum(1 for _ in result['step4_topics'].values())}")
        n += 1

    c = _conn()
    m = 0
    for f in sorted((BASE / "var" / "output" / "audit").glob("*.json")):
        if not re.search(r"_\d{8}_\d{6}\.json$", f.name):
            continue
        d = json.loads(f.read_text(encoding="utf-8"))
        sid = f.name[:-5]  # domain_ts
        row = c.execute("SELECT data FROM audit_snapshot WHERE id=?", (sid,)).fetchone()
        if not row:
            print(f"[检测] {sid[:40]} 快照行不存在，跳过")
            continue
        old = json.loads(row[0])
        old.update(audit_extras(d))
        if APPLY:
            c.execute("UPDATE audit_snapshot SET data=? WHERE id=?",
                      (json.dumps(old, ensure_ascii=False), sid))
        m += 1
    if APPLY:
        c.commit()
    print(f"完成：挖掘 {n} 份、检测 {m} 份（{'已写库' if APPLY else '预览模式，未写库'}）")


if __name__ == "__main__":
    main_run()
