"""问句挖掘报告 · 原型模板渲染（模板 skills/mining_report_template.html）。

适配器 build_data：报告 JSON → 模板 window.__MINING_DATA__ 同构对象（结构对齐产品原型 data.js）。
render：读模板、注入数据、返回离线自包含单文件 HTML。
接线在 main.save_output（mining 分支）；模板缺失或异常由调用方回退简版渲染，绝不丢报告。
数据契约来源：原型 data.js（window.SILKROAD_DATA）+ 模板 React 组件消费点，两侧交叉核对。
"""
import json
import re
from pathlib import Path

TEMPLATE = Path(__file__).resolve().parent / "skills" / "mining_report_template.html"
_COLORS = ["cyan", "blue", "violet", "amber", "orange", "emerald", "rose", "teal"]


def _plain(s):
    """去括号注解、取主名：'痛点触发与需求认知 (Awareness)' → '痛点触发与需求认知'。"""
    s = re.sub(r"[（(].*?[)）]", "", str(s or "")).strip()
    return re.split(r"\s{2,}", s)[0].strip() or str(s or "")[:12]


def _short_stage(name):
    return re.sub(r"^阶段\s*\d+\s*", "", _plain(name))


def _sig_parts(sig):
    """audience_signal「客群 | 心理:… | 策略:…」→ (客群, 策略)。"""
    parts = [p.strip() for p in str(sig or "").split("|")]
    seg = parts[0] if parts else ""
    strategy = ""
    for p in parts[1:]:
        if p.startswith("策略"):
            strategy = re.sub(r"^策略[:：]?\s*", "", p)
    return seg, strategy


def _priority(n):
    return "高" if n >= 8 else ("中" if n >= 4 else "低")


def build_data(result: dict) -> dict:
    brand = str(result.get("brand") or "")
    product = result.get("product") or ""
    jstages = [j for j in (result.get("step3_journey") or []) if isinstance(j, dict)]
    topics_by_stage = result.get("step4_topics") or {}
    clusters_by_stage = result.get("step5_clusters") or {}
    verif = [v for v in (result.get("step6_verification") or []) if isinstance(v, dict)]

    prods = []
    for i, p in enumerate(result.get("step1_products") or []):
        if not isinstance(p, dict):
            continue
        nm = _plain(p.get("product_name"))
        prods.append({"id": i + 1, "selected": i == 0, "name": nm, "subtitle": nm,
                      "positioning": p.get("positioning") or "",
                      "audience": p.get("target_audience") or "",
                      "evidence": p.get("promotion_basis") or "",
                      "color": _COLORS[i % len(_COLORS)]})
    reason = [prods[0]["evidence"]] if prods and prods[0]["evidence"] else ["默认选定第 1 产品为主推"]

    cats, seen = [], {}
    raw = result.get("step1_5_variants") or {}
    if isinstance(raw, dict):
        vals = list(raw.values())
        by_type = bool(vals) and isinstance(vals[0], dict) and vals[0].get("type")
        if by_type:
            for it in vals:
                t = it.get("type") or "其他"
                seen.setdefault(t, []).append(
                    {"name": it.get("variant") or it.get("name") or "",
                     "who": it.get("who_searches") or it.get("who") or ""})
            cats = [{"type": k, "color": _COLORS[i % len(_COLORS)], "variants": v}
                    for i, (k, v) in enumerate(seen.items())]
        else:
            # 真实形态：{产品名: [{type,variant,who_searches}]} → 跨产品按变体类型归组（对齐原型类别分组）
            for lst in raw.values():
                for it in (lst or []):
                    if isinstance(it, dict):
                        t = it.get("type") or "其他"
                        seen.setdefault(t, []).append(
                            {"name": it.get("variant") or it.get("name") or "",
                             "who": it.get("who_searches") or it.get("who") or ""})
            cats = [{"type": k, "color": _COLORS[i % len(_COLORS)], "variants": v}
                    for i, (k, v) in enumerate(seen.items())]
    if not cats:
        cats = [{"type": "官方全称", "color": "cyan", "variants": [{"name": brand, "who": ""}]}]

    segs = []
    for i, p in enumerate(result.get("step2_personas") or []):
        if not isinstance(p, dict):
            continue
        nm = str(p.get("name") or f"客群{i+1}")
        segs.append({"id": nm, "shortName": nm, "fullName": nm, "name": nm,
                     "color": _COLORS[i % len(_COLORS)],
                     "definition": p.get("definition") or "", "features": p.get("core_traits") or "",
                     "motivation": p.get("typical_motivation") or "",
                     "decisionHabit": p.get("decision_habit") or "", "strategy": p.get("sales_strategy") or ""})

    icbs, stage_list, journey_out, question_table = {}, [], [], []
    # step4 反查索引：问句文本 → 话题名（问句总表话题列用）
    topic_of = {}
    for bucket in (topics_by_stage or {}).values():
        if not isinstance(bucket, dict):
            continue
        for ln in (bucket.get("lines") or bucket.get("line_results") or []):
            if isinstance(ln, dict):
                for t in (ln.get("topics") or []):
                    if isinstance(t, dict):
                        for q in (t.get("queries") or []):
                            if isinstance(q, dict) and q.get("query"):
                                topic_of.setdefault(q["query"], t.get("topic") or "")
    for i, j in enumerate(jstages):
        sidx = j.get("stage_index", i + 1)
        skey = f"stage{sidx}"
        jn, short = _plain(j.get("stage_name")), _short_stage(j.get("stage_name"))
        journey_out.append({"stageKey": skey, "stageName": short, "alias": jn,
                            "fullName": f"阶段{sidx} {jn}", "color": _COLORS[i % 6],
                            "status": j.get("user_state") or "", "question": j.get("core_concern") or ""})
        bucket = topics_by_stage.get(str(sidx)) or {}
        tps = []
        for ln in (bucket.get("lines") or bucket.get("line_results") or []):
            if isinstance(ln, dict):
                for t in (ln.get("topics") or []):
                    if isinstance(t, dict):
                        tps.append({"name": t.get("topic") or "", "line": ln.get("line") or ""})
        cls = []
        for c in (clusters_by_stage.get(str(sidx)) or []):
            if not isinstance(c, dict) or not c.get("cluster_name"):
                continue
            cqs = (c.get("queries") or [])
            qs = []
            for q in cqs:
                if not isinstance(q, dict):
                    continue
                seg, strategy = _sig_parts(q.get("audience_signal"))
                qs.append({"text": q.get("query") or "", "topic": topic_of.get(q.get("query"), ""),
                           "segment": seg,
                           "role": "决策者" if "决策" in seg else "影响者",
                           "strategy": strategy, "need": (q.get("entry_type") or ""),
                           "index": len(qs) + 1})
            prio = _priority(len(cqs))
            cls.append({"name": _plain(c.get("cluster_name")), "priority": prio,
                        "basis": c.get("cluster_name") or "",
                        # 模板簇卡对 coversTopics 逐项 .map 渲染标签，必须是数组（字符串会让
                        # 整页 React 树崩掉白屏，2026-09-19 修复：39/39 簇曾为字符串）
                        "coversTopics": sorted({(t["line"] or "").strip() for t in tps if t["line"]}),
                        "questions": qs})
            for q in qs:
                question_table.append({"stage": f"阶段{sidx} {jn}", "stageKey": skey, "topic": q["topic"],
                                       "cluster": _plain(c.get("cluster_name")), "priority": prio,
                                       "question": q["text"], "segment": q["segment"], "role": q["role"],
                                       "strategy": q["strategy"], "need": q["need"]})
        vst = [v for v in verif if str(v.get("stage_index")) == str(sidx)]
        vd = sum(1 for v in vst if "一致" in str(v.get("conclusion") or "") and "调整" not in str(v.get("conclusion") or ""))
        va = sum(1 for v in vst if "调整" in str(v.get("conclusion") or ""))
        results = [{"cluster": _plain(v.get("cluster_name")),
                    "representative": (v.get("reverse_queries") or [""])[0],
                    "check": v.get("coverage_check") or "", "conclusion": v.get("conclusion") or "",
                    "adjustedOld": (v.get("adjustment") or "")} for v in vst]
        icbs[skey] = {"stageName": short, "alias": jn, "fullName": f"阶段{sidx} {jn}",
                      "color": _COLORS[i % 6], "total": len(cls), "clusterCount": len(cls),
                      "highCount": sum(1 for c in cls if c["priority"] == "高"), "topics": tps,
                      "clusters": cls,
                      "verification": {"clusterCount": len(vst), "direct": vd, "adjusted": va, "results": results}}
        stage_list.append({"key": skey, "name": f"阶段{sidx} {short}", "short": short, "alias": jn,
                           "color": _COLORS[i % 6], "total": len(cls), "clusters": len(cls),
                           "high": sum(1 for c in cls if c["priority"] == "高")})

    for n, row in enumerate(question_table, 1):
        row["index"] = n

    total_cl = sum(s["clusters"] for s in stage_list)
    high = sum(s["high"] for s in stage_list)
    vdir = sum(1 for v in verif if "一致" in str(v.get("conclusion") or "") and "调整" not in str(v.get("conclusion") or ""))
    vad = sum(1 for v in verif if "调整" in str(v.get("conclusion") or ""))
    return {
        "brand": {"name": brand, "code": "", "tagline": "GEO 搜索问句挖掘与意图分析",
                  "product": product or (prods[0]["name"] if prods else "")},
        "products": prods, "selectedReason": reason, "nameVariants": {"categories": cats},
        "segments": segs, "journey": journey_out, "stageList": stage_list,
        "intentClustersByStage": icbs, "questionTable": question_table,
        "stats": {"product": len(prods), "segments": len(segs), "clusters": total_cl,
                  "highPriority": high,
                  "midPriority": sum(1 for s in stage_list for c in icbs[s["key"]]["clusters"] if c["priority"] == "中"),
                  "lowPriority": sum(1 for s in stage_list for c in icbs[s["key"]]["clusters"] if c["priority"] == "低"),
                  "questions": len(question_table), "nicheClusters": 0, "pendingVerify": 0,
                  "directConsistent": vdir, "adjustedConsistent": vad,
                  "consistencyRate": f"{round(100*vdir/total_cl)}%" if total_cl else "-"},
        "steps": [{"id": 1, "label": "Step 1", "title": "核心产品选定", "desc": "核心产品清单与选定"},
                  {"id": 1.5, "label": "Step 1.5", "title": "名称变体体系", "desc": "别名体系采集"},
                  {"id": 2, "label": "Step 2", "title": "目标客群画像", "desc": f"{len(segs)} 类客群"},
                  {"id": 3, "label": "Step 3", "title": "决策旅程", "desc": "固定6阶段统一框架"},
                  {"id": 4.5, "label": "Step 4-5", "title": "意图簇与问句", "desc": "话题拆解+意图簇归组"},
                  {"id": 5.5, "label": "Step 5.5", "title": "问句清单总表", "desc": f"{len(question_table)} 句全量总表"}],
    }


def render(result: dict) -> str:
    """注入数据→离线自包含 HTML。模板缺失抛错，由调用方回退简版。"""
    if not TEMPLATE.exists():
        raise FileNotFoundError("挖掘报告模板缺失: " + str(TEMPLATE))
    html = TEMPLATE.read_text(encoding="utf-8")
    if "__MINING_DATA_JSON__" not in html:
        raise ValueError("模板无数据注入占位")
    data = json.dumps(build_data(result), ensure_ascii=False).replace("</", "<\\/")
    out = html.replace("__MINING_DATA_JSON__", data)
    return out


if __name__ == "__main__":
    import glob
    f = sorted(glob.glob("var/output/问句挖掘_*.json"))[0]
    res = json.load(open(f, encoding="utf-8"))
    d = build_data(res)
    assert d["stageList"], "阶段为空"
    assert len(d["questionTable"]) > 100, "问句总表过少"
    assert all(s["key"] in d["intentClustersByStage"] for s in d["stageList"])
    assert all(isinstance(c.get("coversTopics"), list)
               for s in d["intentClustersByStage"].values() for c in s["clusters"]), \
        "coversTopics 必须是数组（模板逐项map渲染，字符串会整页白屏）"
    h = render(res)
    assert "__MINING_DATA_JSON__" not in h and "window.__MINING_DATA__" in h
    # 自包含：不得有跨网络加载的 script/link/img（xmlns 命名空间声明不算）
    ext = re.findall(r'<(?:script|link|img)[^>]*(?:src|href)="https?://[^"]+"', h)
    assert not ext, f"存在外链资源未内联: {ext[:3]}"
    print(f"render 自检 OK: {f.split('/')[-1][:40]} 阶段{len(d['stageList'])} 簇{d['stats']['clusters']} 问句{len(d['questionTable'])} → {len(h)}字节")
