"""对 output 里每家公司的最新报告，重跑画像数据（种子标准化+基本信息，百度源），
合并进缓存（非空覆盖/空值保底），回填 JSON 并重渲染同名报告 HTML。

用法: python3 scripts/rerun_profiles.py            # 全部公司
      python3 scripts/rerun_profiles.py 名称1 名称2  # 指定公司（名称包含匹配）
"""
import asyncio
import glob
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))
from main import load_config, LLMClient, Agent, generate_markdown_report
from org_graph import _chat_json, SEED_PROMPT, CREDIT_RE
from company_cache import upsert_info, get_info

ONLY = sys.argv[1:]


def latest_reports():
    """每个 brand 取最新一份 JSON。"""
    best = {}
    for f in glob.glob(str(ROOT / "var" / "output" / "市场调研_*.json")):
        brand = Path(f).name.split("_", 1)[1].rsplit("_", 1)[0]
        if ONLY and not any(o in brand for o in ONLY):
            continue
        if brand not in best or os.path.getmtime(f) > os.path.getmtime(best[brand]):
            best[brand] = f
    return best


async def run_one(agent, llm, jf):
    d = json.loads(Path(jf).read_text(encoding="utf-8"))
    brand = d.get("brand", "")
    full = (d.get("mr_brief") or {}).get("info", {}).get("full_name") or brand
    print(f"—— {brand}")

    # 1) 种子标准化：法人/创始人/信用代码（百度源）
    try:
        seed = await _chat_json(llm, SEED_PROMPT.format(company=full)) or {}
    except Exception as e:
        seed = {}
        print("  种子失败:", e)
    seed = {k: seed.get(k) for k in ("name", "credit_code", "legal_person", "founder", "status")}
    print("  种子:", {k: str(v)[:24] for k, v in seed.items() if v})

    # 2) 画像字段重跑（百度源）
    skill = next(s for s in agent.skills if s.get("id") == "mr_brief")
    ctx = {k: d.get(k) for k in ("brand", "mr_profile") if d.get(k) is not None}
    fixed = await agent._exec_single(skill, ctx)
    info = fixed.get("info", {}) if isinstance(fixed, dict) else {}
    print("  画像字段:", [k for k, v in info.items() if v])

    # 3) 合并：新值优先，空值由缓存保底；写缓存
    old, ts = get_info(full)
    for k, v in (old or {}).items():
        if not info.get(k):
            info[k] = v
    upsert_info(full, info, credit_code=seed.get("credit_code"))
    upsert_info(full, {k: v for k, v in seed.items() if k != "name"},
                credit_code=seed.get("credit_code"))
    if not info.get("full_name"):
        info["full_name"] = seed.get("name") or full
    cached, ts = get_info(full)
    for k, v in (cached or {}).items():
        if not info.get(k):
            info[k] = f"{v}（历史缓存 {ts[:10]}）" if isinstance(v, str) else v
    if isinstance(d.get("mr_brief"), dict):
        d["mr_brief"]["info"] = info

    # 4) 组织图种子节点同步法人/创始人
    if isinstance(d.get("mr_org"), dict):
        for n in d["mr_org"].get("nodes", []):
            if n.get("id") == d["mr_org"].get("seed_key"):
                n["legal_person"] = n.get("legal_person") or seed.get("legal_person")
                n["founder"] = n.get("founder") or seed.get("founder")
                n["credit_code"] = n.get("credit_code") or seed.get("credit_code")

    Path(jf).write_text(json.dumps(d, ensure_ascii=False, indent=2), encoding="utf-8")
    html = Path(jf).with_suffix(".html")
    html.write_text(generate_markdown_report(d), encoding="utf-8")
    print("  已回填并重渲染:", html.name)


async def main():
    cfg = load_config()["llm"]
    llm = LLMClient(base_url=cfg["base_url"], api_key=cfg["api_key"], model=cfg["model"],
                    temperature=cfg.get("temperature", 0.7), max_tokens=cfg.get("max_tokens", 16384),
                    timeout=cfg.get("timeout", 120), max_retries=2,
                    enable_search=True, enable_thinking=False, search_engine=["baidu"])
    agent = Agent(llm, max_concurrency=2)
    reports = latest_reports()
    print("公司数:", len(reports))
    for brand, jf in sorted(reports.items()):
        try:
            await run_one(agent, llm, jf)
        except Exception as e:
            print(f"—— {brand} 失败: {e}")

asyncio.run(main())
