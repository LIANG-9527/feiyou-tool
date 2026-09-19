"""GEO Query Mining Agent — FastAPI server + CLI entry + HTML report."""
from __future__ import annotations
from typing import Optional
import asyncio
import json
import os
import re
import sys
import time
import threading
import uuid
import argparse
import logging
from pathlib import Path

import yaml
import httpx
from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse, HTMLResponse, StreamingResponse, FileResponse
from pydantic import BaseModel as PydanticModel, Field

from llm_client import LLMClient
from agent import Agent, ChatAgent
from site_audit.runner import run_site_audit
from site_audit.core.ai_engine import estimate_cost_cny

# 平台全局版本号（唯一来源），格式 Version：YY.MDDDi.N——年后两位、月日与当日序号连写、
# 末尾 N=系统累计发布次数（与 versions/ 文档数一致）。发版：versions/ 新增 <版本号>.md + 改这里。
MR_ENGINE_VERSION = "26.09171.7"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("geo-agent")

BASE_DIR = Path(__file__).parent
from paths import OUTPUT_DIR, DATA_DIR, TASKS_DIR  # 可变数据统一在 var/ 下
_vp = MR_ENGINE_VERSION.split(".")
VER_LABEL = f"V_{_vp[0]}.{_vp[1][:4]}.{_vp[2]}"
SESSIONS_DIR = OUTPUT_DIR / "chat_sessions"


def save_session(sid: str, session: dict):
    SESSIONS_DIR.mkdir(parents=True, exist_ok=True)
    session["session_id"] = sid
    session["updated_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
    if "created_at" not in session:
        session["created_at"] = session["updated_at"]
    path = SESSIONS_DIR / f"{sid}.json"
    path.write_text(json.dumps(session, ensure_ascii=False, indent=2), encoding="utf-8")


def load_session(sid: str) -> dict | None:
    path = SESSIONS_DIR / f"{sid}.json"
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def list_sessions() -> list:
    SESSIONS_DIR.mkdir(parents=True, exist_ok=True)
    sessions = []
    for f in SESSIONS_DIR.glob("*.json"):
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
            display = data.get("display", [])
            preview = ""
            for msg in display:
                if msg.get("role") == "user":
                    preview = msg.get("content", "")[:30]
                    break
            sessions.append({
                "session_id": data.get("session_id", f.stem),
                "created_at": data.get("created_at", ""),
                "updated_at": data.get("updated_at", ""),
                "preview": preview,
            })
        except Exception:
            pass
    sessions.sort(key=lambda x: x.get("updated_at", ""), reverse=True)
    return sessions


def _load_dotenv(p):
    """极简 .env 加载（KEY=VALUE，已存在的环境变量不覆盖）。无文件静默跳过。"""
    try:
        for line in p.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, _, v = line.partition("=")
                k, v = k.strip(), v.strip().strip('"').strip("'")
                if k and k not in os.environ:
                    os.environ[k] = v
    except OSError:
        pass


def load_config(config_path: str = None) -> dict:
    path = Path(config_path) if config_path else BASE_DIR / "config.yaml"
    if not path.exists():
        raise FileNotFoundError(f"配置文件不存在: {path}。请复制 config.yaml 并填写 LLM API key。")
    _load_dotenv(BASE_DIR / ".env")
    with open(path, "r", encoding="utf-8") as f:
        config = yaml.safe_load(f)
    # 密钥走环境变量（.env 的 GEO_LLM_API_KEY 或真实环境变量），config.yaml 只留空位
    # ——防密钥随 git 外泄（2026-09-19 定版，历史提交中的旧密钥需在服务方轮换）
    env_key = (os.environ.get("GEO_LLM_API_KEY") or "").strip()
    if env_key:
        config.setdefault("llm", {})["api_key"] = env_key
    return config


def build_agent(config: dict, thinking: bool | None = None) -> Agent:
    llm_cfg = config["llm"]
    llm = LLMClient(
        base_url=llm_cfg["base_url"],
        api_key=llm_cfg["api_key"],
        model=llm_cfg["model"],
        temperature=llm_cfg.get("temperature", 0.7),
        max_tokens=llm_cfg.get("max_tokens", 16384),
        timeout=llm_cfg.get("timeout", 120),
        max_retries=llm_cfg.get("max_retries", 3),
        enable_search=llm_cfg.get("enable_search", False),
        enable_thinking=llm_cfg.get("enable_thinking", False),
    )
    return Agent(llm, max_concurrency=llm_cfg.get("max_concurrency", 5), thinking=thinking)


def _save_mining_facts(result: dict):
    """问句挖掘完成：把产品/客群/旅程/话题摘要写入客户档案 mining 表（供其他板块校准）。"""
    try:
        from company_cache import upsert_mining
        topics = result.get("step4_topics") or {}
        # extract_all_queries 只认单阶段结构（{"lines":[...]}），需逐阶段提取；
        # 整包传入实测恒为 0 条（2026-09-17 修复）
        queries = [q for sd in topics.values() for q in Agent.extract_all_queries(sd)] \
            if isinstance(topics, dict) else []
        journey = result.get("step3_journey") or []
        stages = []
        if isinstance(journey, list):  # 当前模型输出：阶段对象数组
            for line in journey:
                if isinstance(line, dict) and line.get("stage_name"):
                    stages.append(line["stage_name"])
        elif isinstance(journey, dict):  # 旧数据形态：{"lines":[...]}
            for line in journey.get("lines", []):
                if isinstance(line, dict) and line.get("stage_name"):
                    stages.append(line["stage_name"])
        # 话题库（阶段→话题→问句三级，含入口类型/客群信号）+ 客群 B/C 端标注
        from company_cache import derive_audience_type
        topic_tree = []
        for sk, sd in (topics.items() if isinstance(topics, dict) else []):
            if not isinstance(sd, dict):
                continue
            for tp in _iter_topics(sd):
                topic_tree.append({"stage_index": sd.get("stage_index") or sk,
                                   "stage_name": sd.get("stage_name") or f"阶段{sk}",
                                   "line": tp.get("line") or "", "topic": tp.get("topic") or "",
                                   "queries": tp.get("queries") or []})
        personas = []
        for p in (result.get("step2_personas") or []):
            if isinstance(p, dict):
                q = dict(p); q["audience_type"] = derive_audience_type(p); personas.append(q)
        payload = {
            "brand": result.get("brand"),
            "product": result.get("product"),
            "products": result.get("step1_products"),
            "personas": personas,
            "journey": journey if isinstance(journey, list) else [],
            "journey_stages": stages,
            "topics": topic_tree[:400],
            "query_count": len(queries),
            "sample_queries": [q.get("query") for q in queries[:20] if isinstance(q, dict) and q.get("query")],
            "clusters": [c for sd in (result.get("step5_clusters") or {}).values()
                         if isinstance(sd, list) for c in sd
                         if isinstance(c, dict) and c.get("cluster_name")][:60],
            "verifications": [v for v in (result.get("step6_verification") or [])
                              if isinstance(v, dict)][:40],
        }
        upsert_mining(result.get("brand") or "", payload, origin="GEO搜索问句挖掘",
                      content_brand=result.get("brand_alias") or "")
        logger.info(f"问句挖掘事实已入库: 问句 {len(queries)} 条 / 话题 {len(topic_tree)}")
    except Exception as e:
        logger.warning(f"问句挖掘事实入库失败: {e}")


def _iter_topics(stage_data: dict):
    """展平一个阶段的多线话题：产出 {line, topic, queries:[{query,entry_type,audience_signal}]}。"""
    for lr in (stage_data.get("lines") or stage_data.get("line_results") or []):
        if not isinstance(lr, dict):
            continue
        line_name = lr.get("line") or lr.get("line_name") or ""
        for t in (lr.get("topics") or []):
            if isinstance(t, dict) and t.get("topic"):
                yield {"line": line_name, "topic": t.get("topic"),
                       "queries": [q for q in (t.get("queries") or []) if isinstance(q, dict)]}


def save_output(result: dict, config: dict) -> dict:
    """Save JSON + HTML to output dir. Returns file paths."""
    if result.get("method_id") == "geo_query_mining":
        _save_mining_facts(result)
    out_cfg = config.get("output", {})
    out_dir = OUTPUT_DIR if str(out_cfg.get("dir", "./output")).rstrip("/").endswith("output") else BASE_DIR / out_cfg["dir"]
    out_dir.mkdir(parents=True, exist_ok=True)

    brand = result.get("brand", "unknown")
    ts = time.strftime("%Y%m%d_%H%M%S")
    # 文件名带方法标识，同一目录下可肉眼区分报告归属
    _tag = {"geo_query_mining": "问句挖掘", "customer_market_research": "市场调研"}.get(
        result.get("method_id"), (result.get("method_id") or "报告")[:10])
    base_name = f"{_tag}_{brand}_{ts}"
    paths = {}

    if out_cfg.get("save_json", True):
        json_path = out_dir / f"{base_name}.json"
        with open(json_path, "w", encoding="utf-8") as f:
            json.dump(result, f, ensure_ascii=False, indent=2)
        paths["json"] = str(json_path)

    if out_cfg.get("save_html", True):
        html_path = out_dir / f"{base_name}.html"
        # 调研类方法（输出 Markdown）走通用渲染；挖掘走原型模板（异常回退简版，绝不因模板丢报告）
        if result.get("method_id") == "customer_market_research":
            html = generate_markdown_report(result)
        elif result.get("method_id") == "geo_query_mining":
            try:
                from mining_report_render import render as _mr
                html = _mr(result)
            except Exception as e:
                logger.warning(f"挖掘报告原型模板渲染失败，回退简版: {e}")
                html = generate_html_report(result)
        else:
            html = generate_html_report(result)
        with open(html_path, "w", encoding="utf-8") as f:
            f.write(html)
        paths["html"] = str(html_path)

    return paths


# ── HTML Report Generator ──────────────────────────────

def _md_to_html(md: str) -> str:
    """极简 Markdown 渲染（标题/表格/列表/加粗），用于调研类报告。"""
    import html as _h
    import re as _re
    _sub = _re.sub
    if not isinstance(md, str):
        md = json.dumps(md, ensure_ascii=False, indent=2) if md else ""
    lines = (md or "").split("\n")
    out, i, in_table = [], 0, False
    def _inline(t):
        t = _h.escape(t)
        t = t.replace("&lt;br&gt;", "<br>").replace("&lt;br/&gt;", "<br>")  # 模型输出混用 HTML 换行，放行
        t = _sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", t)
        t = _sub(r"\*([^*\n]+)\*", r"<em>\1</em>", t)
        t = _sub(r"`([^`]+)`", r"<code>\1</code>", t)
        return t
    while i < len(lines):
        ln = lines[i].rstrip()
        if ln.startswith("|") and i + 1 < len(lines) and _re.match(r"^\|[\s:|-]+\|$", lines[i + 1].strip()):
            head = [c.strip() for c in ln.strip("|").split("|")]
            i += 2
            rows = []
            while i < len(lines) and lines[i].strip().startswith("|"):
                rows.append([c.strip() for c in lines[i].strip().strip("|").split("|")])
                i += 1
            out.append("<table><thead><tr>" + "".join(f"<th>{_inline(c)}</th>" for c in head) + "</tr></thead><tbody>"
                       + "".join("<tr>" + "".join(f"<td>{_inline(c)}</td>" for c in r) + "</tr>" for r in rows)
                       + "</tbody></table>")
            continue
        if ln.startswith("#### "):
            out.append(f"<h4>{_inline(ln[5:])}</h4>")
        elif ln.startswith("### "):
            out.append(f"<h3>{_inline(ln[4:])}</h3>")
        elif ln.startswith("## "):
            out.append(f"<h2>{_inline(ln[3:])}</h2>")
        elif ln.startswith("# "):
            out.append(f"<h1>{_inline(ln[2:])}</h1>")
        elif _re.match(r"^[-*]\s+", ln):
            out.append("<ul>" + "".join(f"<li>{_inline(l[2:])}</li>" for l in [ln]) + "</ul>")
        elif _re.match(r"^\d+\.\s+", ln):
            _txt = _sub(r"^\d+\.\s+", "", ln)
            out.append("<ol><li>" + _inline(_txt) + "</li></ol>")
        elif ln.startswith(">"):
            # 连续引用行合并为一个 blockquote（「本章结论」框）
            q = [_inline(ln.lstrip("> ").rstrip())]
            i += 1
            while i < len(lines) and lines[i].startswith(">"):
                q.append(_inline(lines[i].lstrip("> ").rstrip()))
                i += 1
            out.append("<blockquote>" + "<br>".join(q) + "</blockquote>")
            continue
        elif ln.strip():
            out.append(f"<p>{_inline(ln)}</p>")
        i += 1
    # 合并相邻同类列表
    merged, prev = [], None
    for blk in out:
        if prev and blk.startswith("<li>") and prev.startswith("<ul>"):
            merged[-1] = prev[:-5] + blk + "</ul>"
            prev = merged[-1]
            continue
        if prev and blk.startswith("<li>") and prev.startswith("<ol>"):
            merged[-1] = prev[:-5] + blk + "</ol>"
            prev = merged[-1]
            continue
        merged.append(blk)
        prev = blk
    return "\n".join(merged)


_CITY_NAMES = set("""北京 上海 天津 重庆 广州 深圳 成都 武汉 杭州 南京 苏州 西安 郑州 长沙 青岛
沈阳 哈尔滨 昆明 厦门 福州 无锡 佛山 东莞 宁波 合肥 济南 大连 长春 石家庄 南昌 贵阳 南宁 兰州
太原 呼和浩特 乌鲁木齐 银川 西宁 海口 拉萨 香港 澳门 高邮 佛山 中山 珠海 惠州 汕头 温州 绍兴
嘉兴 常州 南通 徐州 扬州 镇江 泰州 江都 邗江""".split())


def _node_quality(name: str) -> tuple[bool, str]:
    """组织图节点合格性：返回 (合格, 剔除原因)。城市/泛称/描述句/地址不当节点。"""
    n = str(name or "").strip()
    if not n:
        return False, "空节点"
    if len(n) > 30 or "，" in n:
        return False, "整句描述当作节点"
    if n in _CITY_NAMES or (n.endswith("市") and len(n) <= 5):
        return False, "纯城市名不是实体"
    if len(n) <= 6 and any(p in n for p in ("广东", "江苏", "浙江", "山东", "河南", "四川", "湖北", "湖南", "福建")):
        return False, "省市组合名不是实体"
    head4 = n[:4]
    if any(w in head4 for w in ("各地", "其他", "相关", "若干", "各区域", "全国", "东南亚")) or n.endswith("等"):
        return False, "泛称"
    if re.search(r"(路|街|道\d|号$|广场|大厦|大楼|科技园|产业园|工业园)", n) and len(n) >= 8:
        return False, "地址当作节点"
    if re.match(r"^(无|没有|暂无|主要依托|依托)", n):
        return False, "描述句当作节点"
    return True, ""


def _mr_brief_html(brief, brand, mr_org=None) -> str:
    """企业画像页签：按 intel-deck 模板基本面页风格渲染；无数据返回空串（页签仍显示占位）。"""
    import html as _h
    if isinstance(brief, list):  # 模型偶发把对象包成数组，取首个字典元素兜底
        brief = next((x for x in brief if isinstance(x, dict)), {})
    if not isinstance(brief, dict):
        return ""
    e = _h.escape
    info = brief.get("info") or {}
    layout = brief.get("layout") or {}
    org = brief.get("org_tree") or {}
    culture = brief.get("culture") or {}
    tl = sorted(brief.get("timeline") or [], key=lambda x: str(x.get("date", "")), reverse=True)
    gaps = [str(x) for x in (brief.get("gaps") or []) if str(x).strip()]  # 待补数据保留在JSON，页面不再展示
    if not (info or layout or org or culture or tl):
        return ""

    def _split_top(s, seps="、，"):
        # 只在中/英文括号之外按分隔符拆行，括号内的逗号不切断一句话
        depth, cur, parts = 0, "", []
        for ch in s:
            if ch in "（(":
                depth += 1
            elif ch in "）)":
                depth = max(depth - 1, 0)
            if depth == 0 and ch in seps:
                parts.append(cur)
                cur = ""
            else:
                cur += ch
        parts.append(cur)
        return [p.strip() for p in parts if p.strip()]

    def _v(x, fk=""):
        if x in (None, "", [], "None", "none", "待补"):
            return "待补"
        s = e(str(x))
        # 仅指定字段拆行加符号：行业地位（中文逗号）、资质/专利（顿号+中文逗号）；其余字段原样单行
        if fk not in ("industry_position", "qualifications", "patents"):
            return s
        if fk == "industry_position":
            parts = _split_top(s, "，")
        else:
            parts = _split_top(s)
        if len(parts) > 1:
            b = '<span style="color:#2f54eb">•</span> '
            return "<br>".join(b + p for p in parts)
        return s

    quals = "、".join(map(str, info.get("qualifications") or [])) or None
    # 专利按类型排序：发明专利 > 实用新型 > 外观设计 > 软著，同类保持原序
    def _pt_rank(x):
        s = str(x)
        for kw, r in (("发明专利", 0), ("实用新型", 1), ("外观设计", 2), ("软著", 3), ("软件著作权", 3)):
            if kw in s:
                return r
        return 9
    patents = "、".join(sorted((str(x) for x in (info.get("patents") or [])), key=_pt_rank)) or None
    org_valid = isinstance(mr_org, dict) and mr_org.get("tree") and mr_org.get("nodes")
    seed = None
    if isinstance(mr_org, dict):
        _nodes_by_id = {n["id"]: n for n in (mr_org.get("nodes") or [])}
        seed = _nodes_by_id.get(mr_org.get("seed_key"))
    from company_cache import get_info as _cache_get, derive_nature
    _cache, _cache_ts = _cache_get(brand or str(info.get("full_name") or ""))
    def _with_cache(v, cache_key):
        if v not in (None, "", [], "None", "待补") or not _cache:
            return v
        cv = _cache.get(cache_key)
        if cv in (None, "", [], "None"):
            return v
        cv = "、".join(map(str, cv)) if isinstance(cv, list) else str(cv)
        return f"{cv}（历史缓存 {_cache_ts[:10]}）"
    ig = [("成立时间", _with_cache(info.get("founded"), "founded"), "founded"),
          ("总部/地址", _with_cache(info.get("address"), "address"), "address"),
          ("行业地位", _with_cache(info.get("industry_position"), "industry_position"), "industry_position"),
          ("年营收", _with_cache(info.get("revenue"), "revenue"), "revenue_detail"),
          ("员工人数", _with_cache(info.get("employees"), "employees"), "employees"),
          ("资质", _with_cache(quals, "qualifications"), "qualifications"),
          ("专利", _with_cache(patents, "patents"), "patents"),
          ("法定代表人", (seed.get("legal_person") if seed else None) or _with_cache(None, "legal_person"), "legal_person"),
          ("创始人/实控人", (seed.get("founder") if seed else None) or _with_cache(None, "founder"), "founder")]

    # 溯源信息：从 _pipeline_meta 中获取，嵌入到 data-* 属性中（不展示，便于审计）
    _pw_meta = brief.get("_pipeline_meta") or {}
    _prov = (_pw_meta.get("provenance") or {}) if isinstance(_pw_meta, dict) else {}
    def _prov_attrs(field_key):
        p = _prov.get(field_key) or {}
        if not p:
            return ""
        src = p.get("source", "")
        mth = p.get("method", "")
        conf = p.get("confidence", "")
        fetched = p.get("fetched_at", "")
        detail = p.get("detail") or {}
        # 把 detail 转成 JSON 字符串，嵌入到 data-detail 属性中
        import json as _json
        try:
            detail_str = _json.dumps(detail, ensure_ascii=False)
        except Exception:
            detail_str = ""
        return (f' data-source="{e(src)}" data-method="{e(mth)}" '
                f'data-confidence="{e(str(conf))}" data-fetched-at="{e(fetched)}" '
                f'data-detail="{e(detail_str)}"')

    # 企业性质单一结论标签：纯API字段规则，互斥优先级命中即停
    def _chip(t):
        return ('<span style="display:inline-block;padding:3px 10px;margin:2px 6px 2px 0;'
                'background:#eef3ff;border:1px solid #c9d8ff;color:#2f54eb;border-radius:999px;'
                'font-size:12px;font-weight:600;">' + e(t) + '</span>')

    from company_cache import derive_nature as _dn, get_info as _gi
    _full = str(info.get("full_name") or brand or "")
    _arch, _ats = _gi(_full)  # 档案存储结论优先（SPEC-company-nature-unify）
    _nature = str((_arch or {}).get("nature") or "") or _dn({
        "full_name": _full,
        "company_type": str((seed or {}).get("company_type") or (_arch or {}).get("company_type") or ""),
        "staff_number": str((seed or {}).get("staff_number") or (_arch or {}).get("staff_number") or ""),
        "listed": str((_arch or {}).get("listed") or info.get("listed") or "")})
    nature_row = ('<tr><th>企业性质</th><td>'
                  + (_chip(_nature) if _nature else "待补")
                  + '</td></tr>')

    ig_html = ('<table class="info-table">' + nature_row + "".join(
        '<tr><th>' + e(k) + '</th><td' + _prov_attrs(fk) + '>' + _v(v, fk) + '</td></tr>' for k, v, fk in ig) + '</table>')

    # 网页全文抓取状态徽章（在企业画像页签顶部显示）
    _pw_status = _pw_meta.get("fetcher_status", "unknown")
    _pw_ft = _pw_meta.get("fulltext_evidence", 0)
    _pw_badge = ""
    if _pw_status == "success":
        _pw_badge = ('<div style="margin:0 0 14px 0;padding:8px 14px;background:#e8f5e9;border-left:4px solid #43a047;'
                     'border-radius:4px;font-size:13px;color:#2e7d32;line-height:1.5;">'
                     f'✓ 已通过网页全文抓取获取信息（{_pw_ft} 条证据来自全文抓取），信息完整度高于搜索摘要模式'
                     '</div>')
    elif _pw_status == "failed":
        _pw_badge = ('<div style="margin:0 0 14px 0;padding:8px 14px;background:#fff3e0;border-left:4px solid #fb8c00;'
                     'border-radius:4px;font-size:13px;color:#e65100;line-height:1.5;">'
                     '⚠ 网页全文抓取未获取到内容，已自动降级为搜索摘要模式（信息可能不完整）。'
                     '</div>')
    elif _pw_status == "disabled":
        _pw_badge = ('<div style="margin:0 0 14px 0;padding:8px 14px;background:#f5f5f5;border-left:4px solid #9e9e9e;'
                     'border-radius:4px;font-size:13px;color:#616161;line-height:1.5;">'
                     '○ 网页全文抓取未启用，当前使用搜索摘要模式'
                     '</div>')

    _truncated = []
    cells = []
    if layout.get("headquarters"):
        cells.append('<div class="org-cell">总部：' + e(str(layout["headquarters"])) + '</div>')
        ok_hq, why_hq = _node_quality(str(layout["headquarters"]))
        if not ok_hq:
            _truncated.append("总部：" + str(layout["headquarters"]) + f"（{why_hq}）")
    for key in ("branches", "subsidiaries", "sites"):
        val = layout.get(key)
        items = [str(x) for x in val if str(x).strip() and str(x) != "None"] if isinstance(val, list) else ([str(val)] if val else [])
        for x in items[:6]:
            cells.append('<div class="org-cell sub">' + e(x[:48]) + ('…' if len(x) > 48 else '') + '</div>')
    def _static_tree(item, cap):
        """静态回退：与交互图同源（根+一级子节点），CDN 失败/离线导出时可见。"""
        kids = (item.get("children") or [])[:12]
        head = ('<div class="org-cap">' + e(cap) + '</div><div class="org"><div class="org-box">'
                + e(str(item.get("name") or "")) + '</div>')
        if kids:
            head += ('<div class="org-line"></div><div class="org-row">'
                     + "".join('<div class="org-cell sub'
                               + (' dead' if '注销' in str(c.get('value') or '') else '')
                               + '">' + e(str(c.get("name") or "")[:48]) + '</div>'
                               for c in kids if c.get("name")) + '</div>')
        return head + '</div>'
 

    org2 = ""
    if org.get("name") and not org_valid:
        kids = [k for k in (org.get("children") or []) if isinstance(k, dict) and k.get("name")]
        kid_html = "".join('<div class="org-cell sub">' + e(str(k["name"])) + '</div>' for k in kids[:12])
        org2 = ('<div class="org-cap">部门架构（推断，两层示意）</div><div class="org">'
                '<div class="org-box" style="background:#3567ff">' + e(str(org["name"])) + '</div>'
                + ('<div class="org-line"></div><div class="org-row">' + kid_html + '</div>' if kid_html else '') + '</div>')

    # ECharts 径向树组织架构数据（嵌套树）：CDN 加载成功时替换静态 CSS 版，离线/失败回落静态版
    def _tn(name, pos):
        # 长名规整：括号说明并入说明行；"类型：内容"拆开——类型做节点名，内容进悬停说明
        name = str(name)
        extra = ""
        for sep in ("（", "("):
            if sep in name:
                name, _, tail = name.partition(sep)
                extra = tail.rstrip("）)")
                break
        for sep in ("：", ":"):
            if sep in name and len(name) > 8:
                head, _, tail = name.partition(sep)
                if len(head) <= 6:
                    extra = tail.strip() + ("；" + extra if extra else "")
                    name = head
                break
        name = name.strip()[:24]
        return {"name": name, "value": " · ".join(x for x in (str(pos or ""), extra) if x)}

    if org_valid:
        # 工商实名数据优先：母公司→目标企业→控股子公司，参股不进树
        _children_map = {}
        for _e in mr_org["tree"]:
            _children_map.setdefault(_e["parent"], []).append(_e["child"])
        _tree_by_child = {e["child"]: e for e in mr_org["tree"]}

        def _from_org(nid, seen):
            n = _nodes_by_id.get(nid) or {}
            item = _tn(n.get("name") or nid, "")
            te = _tree_by_child.get(nid)
            if nid == mr_org.get("seed_key"):
                v = "目标企业" + ((" · " + str(seed.get("status"))) if seed and seed.get("status") else "")
            elif nid in (mr_org.get("roots") or []):
                v = "母公司"
            elif te and te.get("rel") == "branch":
                v = "分支机构" + ((" · " + str(te["signal"])) if te.get("signal") else "")
            else:
                v = "子公司" + ((" · " + str(te["signal"])) if te and te.get("signal") else "")
                if te and te.get("confidence") == "low":
                    v += " · 待核实"
            item["value"] = v
            if "注销" in v:  # 已注销分支：红点+红标签，模板侧加删除线
                item["dead"] = True
                item["itemStyle"] = {"color": "#cf1322"}
                item["label"] = {"color": "#cf1322"}
            kid = []
            for c in _children_map.get(nid, []):
                if c == nid or c in seen:
                    continue
                seen.add(c)
                cn = (_nodes_by_id.get(c) or {}).get("name") or c
                ok, why = _node_quality(cn)
                if ok:
                    kid.append(_from_org(c, seen))
                else:
                    _truncated.append("股权关系：" + cn + f"（{why}）")
            if kid:
                item["children"] = kid
            return item

        _roots = [r for r in (mr_org.get("roots") or []) if r in _nodes_by_id]
        _seen_all = set()
        forest = []
        for _r in _roots:
            if _r in _seen_all:
                continue
            import re as _re2
            _nm = _re2.sub(r"[\s（）()：:，,。·]", "", str((_nodes_by_id.get(_r) or {}).get("name") or ""))
            def _nn(s):
                return _re2.sub(r"[\s（）()：:，,。·]", "", str(s or ""))
            if any(_nm and (_nm in _nn(t.get("name") or "") or _nn(t.get("name") or "") in _nm)
                   for t in forest):
                continue  # 同名根只保留第一棵，防重复主体
            _seen_all.add(_r)
            forest.append(_from_org(_r, _seen_all))
        if not forest:
            forest = [_tn(brand, "目标企业")]
        tree_root = forest[0] if len(forest) == 1 else forest
    else:
        tree_root = _tn(info.get("full_name") or brand, "公司主体")
        kids = tree_root["children"] = []
        for key, label in (("branches", "分支"), ("subsidiaries", "子公司"), ("sites", "基地")):
            val = layout.get(key)
            items = [str(x) for x in val if str(x).strip() and str(x) != "None"] if isinstance(val, list) else ([str(val)] if val else [])
            for x in items[:6]:
                ok, why = _node_quality(x)
                if ok:
                    kids.append(_tn(x, label))
                else:
                    _truncated.append(label + "：" + x + f"（{why}）")
            for x in items[6:]:  # 截断项转待补，不再静默丢弃
                _truncated.append(label + "：" + x)
        import re as _re_dup
        _root_nm = _re_dup.sub(r"[\s（）()：:，,。·]", "", str(info.get("full_name") or brand or ""))
        _dept_nm = _re_dup.sub(r"[\s（）()：:，,。·]", "", str(org.get("name") or ""))
        if org.get("name") and not (_dept_nm and (_dept_nm in _root_nm or _root_nm in _dept_nm)):
            def _dept_item(node):
                it = _tn(node["name"], str(node.get("role") or "部门"))
                subs = [_dept_item(c) for c in (node.get("children") or [])[:8]
                        if isinstance(c, dict) and c.get("name")]
                if subs:
                    it["children"] = subs
                return it
            dept = _tn(org["name"], "部门架构")
            dept["children"] = [_dept_item(k) for k in (org.get("children") or [])[:12]
                                if isinstance(k, dict) and k.get("name")]
            kids.append(dept)

    def _mark_expandable(node):
        if isinstance(node, list):
            for t in node:
                _mark_expandable(t)
            return node
        if node.get("children"):
            node["itemStyle"] = {"color": "#E36397"}
            node["value"] = (node.get("value") or "") + " · 有下级，点击展开/收起"
            for c in node["children"]:
                _mark_expandable(c)
        return node
    tree_root = _mark_expandable(tree_root)
    if org_valid:
        org1 = _static_tree(tree_root if not isinstance(tree_root, list) else tree_root[0],
                            "企业架构布局（工商实名核验 · 交互图加载后可展开）")
    else:
        org1 = ('<div class="org-cap">企业架构布局（公开信息推断，首访验证）</div><div class="org">'
                '<div class="org-box">' + e(str(info.get("full_name") or brand)) + '</div>'
                + ('<div class="org-line"></div><div class="org-row">' + "".join(cells) + '</div>' if cells else '') + '</div>')
    org_json = json.dumps(tree_root if isinstance(tree_root, list) else [tree_root],
                          ensure_ascii=False).replace("</", "<\\/")
    org_note = ('<div class="org-cap" style="white-space:nowrap">二级及以下股权关系未获检索数据，完整性待首访核实 · 组织架构图（粉色圆点=有下级，点击展开/收起；青色=明细；悬停看说明）</div>'
                if org_valid and isinstance(mr_org, dict) and mr_org.get("depth2_empty") else "")
    org_graph = ('<div id="orgStatic">' + org1 + org2 + '</div>'
                 + org_note
                 + '<div id="orgGraph" hidden style="height:560px"></div>'
                 '<script id="orgData" type="application/json">' + org_json + '</script>')

    cult = ""
    cult_items = [x for x in (("使命", culture.get("mission")), ("愿景", culture.get("vision")), ("价值观", culture.get("values"))) if x[1]]
    if cult_items:
        cult = ('<div class="pf-card"><div class="ct">03 · 企业文化</div><div class="cb">'
                + "<br>".join('<strong style="color:#1a1d29">' + e(k) + '</strong>：' + e(str(v))
                              for k, v in cult_items) + '</div></div>')

    weak_html = ""
    if org_valid:
        _weak_names = [_nodes_by_id[w["child"]]["name"] for w in (mr_org.get("weak") or [])
                       if w["child"] in _nodes_by_id]
        if _weak_names:
            weak_html = ('<div class="pf-card"><div class="ct">🤝 参股企业（未达控股，不进组织图）</div><div class="words">'
                         + "".join('<span class="wd">' + e(x) + '</span>' for x in _weak_names[:10]) + '</div></div>')

    tl_html = ""
    if tl:
        # 时间线：静态列表为兜底，ECharts 蛇形时间线（数据 #tlData）加载成功后替换
        # 剥离事件文本开头的来源引导语（"客户画像显示…"等），只展示内容本身
        def _ev_text(s):
            return re.sub(r'^(?:客户画像|历史档案|已知档案)(?:/[^\s，。、；]{2,12})?(?:显示|判断|称|记录)[:：]?', '', str(s)).strip()

        def _is_snap(x):  # 调研快照等非企业事件不进发展历程
            return "快照" in (str(x.get("tag") or "") + str(x.get("event") or ""))
        _tl_f = [x for x in tl if isinstance(x, dict) and not _is_snap(x)]
        _tl_items = [{"date": str(x.get("date", "")), "tag": str(x.get("tag") or ""),
                      "event": _ev_text(x.get("event") or "")}
                     for x in _tl_f]
        _tl_asc = list(reversed(_tl_items))  # 由早到近，蛇形从左到右
        _per_row = -(-len(_tl_asc) // 3) if len(_tl_asc) > 6 else 4  # 与模板一致：>6 个压成 3 行
        _rows = max(1, -(-len(_tl_asc) // _per_row))
        tl_json = json.dumps(_tl_asc, ensure_ascii=False).replace("</", "<\\/")
        tl_html = ('<div class="ct">04 · 发展历程</div>'
                   '<div class="org-cap" style="margin-bottom:8px">点击节点固定详情（可复制），悬停快速查看</div>'
                   '<div class="tl" id="tlStatic">' + "".join(
                       '<div class="tl-i"><div class="tl-y">' + e(str(x.get("date", ""))) + (('　' + e(str(x.get("tag")))) if x.get("tag") else '')
                       + '</div><div class="tl-x">' + e(_ev_text(x.get("event", ""))) + '</div></div>' for x in _tl_f) + '</div>'
                   + f'<div id="tlGraph" hidden style="height:{_rows * 130 + 60}px"></div>'
                   + '<div id="tlPin" hidden></div>'
                   + '<script id="tlData" type="application/json">' + tl_json + '</script>')

    if org_valid:
        gaps += ["组织检索存疑：" + str(u.get("claim", ""))[:40] for u in (mr_org.get("uncertain") or [])[:6]]
    gaps += ["组织信息未完整展示：" + t for t in _truncated[:6]]

    return ('<div class="pf">'
            + '<div class="pf-head" style="display:flex;justify-content:space-between;align-items:center;">'
            + '<div><div class="k">01 · 公司基本面</div><div class="t">' + e(str(brand)) + '</div></div>'
            + '<button class="pf-refresh-btn" id="refreshCompanyBtn" onclick="refreshCompanyInfo()" style="padding:8px 16px;background:linear-gradient(135deg,#3567ff,#6b8cff);color:#fff;border:none;border-radius:8px;font-size:13px;font-weight:600;cursor:pointer;box-shadow:0 2px 8px rgba(53,103,255,0.25);transition:all .2s;white-space:nowrap;" onmouseover="this.style.transform=\'translateY(-1px)\';this.style.boxShadow=\'0 4px 12px rgba(53,103,255,0.35)\'" onmouseout="this.style.transform=\'none\';this.style.boxShadow=\'0 2px 8px rgba(53,103,255,0.25)\'">🔄 更新企业信息</button>'
            + '</div>'
            + _pw_badge
            + '<div class="pf-card">' + ig_html + '</div>'
            + '<div class="pf-card"><div class="ct">02 · 企业分支机构</div>' + org_graph + '</div>'
            + cult + weak_html
            + (('<div class="pf-card">' + tl_html + '</div>') if tl_html else '')
            + '</div>')


def _mr_deck_slides_html(deck) -> str:
    """概要PPT页签的幻灯 HTML：mr_deck 输出 {"slides":[{k,t,p?,blocks[]}]}，
    块型协议见 docs/简报版式对齐规格.md（two_col/timeline/words/table/bars/media/kws/note/src）。
    旧格式（items 列表）继续渲染为要点卡；解析失败返回空串。"""
    import html as _h

    def esc(x):
        return _h.escape(str(x))

    def b_two_col(b, _h=esc):
        cs = []
        for c in (b.get("cards") or [])[:2]:
            rows = "".join(f'<div class="sb-r"><span class="rk">{_h(r[0])}</span>'
                           f'<span class="rv">{_h(r[1] if len(r) > 1 else "")}</span></div>'
                           for r in (c.get("rows") or []) if isinstance(r, (list, tuple)) and r)
            cs.append(f'<div class="sb-card"><div class="sb-ct">{_h(c.get("title") or "")}</div>{rows}</div>')
        cls = "sb sb-two" + ("" if len(cs) == 2 else " single")
        return f'<div class="{cls}">' + "".join(cs) + "</div>"

    def b_timeline(b, _h=esc):
        items = "".join(f'<div class="sb-ti"><div class="sb-ty">{_h(x[0])}</div>'
                        f'<div class="sb-tx">{_h(x[1] if len(x) > 1 else "")}</div></div>'
                        for x in (b.get("items") or []) if isinstance(x, (list, tuple)) and x)
        return f'<div class="sb sb-tl">{items}</div>'

    def _chips(chips, tone, _h=esc):
        out = []
        for ch in (chips or [])[:10]:
            w, src = (ch, "") if isinstance(ch, str) else (ch.get("w", ""), ch.get("src", ""))
            if not str(w).strip():
                continue
            tag = f'<i>{_h(src)}</i>' if src.strip() else ""
            out.append(f'<span class="sb-wd {tone}">{_h(w)}{tag}</span>')
        return "".join(out)

    def b_words(b, _h=esc):
        cols = []
        tones = ("pos", "neg", "mid")
        for i, c in enumerate(b.get("cols") or []):
            cols.append(f'<div class="sb-wcol"><div class="sb-ct">{_h(c.get("title") or "")}</div>'
                        f'<div>{_chips(c.get("chips"), tones[min(i, 2)], _h)}</div></div>')
        return '<div class="sb sb-words">' + "".join(cols) + "</div>"

    def b_table(b, _h=esc):
        head = b.get("head") or []
        th = "<tr>" + "".join(f"<th>{_h(x)}</th>" for x in head) + "</tr>" if head else ""
        trs = "".join("<tr>" + "".join(f"<td>{_h(c)}</td>" for c in (r or [])) + "</tr>"
                      for r in (b.get("rows") or [])[:6])
        return f'<div class="sb sb-tablewrap"><table>{th}{trs}</table></div>'

    def b_bars(b, _h=esc):
        items = []
        for it in (b.get("items") or [])[:6]:
            name, desc, note = _h(it.get("name") or ""), _h(it.get("desc") or ""), _h(it.get("note") or "")
            sc = it.get("score")
            if isinstance(sc, (int, float)) and not isinstance(sc, bool):
                sc = max(0, min(100, int(sc)))
                bar, lab = f'<div style="width:{sc}%"></div>', f"适配度 {sc} · {note}" if note else f"适配度 {sc}"
            else:
                bar, lab = "", f"待补充 · {note}" if note else "待补充"
            items.append(f'<div class="sb-bc"><div class="bn">{name}</div>'
                         f'<div class="bd">{desc}</div>'
                         f'<div class="sb-bbg">{bar}</div><div class="bp">{lab}</div></div>')
        return '<div class="sb sb-bars">' + "".join(items) + "</div>"

    def b_media(b, _h=esc):
        gs = []
        for g in (b.get("groups") or [])[:2]:
            rows = "".join(
                f'<div class="sb-mr"><span>{_h(r[0])}</span>'
                f'<span class="sb-st {_h(r[1] if r[1] in ("y", "n", "q") else "q")}">{_h(r[2] if len(r) > 2 else "")}</span></div>'
                for r in (g.get("rows") or []) if isinstance(r, (list, tuple)) and r)
            gs.append(f'<div class="sb-card"><div class="sb-ct">{_h(g.get("title") or "")}</div>{rows}</div>')
        return '<div class="sb sb-media">' + "".join(gs) + "</div>"

    def b_kws(b, _h=esc):
        tones = ("d1", "d2", "d3")
        cs = []
        for i, c in enumerate(b.get("cols") or []):
            cs.append(f'<div class="sb-kw {tones[min(i, 2)]}"><div class="kn">{_h(c.get("tier") or "")}</div>'
                      f'<div class="kd">{_h(c.get("desc") or "")}</div><div>{_chips(c.get("chips"), "mid", _h)}</div></div>')
        return '<div class="sb sb-kws">' + "".join(cs) + "</div>"

    def b_note(b, _h=esc):
        return f'<div class="sb sb-note">{_h(b.get("text") or "")}</div>'

    def b_src(b, _h=esc):
        return f'<div class="sb sb-src">{_h(b.get("text") or "")}</div>'

    _BLOCKS = {"two_col": b_two_col, "timeline": b_timeline, "words": b_words, "table": b_table,
               "bars": b_bars, "media": b_media, "kws": b_kws, "note": b_note, "src": b_src}

    if isinstance(deck, str):
        try:
            deck = json.loads(deck)
        except (ValueError, TypeError):
            return ""
    if not isinstance(deck, dict):
        return ""
    out = []
    for s in deck.get("slides") or []:
        if not isinstance(s, dict) or not s.get("t"):
            continue
        k = esc(s.get("k") or "")
        t = esc(s.get("t"))
        b = ""
        p = str(s.get("p") or "").strip()
        if p:
            b += f"<p>{esc(p)}</p>"
        for blk in s.get("blocks") or []:
            if not isinstance(blk, dict):
                continue
            fn = _BLOCKS.get(str(blk.get("type") or ""))
            b += fn(blk) if fn else b_note({**blk, "text": blk.get("text") or blk.get("note") or "未知内容块"})
        if not (s.get("blocks")):  # 旧格式兼容：纯 items 要点卡
            items = [esc(x) for x in (s.get("items") or []) if str(x).strip()]
            if items:
                b += "<ul>" + "".join(f"<li>{i}</li>" for i in items) + "</ul>"
        out.append(f'<div class="slide"><div class="slide-k">{k}</div>'
                   f'<div class="slide-t">{t}</div><div class="slide-b">{b}</div></div>')
    return "".join(out)


def generate_markdown_report(result: dict) -> str:
    """客户市场调研：三页签报告——「企业画像」+「销售情报简报」+「完整调研报告」三页签固定显示。

    版式来自 skills/mr_report_template.html（单文件、无外部依赖、导出后离线可用）。"""
    import html as _h
    brand = result.get("brand", "")
    method_name = result.get("method_name", "市场调研")
    sections = [k for k in ("mr_profile", "mr_marketing", "mr_opportunity",
                             "mr_plan", "mr_proposal", "mr_sales")
                if result.get(k)]
    body = "".join(_md_to_html(result[k]) for k in sections)
    slides = _mr_deck_slides_html(result.get("mr_deck"))
    brief_html = _mr_brief_html(result.get("mr_brief"), brand, result.get("mr_org"))
    tpl = (BASE_DIR / "skills" / "mr_report_template.html").read_text(encoding="utf-8")
    title = f"{brand} · 市场调研报告"
    _te = int(round(result.get("total_elapsed", 0)))
    _dur = f"{_te // 60}分{_te % 60}秒"
    meta = f"{time.strftime('%Y-%m-%d')} · 耗时 {_dur}"
    out = (tpl.replace("__TITLE__", _h.escape(title))
              .replace("__META__", _h.escape(meta))
              .replace("__SLIDES__", slides)
              .replace("__BRIEF__", brief_html)
              .replace('<div class="brief-empty">企业画像内容待生成</div>', "" if brief_html else '<div class="brief-empty">企业画像内容待生成</div>')
              .replace("__TOTAL_PAGES__", str(max(len(re.findall(r'class="slide"', slides)), 1)))
              .replace("__REPORT__", body))
    # 三个页签固定显示；缺数据的页签点开显示占位提示，默认停在有内容的那一页
    if not slides:
        out = out.replace('<div class="deck-stage" id="deckStage">',
                          '<div class="deck-stage" id="deckStage"><div class="brief-empty">情报简报内容待生成</div>')
    default = "brief" if brief_html else ("deck" if slides else "report")
    out = out.replace("rptTab('deck');", "rptTab('" + default + "');")
    # 隐藏版本标识（仅源码可见），排查问题时区分报告由哪一版引擎生成；逻辑或模板改动须 +1
    out = out.replace("</head>", f"<!-- 市场调研引擎版本: {MR_ENGINE_VERSION} ｜ 生成时间: {meta} -->\n</head>", 1)
    return out


def generate_html_report(result: dict) -> str:
    brand = result.get("brand", "")
    product = result.get("product")
    if product:
        product_name = product
    else:
        products = result.get("step1_products", [])
        if products and isinstance(products[0], dict):
            product_name = products[0].get("product_name", "自动选择")
        else:
            product_name = "自动选择"
    elapsed = result.get("total_elapsed", 0)

    sections = []
    sections.append(_html_meta(brand, product_name, elapsed))
    sections.append(_html_step1(result.get("step1_products", [])))
    sections.append(_html_step1_5(result.get("step1_5_variants", {})))
    sections.append(_html_step2(result.get("step2_personas", [])))
    sections.append(_html_step3(result.get("step3_journey", [])))
    sections.append(_html_step3_5(result.get("step3_5_dual_entry", [])))
    sections.append(_html_step4(result.get("step4_topics", {})))
    sections.append(_html_step5(result.get("step5_clusters", {})))

    css = """
    * { margin: 0; padding: 0; box-sizing: border-box; }
    body { font-family: -apple-system, "PingFang SC", "Microsoft YaHei", sans-serif; background: #ffffff; color: #091026; line-height: 1.6; }
    .container { max-width: 960px; margin: 0 auto; padding: 40px 24px; }
    h1 { font-size: 28px; margin-bottom: 8px; }
    h2 { font-size: 22px; margin: 32px 0 16px; padding-bottom: 8px; border-bottom: 2px solid #3567ff; }
    h3 { font-size: 17px; margin: 20px 0 10px; }
    .meta { background: #fff; border-radius: 12px; padding: 16px 20px; margin-bottom: 24px; box-shadow: 0 1px 4px rgba(0,0,0,0.06); }
    .meta p { margin: 4px 0; color: #6e6e73; }
    .meta strong { color: #091026; }
    .card { background: #fff; border-radius: 12px; padding: 20px; margin-bottom: 16px; box-shadow: 0 1px 4px rgba(0,0,0,0.06); }
    table { width: 100%; border-collapse: collapse; margin: 8px 0; }
    th { background: #eff1f6; padding: 8px 12px; text-align: left; font-size: 13px; font-weight: 600; }
    td { padding: 8px 12px; border-bottom: 1px solid #e8e8ed; font-size: 14px; vertical-align: top; }
    .tag { display: inline-block; padding: 2px 8px; border-radius: 4px; font-size: 12px; margin: 1px; }
    .tag-a { background: #e3f0ff; color: #3567ff; }
    .tag-b { background: #fff4e3; color: #c97000; }
    .badge { display: inline-block; padding: 2px 8px; border-radius: 4px; font-size: 12px; font-weight: 600; }
    .pass { background: #d4edda; color: #155724; }
    .check { background: #fff3cd; color: #856404; }
    .cluster { border-left: 3px solid #3567ff; padding-left: 16px; margin: 12px 0; }
    .query-item { padding: 6px 0; border-bottom: 1px solid #eff1f6; }
    .signal { color: #6e7991; font-size: 13px; }
    .stage-block { margin: 16px 0; }
    ol, ul { padding-left: 20px; }
    li { margin: 4px 0; }
    """

    return (
        '<!DOCTYPE html><html lang="zh-CN"><head>'
        f'<meta charset="UTF-8"><meta name="viewport" content="width=device-width, initial-scale=1.0">'
        f'<title>GEO搜索问句挖掘报告 - {brand}</title>'
        f'<style>{css}</style></head><body>'
        f'<div class="container">'
        + "\n".join(sections)
        + "</div></body></html>"
    )


def _html_meta(brand, product, elapsed):
    return f"""
    <h1>GEO搜索问句挖掘与意图分析报告</h1>
    <div class="meta">
        <p><strong>品牌:</strong> {brand}</p>
        <p><strong>目标产品:</strong> {product}</p>
        <p><strong>耗时:</strong> {elapsed}s</p>
        <p><strong>生成时间:</strong> {time.strftime('%Y-%m-%d %H:%M:%S')}</p>
    </div>"""


def _html_step1(products):
    if not products:
        return "<h2>Step 1 · 核心产品/服务</h2><div class='card'>无数据</div>"
    rows = ""
    for p in products:
        if not isinstance(p, dict):
            continue
        rows += "<tr>"
        rows += f"<td>{p.get('product_name', '')}</td>"
        rows += f"<td>{p.get('positioning', '')}</td>"
        rows += f"<td>{p.get('target_audience', '')}</td>"
        rows += f"<td>{p.get('promotion_basis', '')}</td>"
        rows += "</tr>"
    return f"""
    <h2>Step 1 · 核心产品/服务</h2>
    <table><thead><tr>
        <th>产品/服务</th><th>定位</th><th>目标客群</th><th>主推依据</th>
    </tr></thead><tbody>{rows}</tbody></table>"""


def _html_step1_5(variants):
    if not variants:
        return "<h2>Step 1.5 · 名称变体</h2><div class='card'>无数据</div>"
    sections = ""
    for product_name, variant_list in variants.items():
        if not isinstance(variant_list, list):
            continue
        rows = ""
        for v in variant_list:
            if not isinstance(v, dict):
                continue
            rows += "<tr>"
            rows += f"<td>{v.get('type', '')}</td>"
            rows += f"<td>{v.get('variant', '')}</td>"
            rows += f"<td>{v.get('who_searches', '')}</td>"
            rows += "</tr>"
        sections += f"<h3>{product_name}</h3>"
        sections += f"<table><thead><tr><th>类型</th><th>变体</th><th>谁在搜</th></tr></thead><tbody>{rows}</tbody></table>"
    return f"<h2>Step 1.5 · 名称变体</h2>{sections}"


def _html_step2(personas):
    if not personas:
        return "<h2>Step 2 · 目标客群</h2><div class='card'>无数据</div>"
    cards = ""
    for p in personas:
        if not isinstance(p, dict):
            continue
        traits = "".join(f"<li>{t}</li>" for t in p.get("core_traits", []))
        cards += f"""
        <div class="card">
            <h3>{p.get('name', '')}</h3>
            <p><strong>定义:</strong> {p.get('definition', '')}</p>
            <p><strong>核心特征:</strong></p><ul>{traits}</ul>
            <p><strong>典型动机:</strong> {p.get('typical_motivation', '')}</p>
            <p><strong>决策习惯:</strong> {p.get('decision_habit', '')}</p>
            <p><strong>销售策略:</strong> {p.get('sales_strategy', '')}</p>
        </div>"""
    return f"<h2>Step 2 · 目标客群</h2>{cards}"


def _html_step3(journey):
    if not journey:
        return "<h2>Step 3 · 决策旅程</h2><div class='card'>无数据</div>"
    rows = ""
    for s in journey:
        if not isinstance(s, dict):
            continue
        rows += "<tr>"
        rows += f"<td>{s.get('stage_index', '')}</td>"
        rows += f"<td>{s.get('stage_name', '')}</td>"
        rows += f"<td>{s.get('user_state', '')}</td>"
        rows += f"<td>{s.get('core_concern', '')}</td>"
        rows += "</tr>"
    return f"""
    <h2>Step 3 · 决策旅程</h2>
    <table><thead><tr>
        <th>阶段</th><th>名称</th><th>用户状态</th><th>核心关注</th>
    </tr></thead><tbody>{rows}</tbody></table>"""


def _html_step3_5(dual_entries):
    if not dual_entries:
        return "<h2>Step 3.5 · 双搜索入口</h2><div class='card'>无数据</div>"
    cards = ""
    for d in dual_entries:
        if not isinstance(d, dict):
            continue
        hints = ", ".join(d.get("entry_b_combination_hints", []))
        cards += f"""
        <div class="card">
            <h3>阶段 {d.get('stage_index', '')}</h3>
            <p><span class="tag tag-a">入口A(产品名)</span> {d.get('entry_a', '')}</p>
            <p><span class="tag tag-b">入口B(问题型)</span> {d.get('entry_b', '')}</p>
            <p><strong>入口B要素组合提示:</strong> {hints}</p>
        </div>"""
    return f"<h2>Step 3.5 · 双搜索入口</h2>{cards}"


def _html_step4(topics):
    if not topics:
        return "<h2>Step 4 · 各阶段话题与问句</h2><div class='card'>无数据</div>"
    sections = ""
    for stage_key, stage_data in topics.items():
        # 两代流水线键兼容：skills 版 stage_name 直挂顶层、线数据键为 lines；旧 pipeline 版为 stage/line_results
        stage = stage_data.get("stage", {})
        stage_name = stage.get("stage_name") or stage_data.get("stage_name") or f"阶段{stage_key}"
        sections += f"<h3>阶段 {stage_key}: {stage_name}</h3>"

        for line_result in stage_data.get("line_results") or stage_data.get("lines", []):
            if not isinstance(line_result, dict) or not line_result.get("topics"):
                continue
            line_name = line_result.get("line", "")
            sections += f"<div class='card'><strong>{line_name}</strong>"
            for topic in line_result["topics"]:
                if not isinstance(topic, dict):
                    continue
                sections += f"<h4>{topic.get('topic', '')}</h4>"
                for q in topic.get("queries", []):
                    if not isinstance(q, dict):
                        continue
                    entry_tag = "tag-a" if q.get("entry_type", "").startswith("产品") else "tag-b"
                    sections += (
                        f"<div class='query-item'>"
                        f"<span class='tag {entry_tag}'>{q.get('entry_type', '')}</span> "
                        f"{q.get('query', '')}"
                        f"<div class='signal'>{q.get('audience_signal', '')}</div>"
                        f"</div>"
                    )
            sections += "</div>"

        gap = stage_data.get("gap_fill") or stage_data.get("gap_fills") or {}
        gap_list = gap.get("gap_fills", []) if isinstance(gap, dict) else (gap if isinstance(gap, list) else [])
        if gap_list:
            sections += "<div class='card'><strong>补漏问句</strong>"
            for gf in gap_list:
                if not isinstance(gf, dict):
                    continue
                entry_tag = "tag-a" if gf.get("entry_type", "").startswith("产品") else "tag-b"
                sections += (
                    f"<div class='query-item'>"
                    f"<span class='tag {entry_tag}'>{gf.get('entry_type', '')}</span> "
                    f"{gf.get('query', '')}"
                    f"<div class='signal'>{gf.get('audience_signal', '')}</div>"
                    f"</div>"
                )
            sections += "</div>"

    return f"<h2>Step 4 · 各阶段话题与问句</h2>{sections}"


def _html_step5(clusters_by_stage):
    if not clusters_by_stage:
        return "<h2>Step 5 · 意图簇</h2><div class='card'>无数据</div>"
    sections = ""
    for stage_key, stage_data in clusters_by_stage.items():
        # 三形态兼容：模型按配表约定输出簇数组；旧数据为 {stage,clusters} 或单簇对象
        if isinstance(stage_data, list):
            stage_name, clusters = f"阶段{stage_key}", stage_data
        elif isinstance(stage_data, dict) and "cluster_name" in stage_data:
            stage_name, clusters = f"阶段{stage_key}", [stage_data]
        elif isinstance(stage_data, dict):
            stage_name = (stage_data.get("stage") or {}).get("stage_name") or f"阶段{stage_key}"
            clusters = stage_data.get("clusters", [])
        else:
            continue
        sections += f"<h3>阶段 {stage_key}: {stage_name}</h3>"
        for cluster in clusters:
            if not isinstance(cluster, dict):
                continue
            sections += f"<div class='cluster card'>"
            sections += f"<strong>{cluster.get('cluster_name', '')}</strong>"
            for q in cluster.get("queries", []):
                if not isinstance(q, dict):
                    continue
                entry_tag = "tag-a" if q.get("entry_type", "").startswith("产品") else "tag-b"
                sections += (
                    f"<div class='query-item'>"
                    f"<span class='tag {entry_tag}'>{q.get('entry_type', '')}</span> "
                    f"{q.get('query', '')}"
                    f"<div class='signal'>{q.get('audience_signal', '')}</div>"
                    f"</div>"
                )
            sections += "</div>"
    return f"<h2>Step 5 · 意图簇</h2>{sections}"


def _html_step6(verifications):
    if not verifications:
        return "<h2>Step 6 · 反演验证</h2><div class='card'>无数据</div>"
    rows = ""
    for v in verifications:
        if not isinstance(v, dict):
            continue
        conclusion = v.get("conclusion", "")
        badge_class = "pass" if "一致" in conclusion else "check"
        rows += "<tr>"
        rows += f"<td>{v.get('stage_index', '')}</td>"
        rows += f"<td>{v.get('cluster_name', '')}</td>"
        rows += f"<td>{', '.join(v.get('reverse_queries', []))}</td>"
        rows += f"<td>{v.get('coverage_check', '')}</td>"
        rows += f"<td><span class='badge {badge_class}'>{conclusion}</span></td>"
        rows += "</tr>"
    return f"""
    <h2>Step 6 · 反演验证</h2>
    <table><thead><tr>
        <th>阶段</th><th>意图簇</th><th>反演问句</th><th>覆盖检查</th><th>结论</th>
    </tr></thead><tbody>{rows}</tbody></table>"""


def _html_self_check(checks):
    if not checks:
        return "<h2>自校验清单</h2><div class='card'>无数据</div>"
    rows = ""
    for c in checks:
        if not isinstance(c, dict):
            continue
        status = c.get("status", "")
        badge_class = "pass" if status == "pass" else "check"
        badge_text = "通过" if status == "pass" else "待检查"
        rows += "<tr>"
        rows += f"<td>{c.get('item', '')}</td>"
        rows += f"<td><span class='badge {badge_class}'>{badge_text}</span></td>"
        rows += f"<td>{c.get('detail', '')}</td>"
        rows += "</tr>"
    return f"""
    <h2>自校验清单</h2>
    <table><thead><tr>
        <th>检查项</th><th>状态</th><th>详情</th>
    </tr></thead><tbody>{rows}</tbody></table>"""


# ── FastAPI Server ──────────────────────────────────

app = FastAPI(title="GEO Query Mining Agent", version="1.0.0")

# 代码知识图谱可视化（graphify 建图产物，rebuild 后刷新页面即可）
try:
    from fastapi.staticfiles import StaticFiles
    _graph_dir = BASE_DIR / "graphify-out"
    if _graph_dir.is_dir():
        app.mount("/graph", StaticFiles(directory=_graph_dir, html=True), name="graph")
except Exception:
    pass  # 图谱目录不存在（未建图）时跳过挂载，不影响服务


class AnalyzeRequest(PydanticModel):
    brand: str = Field(..., description="企业/品牌名称（用户确认后的企业全称）")
    product: Optional[str] = Field(None, description="目标产品/服务(可选)")
    method: Optional[str] = Field(None, description="分析方法ID(可选)")
    brand_alias: Optional[str] = Field(None, description="品牌别名/用户原始输入（如'货拉拉'），用于搜索百度百科")
    force_refresh: bool = Field(False, description="是否强制刷新企业基本信息（绕过本地缓存，调用工商API获取最新数据）")


class AnalyzeResponse(PydanticModel):
    brand: str
    product: Optional[str]
    elapsed: float
    files: dict
    result_keys: list


class ContentUpdate(PydanticModel):
    content: str = Field(..., description="File content")


def _safe_filename(name: str) -> bool:
    return all(c.isalnum() or c in "._-" for c in name) and name.count(".") <= 2


def build_chat_agent(config: dict) -> ChatAgent:
    llm_cfg = config["llm"]
    llm = LLMClient(
        base_url=llm_cfg["base_url"],
        api_key=llm_cfg["api_key"],
        model=llm_cfg["model"],
        temperature=llm_cfg.get("temperature", 0.7),
        max_tokens=llm_cfg.get("max_tokens", 16384),
        timeout=llm_cfg.get("timeout", 120),
        max_retries=llm_cfg.get("max_retries", 3),
        enable_search=llm_cfg.get("enable_search", False),
        enable_thinking=llm_cfg.get("enable_thinking", False),
    )
    return ChatAgent(llm, max_concurrency=llm_cfg.get("max_concurrency", 5))


class ChatRequest(PydanticModel):
    message: str = Field(..., description="用户消息")
    session_id: str = Field("default", description="会话ID")


chat_sessions: dict = {}


# ── 官网诊断（site_audit）────────────────────────────

class AuditRequest(PydanticModel):
    url: str = Field(..., description="要诊断的官网网址")


class BatchAuditRequest(PydanticModel):
    urls: list[str] = Field(..., description="要诊断的官网网址列表")


class AuditBindRequest(PydanticModel):
    filename: str = Field(..., description="诊断报告文件名主干（不含扩展名）")
    company: str = Field(..., description="人工选定的企业工商全称")


AUDIT_JOBS: dict = {}  # job_id -> {"status": running/done/error, "url":..., "result":..., "started": ts}
AUDIT_MAX_ACTIVE = 10  # 进行中（含批量）任务总上限，超出禁止新增


def _active_audit_count() -> int:
    return sum(1 for j in AUDIT_JOBS.values() if j["status"] == "running")


def _start_audit_job(url: str) -> str:
    """登记并后台启动一个诊断任务，返回 job_id。"""
    job_id = uuid.uuid4().hex[:12]
    AUDIT_JOBS[job_id] = {"status": "running", "url": url, "started": time.time()}
    llm_config = load_config().get("llm", {})
    audit_dir = OUTPUT_DIR / "audit"

    def _run():
        try:
            result = run_site_audit(url, audit_dir, 12, llm_config)
            AUDIT_JOBS[job_id]["result"] = result
            _audit_bind_company_sync(result)  # 先绑定写回再报完成：列表刷新时绑定结果必已落盘
            AUDIT_JOBS[job_id]["status"] = "done"
        except Exception as e:
            AUDIT_JOBS[job_id]["status"] = "error"
            AUDIT_JOBS[job_id]["result"] = {"ok": False, "error": f"诊断过程异常：{type(e).__name__}: {e}"}
        AUDIT_JOBS[job_id]["finished"] = time.time()

    threading.Thread(target=_run, daemon=True).start()
    return job_id


async def _audit_bind_company(result: dict):
    """诊断完成后：按规格执行企业身份精确绑定（站内全称提取→工商精准核验→唯一才绑；
    英文名/多候选/零命中落 company_candidates 供人工选定），工商字段沉淀进企业信息缓存
    （存在即更新、不存在即创建，与其他分析页面同口径）。失败不影响诊断，但必须写日志留痕。"""
    if not result or not result.get("ok") or not result.get("json_file"):
        return
    try:
        from audit_company_bind import bind_report
        from business_api_client import get_business_api_client
        from company_cache import upsert_info
        jf = OUTPUT_DIR / "audit" / result["json_file"]
        summary = await bind_report(jf, get_business_api_client(), upsert_info)
        if summary.get("action") in ("bound", "candidates"):
            logger.info("官网诊断企业绑定 %s: %s", summary.get("action"), summary)
        elif summary.get("action") in ("fallback_failed", "error"):
            # 失败必须留痕：缺库/超时/异常绝不许被当成"站内没有"
            logger.warning("官网诊断企业绑定未完成 %s: %s", summary.get("action"), summary)
    except Exception as e:
        logger.warning("官网诊断企业绑定异常: %s: %s", type(e).__name__, e)


def _audit_bind_company_sync(result: dict):
    """线程/同步上下文用的包装（诊断跑在线程里，无事件循环）。"""
    try:
        asyncio.run(_audit_bind_company(result))
    except Exception:
        pass


@app.post("/api/audit/start")
async def api_audit_start(req: AuditRequest):
    """提交诊断任务立即返回 job_id（诊断在后台线程执行）。

    诊断耗时 1~3 分钟，走同步接口时会被链路上的代理/nginx 掐断返回 HTML 错误页，
    前端按 JSON 解析就报 "Unexpected token '<'"——改成提交+轮询彻底避开长连接。"""
    url = (req.url or "").strip()
    if not url:
        return JSONResponse({"ok": False, "error": "请输入要诊断的网址"}, status_code=400)
    if _active_audit_count() >= AUDIT_MAX_ACTIVE:
        return JSONResponse({"ok": False,
                             "error": f"进行中的诊断任务已达上限（{AUDIT_MAX_ACTIVE} 个），请等待完成后再发起"},
                            status_code=429)
    _log_op("官网诊断", "发起检测", url, "后台任务方式")
    return JSONResponse({"ok": True, "job_id": _start_audit_job(url)})


@app.post("/api/audit/batch")
async def api_audit_batch(req: BatchAuditRequest):
    """批量创建诊断任务：整批一起受理，进行中的任务总数不得超过上限。"""
    urls = list(dict.fromkeys(u.strip() for u in req.urls if u and u.strip()))
    if not urls:
        return JSONResponse({"ok": False, "error": "请至少输入一个网址"}, status_code=400)
    if len(urls) > AUDIT_MAX_ACTIVE:
        return JSONResponse({"ok": False,
                             "error": f"单次批量任务不能超过 {AUDIT_MAX_ACTIVE} 个（当前提交 {len(urls)} 个）"},
                            status_code=400)
    active = _active_audit_count()
    if active + len(urls) > AUDIT_MAX_ACTIVE:
        return JSONResponse({"ok": False,
                             "error": f"进行中的任务 {active} 个 + 本次 {len(urls)} 个 超过上限 {AUDIT_MAX_ACTIVE}，请等待部分完成后再添加"},
                            status_code=429)
    _log_op("官网诊断", "批量发起检测", f"{len(urls)} 个网址", "；".join(urls))
    jobs = [{"job_id": _start_audit_job(u), "url": u} for u in urls]
    return JSONResponse({"ok": True, "jobs": jobs})


@app.get("/api/audit/jobs")
async def api_audit_jobs():
    """任务面板数据：进行中 + 最近完成/失败的任务（不含报告正文）。"""
    # 完成/失败超1小时的任务连结果一起清掉，防常驻内存里攒大对象
    for jid in [k for k, v in AUDIT_JOBS.items()
                if v["status"] != "running" and time.time() - v.get("finished", v["started"]) > 3600]:
        AUDIT_JOBS.pop(jid, None)
    items = []
    for jid, j in AUDIT_JOBS.items():
        if j["status"] == "running":
            items.append({"job_id": jid, "url": j.get("url", ""), "status": "running",
                          "elapsed": int(time.time() - j["started"])})
        elif time.time() - j.get("finished", j["started"]) < 600:
            res = j.get("result") or {}
            err = ""
            if j["status"] == "error" or res.get("ok") is False:
                err = str(res.get("error", "诊断失败"))[:120]
            items.append({"job_id": jid, "url": j.get("url", ""),
                          "status": "error" if err else "done",
                          "elapsed": int(j.get("finished", j["started"]) - j["started"]), "error": err})
    items.sort(key=lambda x: x["elapsed"], reverse=True)
    return JSONResponse({"ok": True, "jobs": items,
                         "active": _active_audit_count(), "max_active": AUDIT_MAX_ACTIVE})


@app.get("/api/audit/job/{job_id}")
async def api_audit_job(job_id: str):
    job = AUDIT_JOBS.get(job_id)
    if not job:
        return JSONResponse({"ok": False,
                             "error": "任务不存在（可能服务刚重启过），请重新发起诊断"}, status_code=404)
    if job["status"] == "running":
        return JSONResponse({"ok": True, "status": "running",
                             "elapsed": int(time.time() - job["started"])})
    return JSONResponse({"ok": True, "status": job["status"],
                         "result": job.get("result") or {"ok": False, "error": "未知错误"}})


@app.post("/api/audit")
async def api_audit(req: AuditRequest):
    """运行 GEO 官网诊断，生成 HTML 报告，返回摘要。"""
    url = (req.url or "").strip()
    if not url:
        return JSONResponse({"ok": False, "error": "请输入要诊断的网址"}, status_code=400)
    _log_op("官网诊断", "发起检测", url, "同步接口方式")
    audit_dir = OUTPUT_DIR / "audit"
    # 传入LLM配置以启用AI语义检查
    llm_config = load_config().get("llm", {})
    result = await asyncio.to_thread(run_site_audit, url, audit_dir, 15, llm_config)
    if not result.get("ok"):
        return JSONResponse(result, status_code=422)
    await _audit_bind_company(result)
    return JSONResponse(result)


@app.get("/audit-result/{filename:path}")
async def get_audit_result(filename: str):
    """提供官网诊断生成的 HTML / JSON 报告文件。"""
    audit_dir = OUTPUT_DIR / "audit"
    file_path = audit_dir / filename
    if not file_path.exists():
        return JSONResponse({"error": "file not found"}, status_code=404)
    if filename.endswith(".html"):
        return HTMLResponse(content=file_path.read_text(encoding="utf-8"), headers={"Cache-Control": "no-store"})
    if filename.endswith(".json"):
        return FileResponse(file_path, media_type="application/json", filename=filename)
    return FileResponse(file_path)


def _apply_priority_config_to_reports() -> int:
    """优先级配置变更后重算全部历史报告：按新优先级重算计分/排序并重写 HTML+JSON。

    历史报告的 JSON 里存有每项原始结果（状态/结论/证据），重建报告对象后只需
    换优先级、重算维度分与总分、重跑优先级标记与根因聚合，再重新渲染，无需重爬。
    """
    from site_audit.core import aggregator, priority_config
    from site_audit.core.models import Priority
    from site_audit.core.scorer import (calculate_category_scores,
                                        calculate_total_score, grade_from_score)
    from site_audit.report.html_report import save_html_report
    from site_audit.report.json_report import report_from_dict, save_json_report

    audit_dir = OUTPUT_DIR / "audit"
    overrides = priority_config.load_overrides()
    defaults = {it["id"]: it["default_priority"] for it in priority_config.item_catalog()}
    updated = 0
    for json_path in sorted(audit_dir.glob("*.json")):
        try:
            data = json.loads(json_path.read_text(encoding="utf-8"))
            report = report_from_dict(data)
            for r in report.results:
                # 先归位目录默认值再套覆盖——历史 JSON 里的优先级可能已是上次覆盖后的值
                default = defaults.get(r.item_id)
                if default:
                    r.priority = Priority(default)
                if r.item_id in overrides:
                    r.priority = Priority(overrides[r.item_id])
            report.categories = calculate_category_scores(report.results)
            report.total_score = calculate_total_score(report.categories)
            report.grade = grade_from_score(report.total_score)
            aggregator.annotate(report.results)
            report.stories = aggregator.aggregate_stories(report.results)
            html_path = json_path.with_suffix(".html")
            if html_path.exists():
                save_html_report(report, str(html_path))
            save_json_report(report, str(json_path))
            updated += 1
        except Exception as e:
            logger.warning("按新优先级重渲染历史报告失败 %s: %s", json_path.name, e)
    return updated


class PriorityConfigSave(PydanticModel):
    overrides: dict = Field(default_factory=dict,
                            description="检查项编号 -> P0/P1/P2；空对象表示全部恢复默认")


@app.get("/brand/logo.png")
async def brand_logo():
    """导航条品牌 Logo。"""
    logo = BASE_DIR / "site_audit" / "report" / "logo_nav.png"
    if not logo.exists():
        return JSONResponse({"error": "logo not found"}, status_code=404)
    return FileResponse(logo, media_type="image/png",
                        headers={"Cache-Control": "public, max-age=86400"})


@app.get("/api/audit/priority-config")
async def get_priority_config():
    """检查项优先级配置：全量目录 + 当前覆盖。"""
    from site_audit.core import priority_config
    overrides = priority_config.load_overrides()
    items = priority_config.item_catalog()
    for it in items:
        it["priority"] = overrides.get(it["id"], it["default_priority"])
    return JSONResponse({"ok": True, "items": items, "overrides": overrides})


@app.post("/api/audit/priority-config")
async def save_priority_config(body: PriorityConfigSave):
    """保存优先级覆盖并立即重算/重排全部历史报告。"""
    from site_audit.core import priority_config
    valid_ids = {it["id"] for it in priority_config.item_catalog()}
    clean = {k: v for k, v in (body.overrides or {}).items()
             if k in valid_ids and v in ("P0", "P1", "P2")}
    saved = priority_config.save_overrides(clean)
    updated = await asyncio.to_thread(_apply_priority_config_to_reports)
    return JSONResponse({"ok": True, "saved": saved, "reports_updated": updated})


def _shorten_brand_name(full_name: str) -> str:
    """从公司全称中提取品牌简称"""
    import re
    name = full_name.strip()
    # 去掉括号及注释
    name = re.sub(r"[（(].*?[)）]", "", name)
    # 去掉地域前缀（长前缀优先）
    for prefix in sorted(["中国", "全国", "北京市", "上海市", "深圳市", "深圳市", "广州", "广州市", "杭州", "东莞市", "东莞", "苏州", "南京市", "南京", "成都", "成都市", "武汉", "西安市", "西安", "佛山", "中山市", "中山", "珠海", "惠州", "汕头", "广东", "广东省", "江苏", "江苏省", "浙江", "浙江省", "福建", "福建省", "山东", "山东省", "四川", "湖南省", "湖南", "湖北省", "湖北", "安徽", "河南省", "河南", "河北", "辽宁省", "辽宁", "陕西", "陕西省", "重庆", "天津", "深圳"], key=len, reverse=True):
        if name.startswith(prefix) and len(name) > len(prefix) + 1:
            name = name[len(prefix):]
            break
    # 去掉公司后缀（长后缀优先）
    for suffix in sorted(["集团有限公司", "科技有限公司", "技术服务有限公司", "信息技术有限公司", "网络科技有限公司", "互联网服务有限公司", "企业管理有限公司", "营销技术有限公司", "企业管理咨询有限公司", "企服集团有限公司", "律师事务所", "有限公司", "有限责任公司", "股份有限公司", "合伙企业", "集团"], key=len, reverse=True):
        if name.endswith(suffix) and len(name) > len(suffix) + 1:
            name = name[:-len(suffix)]
            break
    name = name.strip()
    # 如果剩余名称仍然很长（>8字符），尝试取核心词
    if len(name) > 8:
        # 尝试取前4个字作为简称
        name = name[:4]
    return name if name else full_name


@app.get("/api/audit/reports")
async def list_audit_reports():
    """列出所有历史诊断报告。"""
    audit_dir = OUTPUT_DIR / "audit"
    audit_dir.mkdir(parents=True, exist_ok=True)
    reports = []
    for f in audit_dir.glob("*.html"):
        stem = f.stem
        # 跳过 regen 派生报告（开发期 UI 迭代产物，底层诊断数据与原始报告完全相同）
        if "_regen_" in stem:
            continue
        json_file = audit_dir / f"{stem}.json"
        info = {"html_file": f.name, "filename": stem}
        if json_file.exists():
            try:
                data = json.loads(json_file.read_text(encoding="utf-8"))
                info["url"] = data.get("url", "")
                info["score"] = data.get("total_score", 0)
                info["grade"] = data.get("grade", "")
                info["timestamp"] = data.get("timestamp", "")
                summary = data.get("summary", {})
                info["pass"] = summary.get("pass", 0)
                info["warn"] = summary.get("warn", 0)
                info["fail"] = summary.get("fail", 0)
                info["skip"] = summary.get("skip", 0)
                info["total"] = summary.get("total", 0)
                # LLM token 用量与费用（旧报告无此字段则不展示）
                tu = data.get("token_usage") or {}
                if tu.get("total_tokens"):
                    info["tokens"] = tu.get("total_tokens", 0)
                    info["token_calls"] = tu.get("calls", 0)
                    # 永远按当前官方峰谷价现场计算（存档 cost_cny 仅留档），
                    # 价格修正后历史报告显示自动跟随对账
                    info["cost"] = estimate_cost_cny(
                        tu, ts_hint=data.get("timestamp", ""), search_price_per_call=_search_price())
                # 提取品牌名：优先 auth-05 message（品牌[XXX]），其次 tech-14
                import re
                brand_name = ""
                for r in data.get("results", []):
                    rid = r.get("id", "")
                    if rid == "auth-05" and r.get("message"):
                        m = re.search(r"品牌\[(.+?)\]", r["message"])
                        if m:
                            full_name = m.group(1).strip()
                            brand_name = _shorten_brand_name(full_name)
                            break
                    if rid == "tech-14" and r.get("detail"):
                        detail = r["detail"]
                        if "识别为:" in detail:
                            m = re.search(r"识别为:\s*([^(]+)", detail)
                            if m:
                                brand_name = m.group(1).strip()
                                break
                        elif "识别为SaaS" in detail:
                            m = re.search(r"平台:\s*([^(]+)", detail)
                            if m:
                                brand_name = m.group(1).strip()
                                break
                info["brand_name"] = brand_name
                # 企业绑定状态：已绑定主体 / 待人工（候选列表），供列表「企业绑定」入口
                info["company"] = data.get("company_full_name") or ""
                if data.get("company_candidates"):
                    info["company_candidates"] = data["company_candidates"]
            except Exception:
                pass
        # 如果报告数据没提取到品牌名，从域名提取简称
        if not info.get("brand_name") and info.get("url"):
            import re
            domain = info["url"]
            domain = re.sub(r"^https?://(www\.)?", "", domain).split("/")[0]
            domain = domain.replace("www.", "")
            parts = domain.split(".")
            if len(parts) >= 2:
                info["brand_name"] = parts[-2] if parts[-1] in ("com","cn","net","org","io","co") else parts[0]
            elif parts:
                info["brand_name"] = parts[0]
        if not info.get("url"):
            parts = stem.rsplit("_", 2)
            if len(parts) >= 3:
                info["url"] = parts[0]
                ts = f"{parts[1]}_{parts[2]}"
                info["timestamp"] = f"{ts[:4]}-{ts[4:6]}-{ts[6:8]} {ts[9:11]}:{ts[11:13]}:{ts[13:15]}" if len(ts) >= 15 else ""
        mtime = f.stat().st_mtime
        import datetime
        info["mtime"] = datetime.datetime.fromtimestamp(mtime).strftime("%Y-%m-%d %H:%M")
        reports.append(info)
    # 按诊断时间排序（不是文件修改时间），最新的在前
    reports.sort(key=lambda x: x.get("timestamp", ""), reverse=True)
    return JSONResponse(reports)


@app.get("/api/provenance/list")
async def list_provenance_reports():
    """列出所有包含溯源信息的报告。"""
    out_dir = OUTPUT_DIR
    reports = []
    for json_file in out_dir.glob("*.json"):
        try:
            with open(json_file, "r", encoding="utf-8") as f:
                result = json.load(f)
            mr_brief = result.get("mr_brief") or {}
            pipeline_meta = mr_brief.get("_pipeline_meta") or {}
            provenance = pipeline_meta.get("provenance") or {}
            if provenance:
                stem = json_file.stem
                reports.append({
                    "report": stem,
                    "brand": result.get("brand", ""),
                    "timestamp": result.get("timestamp", ""),
                    "total_fields": len(provenance),
                    "by_source": (pipeline_meta.get("provenance_summary") or {}).get("by_source", {}),
                    "html_file": f"{stem}.html",
                })
        except Exception:
            continue
    reports.sort(key=lambda x: x.get("timestamp", ""), reverse=True)
    return JSONResponse(reports)


@app.get("/api/provenance/{report_stem:path}")
async def get_provenance(report_stem: str):
    """溯源审计接口：查询报告中各字段的来源和获取方法。

    从已保存的 JSON 报告中读取 _pipeline_meta.provenance 信息，
    返回字段级别的来源分布和详细溯源信息。

    Args:
        report_stem: 报告文件名（不含扩展名），如 "市场调研_苏奥传感_20260913_192330"

    Returns:
        {
            "report": 报告名,
            "brand": 品牌名,
            "timestamp": 生成时间,
            "summary": {
                "total_fields": 总字段数,
                "by_source": {来源: 数量},
                "by_method": {方法: 数量},
                "low_confidence_fields": [低置信度字段列表],
                "unknown_source_fields": [来源未知字段列表],
            },
            "fields": {
                "字段名": {
                    "value": 字段值,
                    "source": 来源,
                    "method": 获取方法,
                    "confidence": 置信度,
                    "fetched_at": 获取时间,
                    "detail": {方法细节},
                }
            }
        }
    """
    import os
    out_dir = OUTPUT_DIR
    json_path = out_dir / f"{report_stem}.json"

    if not json_path.exists():
        # 尝试在 output 子目录中查找
        for f in out_dir.glob(f"**/{report_stem}.json"):
            json_path = f
            break

    if not json_path.exists():
        return JSONResponse({"error": "报告不存在", "report_stem": report_stem}, status_code=404)

    try:
        with open(json_path, "r", encoding="utf-8") as f:
            result = json.load(f)
    except Exception as e:
        return JSONResponse({"error": f"读取报告失败: {str(e)}"}, status_code=500)

    # 从 mr_brief._pipeline_meta 中提取溯源信息
    mr_brief = result.get("mr_brief") or {}
    pipeline_meta = mr_brief.get("_pipeline_meta") or {}
    provenance = pipeline_meta.get("provenance") or {}
    provenance_summary = pipeline_meta.get("provenance_summary") or {}

    # 获取字段值（从 mr_brief.info 中）
    info = mr_brief.get("info") or {}

    # 构建字段级别的详细溯源信息
    fields_detail = {}
    for field_name, prov_info in provenance.items():
        if not isinstance(prov_info, dict):
            continue
        fields_detail[field_name] = {
            "value": info.get(field_name, ""),
            "source": prov_info.get("source", "unknown"),
            "method": prov_info.get("method", "unknown"),
            "confidence": prov_info.get("confidence", 0),
            "fetched_at": prov_info.get("fetched_at", ""),
            "detail": prov_info.get("detail", {}),
        }

    # 如果没有预计算的 summary，现场计算
    if not provenance_summary:
        by_source = {}
        by_method = {}
        low_conf = []
        unknown_src = []
        for field_name, prov_info in provenance.items():
            if not isinstance(prov_info, dict):
                continue
            src = prov_info.get("source", "unknown")
            mth = prov_info.get("method", "unknown")
            conf = prov_info.get("confidence", 0)
            by_source[src] = by_source.get(src, 0) + 1
            by_method[mth] = by_method.get(mth, 0) + 1
            if conf < 0.5:
                low_conf.append(field_name)
            if src == "unknown":
                unknown_src.append(field_name)
        provenance_summary = {
            "total_fields": len(provenance),
            "by_source": by_source,
            "by_method": by_method,
            "low_confidence_fields": low_conf,
            "unknown_source_fields": unknown_src,
        }

    return JSONResponse({
        "report": report_stem,
        "brand": result.get("brand", ""),
        "timestamp": result.get("timestamp", ""),
        "method_id": result.get("method_id", ""),
        "summary": provenance_summary,
        "fields": fields_detail,
        "pipeline_meta": {
            "fetcher_status": pipeline_meta.get("fetcher_status", "unknown"),
            "fulltext_evidence": pipeline_meta.get("fulltext_evidence", 0),
            "evidence_count": pipeline_meta.get("evidence_count", 0),
        },
    })


_MR_DECK_SPEC = [
    # 八页各自的必备块型（顺序对位 slides）；None=块存在即可，函数=内容达标条件
    (["two_col", "note"], {"two_col": lambda b: len(b.get("cards") or []) >= 2}),
    (["two_col", "timeline", "note"], {}),
    (["bars", "note"], {"bars": lambda b: len(b.get("items") or []) >= 2}),
    (["words", "note"], {"words": lambda b: len(b.get("cols") or []) >= 2}),
    (["table", "note"], {}),
    (["media", "note"], {"media": lambda b: len(b.get("groups") or []) >= 2}),
    (["table", "note"], {}),
    (["kws", "note"], {"kws": lambda b: len(b.get("cols") or []) >= 3}),
]
_MR_DECK_PAGE_SRC = ["第一章画像", "第一章画像", "第一章画像", "第二三章现状",
                     "第四五章竞品", "第三/七章渠道预算", "第四五章竞品", "第六七章方案"]


def _deck_missing_report(deck) -> list[dict]:
    """机检简报八页：返回缺块清单 [{"i","k","missing":[...]}]。deck 可为 str/dict。"""
    if isinstance(deck, str):
        try:
            deck = json.loads(deck)
        except (ValueError, TypeError):
            return []
    slides = (deck or {}).get("slides") if isinstance(deck, dict) else None
    out = []
    for i, (req, conds) in enumerate(_MR_DECK_SPEC):
        if not slides or i >= len(slides) or not isinstance(slides[i], dict):
            continue
        blocks = [b for b in (slides[i].get("blocks") or []) if isinstance(b, dict)]
        types = [str(b.get("type") or "") for b in blocks]
        miss = [t for t in req if t not in types]
        for t, ok in conds.items():
            b = next((b for b in blocks if str(b.get("type")) == t), None)
            if b is not None and not ok(b):
                miss.append(f"{t}内容不全")
        if "src" not in types:
            miss.append("src")
        if miss:
            out.append({"i": i, "k": slides[i].get("k", ""), "missing": miss})
    return out


def _search_price() -> float:
    try:
        return float(load_config().get("llm", {}).get("search_price_per_call", 0) or 0)
    except Exception:
        return 0.0


async def _proc_step(plog, step, label, fn):
    """执行收尾环节一步并记录单步耗时（生成过程面板用）；fn 可为同步或协程。"""
    t0 = time.time()
    out = fn()
    if asyncio.iscoroutine(out):
        out = await out
    plog.append({"step": step, "message": f"完成: {label}", "dur": round(time.time() - t0, 1)})
    return out


def _merge_usage(result: dict, llm):
    """守卫链/管线独立客户端的 token 用量回传主统计（真实成本口径）。"""
    u = getattr(llm, "token_usage", None)
    if not u:
        return
    tu = result.setdefault("token_usage", {"prompt_tokens": 0, "completion_tokens": 0,
                                           "total_tokens": 0, "prompt_cache_hit_tokens": 0,
                                           "prompt_cache_miss_tokens": 0, "calls": 0,
                                           "search_calls": 0, "failed_attempts": 0})
    for k, v in u.items():
        tu[k] = (tu.get(k) or 0) + (v or 0)


async def ensure_mr_brief_valid(result: dict, config: dict, on_progress=None):
    """画像字段合并：三层数据管线（在 agent.run 之前执行）的字段覆盖画像 info，
    并写缓存（正常路径也入库）。管线本体见 profile_pipeline.build_profile。"""
    fields = result.pop("_profile_fields", None)
    if not isinstance(fields, dict):
        return
    b = result.get("mr_brief")
    if not isinstance(b, dict):
        b = result["mr_brief"] = {}
    # 保存管线元数据（抓取器状态、全文证据数、溯源信息等），供报告页显示标识
    b["_pipeline_meta"] = {
        "fetcher_status": fields.get("_fetcher_status", "unknown"),
        "fulltext_evidence": fields.get("_fulltext_evidence", 0),
        "evidence_count": fields.get("_evidence_count", 0),
        "provenance": fields.get("_provenance") or {},
        "provenance_summary": fields.get("_provenance_summary") or {},
    }
    info = b.setdefault("info", {})
    from profile_pipeline import _field_ok
    def _val(item):
        return item.get("value") if isinstance(item, dict) else item
    for k, item in fields.items():
        if k.startswith("_"):
            continue
        v = _val(item)
        if _field_ok(k, v) and not info.get(k):
            info[k] = v
    seedn = {}
    if isinstance(result.get("mr_org"), dict):
        seedn = next((n for n in result["mr_org"].get("nodes", [])
                      if n.get("id") == result["mr_org"].get("seed_key")), {})
        for f in ("legal_person", "founder", "credit_code"):
            _fi = fields.get(f)
            v = _fi.get("value") if isinstance(_fi, dict) else _fi
            if _field_ok(f, v):
                seedn[f] = v
                info.setdefault(f, v)
    from company_cache import upsert_info as _up
    _up(result.get("brand") or "", {**info, "legal_person": seedn.get("legal_person"),
                                    "founder": seedn.get("founder")},
        credit_code=seedn.get("credit_code") or info.get("credit_code"),
        origin="客户市场调研")
    logger.info("画像字段已合并入库（管线证据池口径）")


async def ensure_org_graph(result: dict, config: dict):
    """市场调研落盘前：组织架构实名图（工商API：详情+股东+分支机构，0次LLM）。
    获取失败抛 OrgGraphError 终止任务——生成前已有企业名称API校验与画像层校验
    两层前置，仍失败属真异常，不降级不静默。"""
    if result.get("method_id") != "customer_market_research" or result.get("mr_org"):
        return
    brand = result.get("brand") or ""
    brief_info = ((result.get("mr_brief") or {}).get("info") or {}) if isinstance(result.get("mr_brief"), dict) else {}
    company = brief_info.get("full_name") or brand
    if not company:
        return
    from org_graph import build_org_graph_from_api, OrgGraphError
    try:
        from business_api_client import get_business_api_client
        api = get_business_api_client()
        detail = await api.get_company_detail_by_name(company)
        if not detail or not detail.get("id"):
            raise OrgGraphError(
                f"工商API未查到「{company}」的企业详情，组织架构无法生成。"
                "请确认企业名称是否准确后重试。")
        sh, br = await asyncio.gather(
            api.get_shareholders(detail["id"], page_size=50),
            api.get_branch_orgs(detail["id"], page_size=50))
        org = build_org_graph_from_api(detail, sh, br)
    except OrgGraphError:
        raise
    except Exception as e:
        raise OrgGraphError(f"组织架构工商API调用失败：{e}。请稍后重试或检查API配置。") from e
    if not (org and org.get("tree")):
        raise OrgGraphError(
            f"「{company}」在工商API中无股东与分支机构数据，组织架构无法生成。")
    result["mr_org"] = org
    _seedn = next((n for n in org["nodes"] if n["id"] == org["seed_key"]), {})
    logger.info(f"组织架构实名图(API)完成: {len(org['nodes'])}节点/{len(org['tree'])}边, "
                f"法人={_seedn.get('legal_person')}")


async def ensure_deck_complete(result: dict, config: dict):
    """出报告前的兜底：机检简报缺块 → 查调研正文有据则调 LLM 补齐 → 复检不过则放弃。
    只处理客户市场调研结果；无缺块零开销。"""
    if result.get("method_id") != "customer_market_research" or not result.get("mr_deck"):
        return
    deck = result["mr_deck"]
    if isinstance(deck, str):
        try:
            deck = json.loads(deck)
        except (ValueError, TypeError):
            return
    missing = _deck_missing_report(deck)
    if not missing:
        return
    logger.info(f"简报完整性机检发现缺失: {missing}")
    llm_cfg = config["llm"]
    llm = LLMClient(base_url=llm_cfg["base_url"], api_key=llm_cfg["api_key"],
                    model=llm_cfg["model"], temperature=llm_cfg.get("temperature", 0.7),
                    max_tokens=llm_cfg.get("max_tokens", 16384), timeout=llm_cfg.get("timeout", 120),
                    max_retries=llm_cfg.get("max_retries", 3),
                    enable_search=llm_cfg.get("enable_search", False),
                    enable_thinking=llm_cfg.get("enable_thinking", False))
    agent = Agent(llm, max_concurrency=1)
    ctx = {k: result.get(k) for k in ("brand", "mr_profile", "mr_marketing",
                                      "mr_opportunity", "mr_plan", "mr_proposal", "mr_sales")}
    prompt = agent._render(
        "你是销售情报分析员。简报八页已生成，但机检发现以下页缺块：\n{{missing}}\n\n"
        "当前对应页现状(JSON)：\n{{cur}}\n\n"
        "调研正文（唯一数据来源，禁止编造；无据内容在文本里写「待补：<怎么补>」）：\n"
        "【第一章 画像】{{mr_profile}}\n【第二三章 痛点与营销现状】{{mr_marketing}}\n"
        "【第四五章 竞品与机会】{{mr_opportunity}}\n【第六七章 方案与预算】{{mr_plan}}\n"
        "【第八章与附录】{{mr_proposal}}\n【销售话术】{{mr_sales}}\n\n"
        "## 任务\n对每个缺块页，输出该页**完整**的 blocks 数组（原块原样保留，只补缺的块；"
        "补 two_col 缺卡时把缺的卡补成第二张卡，数据从调研正文对应章节搬，禁止编造数字）。\n"
        "块型协议：two_col/timeline/words/table/bars/media/kws/note/src。\n\n"
        "## 输出格式（仅输出JSON）\n"
        '[{"i":0,"blocks":[{"type":"...","...":"..."}]}]', 
        {"missing": json.dumps(missing, ensure_ascii=False),
         "cur": json.dumps([{"i": m["i"], "k": m["k"],
                             "blocks": (deck.get("slides") or [])[m["i"]].get("blocks")}
                            for m in missing], ensure_ascii=False)})
    try:
        patch = await agent._llm_call(prompt, expect_json=True)
        assert isinstance(patch, list)
    except Exception as e:
        logger.warning(f"简报补齐调用失败，保留原简报: {e}")
        _merge_usage(result, llm)  # 失败前已发生的调用也要入主统计
        return
    _backup = json.dumps(deck.get("slides"), ensure_ascii=False)
    for p in patch:
        i, blocks = p.get("i"), p.get("blocks")
        if isinstance(i, int) and isinstance(blocks, list) and 0 <= i < len(deck["slides"]) and blocks:
            deck["slides"][i]["blocks"] = blocks
    left = _deck_missing_report(deck)
    if left:  # 复检不过：先重试一次，仍不过才回滚（避免越补越缺）
        try:
            patch2 = await agent._llm_call(prompt, expect_json=True)
            for p in (patch2 if isinstance(patch2, list) else []):
                i, blocks = p.get("i"), p.get("blocks")
                if isinstance(i, int) and isinstance(blocks, list) and 0 <= i < len(deck["slides"]) and blocks:
                    deck["slides"][i]["blocks"] = blocks
            left = _deck_missing_report(deck)
        except Exception:
            pass
        if left:
            deck["slides"] = json.loads(_backup)
            logger.warning(f"简报补齐两轮仍不过，已回滚原样: {left}")
            result["mr_deck"] = deck
            _merge_usage(result, llm)
            return
    result["mr_deck"] = deck
    _merge_usage(result, llm)
    logger.info("简报补齐完成，复检通过")


@app.delete("/api/audit/reports/{filename}")
async def delete_audit_report(filename: str):
    audit_dir = OUTPUT_DIR / "audit"
    deleted = []
    # 删除 HTML
    html_path = audit_dir / filename
    if html_path.exists() and html_path.suffix == ".html":
        html_path.unlink()
        deleted.append(html_path.name)
    # 删除同名 JSON
    stem = Path(filename).stem
    json_path = audit_dir / f"{stem}.json"
    if json_path.exists():
        json_path.unlink()
        deleted.append(json_path.name)
    if not deleted:
        return JSONResponse({"ok": False, "error": "文件不存在"}, status_code=404)
    from company_cache import delete_audit_snapshot
    delete_audit_snapshot(stem)  # 同步删对应检测快照行，防孤儿记录
    _log_op("官网诊断", "删除诊断报告", Path(filename).stem, "、".join(deleted))
    return JSONResponse({"ok": True, "deleted": deleted})


@app.delete("/api/audit/snapshots/{sid:path}")
async def delete_audit_snapshot_api(sid: str):
    """删除企业资料页的单条检测记录（只删库内结论行，不动报告文件）。"""
    from company_cache import delete_audit_snapshot
    if not delete_audit_snapshot(sid):
        return JSONResponse({"ok": False, "error": "记录不存在"}, status_code=404)
    _log_op("企业资料", "删除检测记录", sid, "")
    return {"ok": True}


async def _llm_resolve_company_name(name: str) -> str:
    """LLM 联网解析企业工商注册全称（处理站上写法与注册名语序/括号不一致的情况）。
    返回空串表示查不到。仅解析名字，不作为绑定依据——调用方仍须工商精准核验。"""
    _lc = load_config().get("llm", {})
    llm = LLMClient(base_url=_lc["base_url"], api_key=_lc["api_key"], model=_lc["model"],
                    temperature=_lc.get("temperature", 0.7), max_tokens=_lc.get("max_tokens", 16384),
                    timeout=_lc.get("timeout", 120), max_retries=_lc.get("max_retries", 3),
                    enable_search=True, enable_thinking=False, search_engine=["baidu"])
    prompt = (f"「{name}」疑似某企业的工商注册名称变体（地区词位置或括号写法与注册名不同）。\n"
              "请联网查询该企业的准确工商注册全称（统一社会信用代码可作旁证）。\n"
              '只返回 JSON：{"full_name": "注册全称"} 或 {"full_name": ""}（联网确认不了时）。不要返回其他内容。')
    try:
        r = await llm.chat([{"role": "user", "content": prompt}], expect_json=True)
        return str(r.get("full_name") or "").strip() if isinstance(r, dict) else ""
    except Exception:
        return ""


def _brand_overlaps_full(brand: str, full_name: str) -> bool:
    """品牌与全称的字面重合判定：品牌整体含于全称，或与全称有≥3字连续共同段。
    「和创企赢」⊂「广东和创企赢企服集团有限公司」→ True；「国外公司注册」只有
    「公司」2字重合 → False（服务词冒充品牌在此被天然排后）。"""
    from company_cache import norm_name
    b, f = norm_name(brand), norm_name(full_name)
    if not b or b == f:
        return False
    if b in f:
        return True
    return any(b[i:i + 3] in f for i in range(len(b) - 2))


async def _llm_pick_brand(full_name: str, candidates: list) -> str:
    """模型语义裁决：候选里哪个是真正的品牌名，排除服务/业务描述词（如"国外公司注册"）。
    纯语义判断不联网，小调用秒级。返回候选中的胜者；判无真品牌或异常返回空串。"""
    if not candidates:
        return ""
    _lc = load_config().get("llm", {})
    llm = LLMClient(base_url=_lc["base_url"], api_key=_lc["api_key"], model=_lc["model"],
                    temperature=0, max_tokens=200, timeout=30,
                    max_retries=1, enable_search=False, enable_thinking=False)
    prompt = (f"公司工商注册全称：「{full_name}」。\n"
              f"候选名称：{json.dumps(candidates, ensure_ascii=False)}\n"
              "判断哪个候选是这家公司对外的品牌名称（商标/官网/产品上的叫法）。\n"
              "排除标准：像服务项目、业务动作的词（如\"国外公司注册\"\"工商财税\"）不是品牌名。\n"
              "多个候选都像品牌名时，选更完整、更正式的那个（通常是商标或官网全称，而非简称）。\n"
              '只返回 JSON：{"brand": "选中的候选原文"}；候选里没有真品牌名时返回 {"brand": ""}。')
    try:
        r = await llm.chat([{"role": "user", "content": prompt}], expect_json=True)
        b = str(r.get("brand") or "").strip() if isinstance(r, dict) else ""
        return b if b in candidates else ""
    except Exception:
        return ""


async def _web_verify_brand(full_name: str, hints: list) -> str:
    """七渠道联网多方核查品牌名（公众号/官方账号/官网域名/活动新闻/百科/媒体报道/商标）。
    档案候选只作线索；排除服务词与全称机械截断；核查到更准的官方品牌名以联网为准。"""
    _lc = load_config().get("llm", {})
    llm = LLMClient(base_url=_lc["base_url"], api_key=_lc["api_key"], model=_lc["model"],
                    temperature=0.2, max_tokens=_lc.get("max_tokens", 16384), timeout=60,
                    max_retries=1, enable_search=True, enable_thinking=False, search_engine=["baidu"])
    hint_line = (f"档案中已有的品牌候选（历史写入，未必正确，仅供线索）：{json.dumps(hints, ensure_ascii=False)}。\n"
                 if hints else "")
    prompt = (f"「{full_name}」是一家公司的工商注册全称。请联网多方核查这家公司对外使用的品牌名称"
              "（商标/官网/产品上的叫法，不是公司注册名本身）。应交叉核对以下证据渠道：\n"
              "① 微信公众号名称；② 今日头条/官方自媒体账号名；③ 官网域名及官网署名；"
              "④ 线下活动、新闻稿中的品牌描述；⑤ 百科词条（百度百科/维基等）；"
              "⑥ 第三方媒体报道中的称呼；⑦ 商标注册信息。\n"
              + hint_line +
              "排除：服务/业务描述词（如\"国外公司注册\"）；公司全称的机械截断"
              "（如「丝路视觉科技」之于「丝路视觉科技股份有限公司」也不算品牌名）。\n"
              "多个渠道指向同一叫法才可采信；若核查发现比候选更准确的官方品牌名，以其为准。\n"
              "语言一致：候选（或用户已确认）的品牌名与证据相符时，沿用其原文原文语言，"
              "不要替换成外文/其他语言版本；仅当候选全部被排除时才返回新名。\n"
              '只返回 JSON：{"brand": "品牌名", "evidence": "依据了哪些渠道、各渠道叫法"}；'
              '核查不到返回 {"brand": ""}。不要返回其他内容。')
    try:
        r = await llm.chat([{"role": "user", "content": prompt}], expect_json=True)
        b = str(r.get("brand") or "").strip() if isinstance(r, dict) else ""
        logger.info(f"品牌核查（七渠道联网）{full_name}: brand={b!r} evidence={str(r.get('evidence') or '')[:200]}")
        return b
    except Exception as e:
        logger.warning(f"品牌联网核查失败 {full_name}: {e}")
        return ""


async def _lookup_brand_name(full_name: str) -> str:
    """查公司对外品牌名：只有七渠道联网核查过的主品牌才直接采用；
    人工确认、离线裁决、历史存量一律重新走七渠道联网核查（人工确认作优先线索）。
    核查不到退离线裁决/档案首位兜底（不写可信来源，下次再核）。"""
    from company_cache import get_info
    info, _ = get_info(full_name)
    primary = str((info or {}).get("brand_primary") or "").strip()
    src = str((info or {}).get("brand_source") or "").strip()
    brands = [str(b).strip() for b in ((info or {}).get("brands") or []) if str(b).strip()]
    # 来源定信任级（2026-09-19 用户定版）：只有七渠道联网核查过的才免检；
    # 人工确认的品牌名也不被信任——作为优先线索进入联网核查，联网结论说了算
    if primary and src == "web_multichannel":
        return primary
    b = await _web_verify_brand(full_name, brands)
    if b and b != full_name:
        from company_cache import prefer_brand
        prefer_brand(full_name, b, origin="名称核查·多方核查", source="web_multichannel")
        return b
    # 联网失败退离线裁决兜底（不写来源 → 下次识别会再触发联网补核）
    if primary:
        return primary
    if brands:
        picked = await _llm_pick_brand(full_name, brands)
        return picked or brands[0]
    return ""


@app.post("/api/audit/bind")
async def audit_bind_company(req: AuditBindRequest):
    """人工绑定诊断报告的企业主体（多候选/英文站场景的纠错入口）。
    中文名仍走工商精准核验，核验通过才落绑定与档案；查无此名称返回明确报错，绝不落错。"""
    company = (req.company or "").strip()
    if not company or not req.filename or "/" in req.filename or ".." in req.filename:
        return JSONResponse({"ok": False, "error": "参数无效"}, status_code=400)
    # 域名里带点，不能用 Path.stem（会把 www.xx.com 在 .com 处截断）；只剥已知扩展名
    name = req.filename
    for ext in (".json", ".html"):
        if name.endswith(ext):
            name = name[: -len(ext)]
    json_path = OUTPUT_DIR / "audit" / f"{name}.json"
    if not json_path.exists():
        return JSONResponse({"ok": False, "error": "报告不存在"}, status_code=404)
    from audit_company_bind import verify_cn, _name_key
    from business_api_client import get_business_api_client, BusinessAPIClient
    api = get_business_api_client()
    verified, api_err = await verify_cn([company], api)
    if not verified and api_err:
        return JSONResponse({"ok": False, "error": f"工商接口异常：{api_err}"}, status_code=502)
    if not verified:
        # 三级兜底：LLM 联网解析注册全称，须与原写法归一后（剥行政区划、保字序）一致，再过工商精准核验
        resolved = await _llm_resolve_company_name(company)
        if resolved and _name_key(resolved) == _name_key(company):
            verified, api_err = await verify_cn([resolved], api)
            if not verified and api_err:
                return JSONResponse({"ok": False, "error": f"工商接口异常：{api_err}"}, status_code=502)
    if not verified:
        return JSONResponse({"ok": False, "error": f"工商库查无「{company}」，请核对全称（含地区前缀）"}, status_code=422)
    full = verified[0]["name"]
    d = json.loads(json_path.read_text(encoding="utf-8"))
    d["company_full_name"] = full
    d["company_bind_via"] = "manual"
    json_path.write_text(json.dumps(d, ensure_ascii=False), encoding="utf-8")
    from company_cache import upsert_info
    fields = BusinessAPIClient.extract_raw_fields(verified[0]["detail"])
    fields.update(BusinessAPIClient.extract_layer1_fields(verified[0]["detail"]))
    if fields:
        upsert_info(full, fields, credit_code=fields.get("credit_code"), origin="官网诊断·人工绑定")
    from audit_company_bind import merge_brand_aliases, _merge_domain
    merge_brand_aliases(full, str(d.get("site_brand") or ""))  # 品牌名/别名增量并入档案
    _merge_domain(full, str(d.get("url") or ""))               # 官网域名并入档案
    _log_op("官网诊断", "人工绑定企业", full, json_path.name)
    return {"ok": True, "company": full}


class AliasUpdateRequest(PydanticModel):
    id: str = Field(..., description="企业档案 id")


@app.post("/api/company/alias-update")
async def company_alias_update(req: AliasUpdateRequest):
    """LLM 联网搜索品牌常见别名（曾用名/简称/英文名等），增量并入档案别名单（去重，不动品牌名）。"""
    from company_cache import get_company_by_id, upsert_info
    row = get_company_by_id(req.id)
    if not row or not row[0]:
        return JSONResponse({"ok": False, "error": "企业档案不存在"}, status_code=404)
    info = row[0]
    full_name = info.get("full_name") or req.id
    seeds = [b for b in (info.get("brands") or []) if b] or [full_name]
    known = [a for a in (info.get("aliases") or []) if a]
    _lc = load_config().get("llm", {})
    llm = LLMClient(base_url=_lc["base_url"], api_key=_lc["api_key"], model=_lc["model"],
                    temperature=_lc.get("temperature", 0.7), max_tokens=_lc.get("max_tokens", 16384),
                    timeout=_lc.get("timeout", 120), max_retries=_lc.get("max_retries", 3),
                    enable_search=True, enable_thinking=False, search_engine=["baidu"])
    prompt = (f"企业：{full_name}；品牌：{'、'.join(seeds)}；已知别名：{'、'.join(known) or '无'}。\n"
              "请联网搜索该品牌的常见叫法，包括：品牌简称、中文/英文互译名、曾用名、大众口头称呼。"
              "只返回 JSON 数组，元素为字符串，只列确认属于该品牌的别名（最多10个），"
              "不要包含已知别名、公司全称、地区词。无法确认时返回 []。")
    try:
        result = await llm.chat([{"role": "user", "content": prompt}], expect_json=True)
    except Exception as e:
        return JSONResponse({"ok": False, "error": f"LLM 调用失败：{type(e).__name__}"}, status_code=502)
    if isinstance(result, dict):
        result = result.get("aliases") or result.get("data") or []
    found = [str(a).strip() for a in (result or []) if str(a).strip()] if isinstance(result, list) else []
    known_l = {k.lower() for k in known} | {s.lower() for s in seeds} | {full_name.lower()}
    added = []
    for a in found:
        if a.lower() not in known_l and a not in added:
            added.append(a)
    if added:
        upsert_info(full_name, {"aliases": known + added}, origin="企业资料·更新别名")
    _log_op("企业资料", "更新别名", full_name, f"新增 {len(added)} 个" + (f"：{'、'.join(added)}" if added else ""))
    return {"ok": True, "added": added, "aliases": known + added}


@app.get("/health")
async def health():
    return {"status": "ok"}


@app.post("/analyze")
async def analyze(req: AnalyzeRequest):
    # 服务端强制核验（2026-09-19）：同任务接口——名称必须经核查登记，锚定取注册全称；
    # 凭证批准即消费（一次性）
    _anchor, _alias, _verr, _vtok = _resolve_verified_brand(req.brand, getattr(req, "brand_alias", None))
    if _verr:
        raise HTTPException(status_code=422, detail=_verr)
    _consume_verified_token(_vtok)
    req.brand = _anchor
    config = load_config()
    agent = build_agent(config)

    _plog = []  # 生成过程事件序列：随报告 JSON 落盘，供列表「生成过程」悬浮展示
    _t_run0 = time.time()  # 全流程起点（含建档管线/LLM节点/收尾），落盘前回填真实总耗时

    def on_progress(step, msg, elapsed):
        logger.info(f"[{elapsed}s] {step}: {msg}")
        _plog.append({"step": step, "message": msg, "elapsed": elapsed})

    from profile_pipeline import build_profile, ProfileError
    from company_cache import known_facts_text
    _lc = config["llm"]
    _pllm = LLMClient(base_url=_lc["base_url"], api_key=_lc["api_key"],
                      model=_lc["model"], temperature=_lc.get("temperature", 0.7),
                      max_tokens=_lc.get("max_tokens", 16384), timeout=_lc.get("timeout", 120),
                      max_retries=_lc.get("max_retries", 3), enable_search=True,
                      enable_thinking=False, search_engine=["baidu"])
    try:
        _pfields = await build_profile(_pllm, req.brand, on_progress=on_progress, brand_alias=req.brand_alias, force_refresh=req.force_refresh)
    except ProfileError as pe:
        raise HTTPException(status_code=422, detail=str(pe))
    result = await agent.run(req.brand, req.product, on_progress=on_progress,
                             method_id=req.method,
                             known_facts=known_facts_text(req.brand))
    result["_process_log"] = _plog
    result["_profile_fields"] = _pfields
    _merge_usage(result, _pllm)
    await _proc_step(_plog, "画像合并", "企业画像字段三层管线合并入库",
                     lambda: ensure_mr_brief_valid(result, config))
    result["profile_evidence"] = _pfields
    from org_graph import OrgGraphError
    try:
        await _proc_step(_plog, "组织图", "组织架构实名图（工商API：详情+股东+分支机构）",
                         lambda: ensure_org_graph(result, config))
    except OrgGraphError as oe:
        raise HTTPException(status_code=422, detail=str(oe)) from oe
    await _proc_step(_plog, "简报机检", "情报简报缺块机检与LLM补齐",
                     lambda: ensure_deck_complete(result, config))
    _rev = {"step": "渲染落盘", "message": "完成: 三页签报告渲染与HTML/JSON保存"}
    _plog.append(_rev)  # 先入列，报告 JSON 落盘时带上本行；耗时在保存后回填到任务日志
    result["total_elapsed"] = round(time.time() - _t_run0, 1)  # 真实全流程耗时（含管线与收尾）
    _rt0 = time.time()
    files = save_output(result, config)
    _rev["dur"] = round(time.time() - _rt0, 1)

    # Return summary (full data is in the saved files)
    return JSONResponse({
        "brand": req.brand,
        "product": req.product,
        "elapsed": result.get("total_elapsed", 0),
        "files": files,
        "product_count": len(result.get("step1_products", [])),
        "persona_count": len(result.get("step2_personas", [])),
        "stage_count": len(result.get("step3_journey", [])),
        "total_queries": sum(
            len(Agent.extract_all_queries(sd))
            for sd in result.get("step4_topics", {}).values()
        ),
        "cluster_count": sum(
            len(sd.get("clusters", []))
            for sd in result.get("step5_clusters", {}).values()
        ),
        "self_check": result.get("self_check", []),
    })


def _annotation_css() -> str:
    return """
    .annot-toolbar { position: fixed; top: 12px; right: 12px; z-index: 999; display: none; gap: 8px; align-items: center; }  /* 标注入口暂时隐藏：display:none，恢复改 flex */
    .annot-toggle { padding: 6px 16px; border-radius: 20px; border: none; background: #fff; color: #6e7991; font-size: 13px; cursor: pointer; box-shadow: 0 2px 8px rgba(0,0,0,0.15); transition: all .2s; }
    .annot-toggle:hover { color: #3567ff; }
    .annot-toggle.active { background: #3567ff; color: #fff; }
    .annot-count { padding: 2px 8px; border-radius: 10px; background: #c0392b; color: #fff; font-size: 11px; font-weight: 700; display: none; }
    .annot-count.show { display: inline; }
    body.annot-mode [data-aid] { cursor: pointer; transition: outline .15s; }
    body.annot-mode [data-aid]:hover { outline: 2px dashed #3567ff; outline-offset: 2px; }
    [data-aid].annotated { position: relative; }
    [data-aid].issue { outline: 2px solid #c0392b; outline-offset: 2px; background: rgba(192,57,43,0.04); }
    [data-aid].ok-annot { outline: 2px solid #155724; outline-offset: 2px; }
    .annot-badge { display: inline-flex; align-items: center; justify-content: center; width: 20px; height: 20px; border-radius: 50%; font-size: 12px; font-weight: 700; margin-right: 8px; vertical-align: middle; }
    [data-aid].issue .annot-badge { background: #c0392b; color: #fff; }
    [data-aid].ok-annot .annot-badge { background: #155724; color: #fff; }
    .annot-comment-box { margin: 8px 0 0; padding: 8px 12px; border-radius: 8px; font-size: 13px; display: none; }
    [data-aid].issue .annot-comment-box { display: block; background: #fff5f5; color: #c0392b; border-left: 3px solid #c0392b; }
    [data-aid].ok-annot .annot-comment-box { display: block; background: #f0fff0; color: #155724; border-left: 3px solid #155724; }
    .annot-dialog-overlay { position: fixed; top: 0; left: 0; width: 100%; height: 100%; background: rgba(0,0,0,0.3); display: flex; align-items: center; justify-content: center; z-index: 1000; }
    .annot-dialog { background: #fff; border-radius: 16px; padding: 24px; width: 440px; max-width: 90vw; box-shadow: 0 8px 32px rgba(0,0,0,0.15); }
    .annot-dialog h3 { font-size: 16px; margin-bottom: 4px; }
    .annot-dialog .annot-target { font-size: 12px; color: #a0a9bd; margin-bottom: 16px; }
    .annot-field { margin-bottom: 14px; }
    .annot-field label { display: block; font-size: 13px; color: #6e7991; margin-bottom: 5px; font-weight: 500; }
    .annot-field select, .annot-field textarea { width: 100%; padding: 8px 12px; border: 1.5px solid #eff1f6; border-radius: 8px; font-size: 14px; outline: none; font-family: inherit; }
    .annot-field select:focus, .annot-field textarea:focus { border-color: #3567ff; }
    .annot-field textarea { resize: vertical; min-height: 80px; }
    .annot-actions { display: flex; gap: 10px; justify-content: flex-end; }
    .annot-btn { padding: 8px 20px; border-radius: 8px; font-size: 14px; font-weight: 600; cursor: pointer; transition: opacity .2s; }
    .annot-btn-save { background: #3567ff; color: #fff; border: none; }
    .annot-btn-save:hover { opacity: .9; }
    .annot-btn-cancel { background: #fff; color: #6e7991; border: 1.5px solid #eff1f6; }
    .annot-btn-remove { background: #fff; color: #c0392b; border: 1.5px solid #c0392b; }
    """


def _annotation_js() -> str:
    return """
    <script>
    (function() {
      const reportStem = decodeURIComponent(window.location.pathname.split('/').pop().replace('.html', ''));
      let annotMode = false;
      let annotations = {};
      let currentAid = null;

      function assignIds() {
        var idx = 0;
        var sels = 'h2, h3, .card, .cluster, .query-item, .stage-block, .meta, p, li, tr, td';
        document.querySelectorAll(sels).forEach(function(el) {
          el.setAttribute('data-aid', 'el_' + idx);
          idx++;
        });
      }

      function applyAnnotations() {
        document.querySelectorAll('[data-aid]').forEach(function(el) {
          const aid = el.getAttribute('data-aid');
          const ann = annotations[aid];
          el.classList.remove('annotated', 'issue', 'ok-annot');
          const oldBadge = el.querySelector('.annot-badge');
          if (oldBadge) oldBadge.remove();
          const oldBox = el.querySelector('.annot-comment-box');
          if (oldBox) oldBox.remove();
          if (ann) {
            el.classList.add('annotated', ann.status === 'issue' ? 'issue' : 'ok-annot');
            const badge = document.createElement('span');
            badge.className = 'annot-badge';
            badge.textContent = ann.status === 'issue' ? '!' : '\\u2713';
            el.insertBefore(badge, el.firstChild);
            if (ann.comment) {
              const box = document.createElement('div');
              box.className = 'annot-comment-box';
              box.textContent = ann.comment;
              el.appendChild(box);
            }
          }
        });
        const count = Object.keys(annotations).filter(function(k){return annotations[k].status === 'issue';}).length;
        const badge = document.getElementById('annotCount');
        if (count > 0) { badge.textContent = count; badge.classList.add('show'); }
        else { badge.classList.remove('show'); }
      }

      async function loadAnnotations() {
        try {
          const resp = await fetch('/api/annotations/' + encodeURIComponent(reportStem));
          const data = await resp.json();
          annotations = data.annotations || {};
          applyAnnotations();
        } catch(e) { console.error('load annotations failed:', e); }
      }

      async function saveToServer() {
        try {
          await fetch('/api/annotations/' + encodeURIComponent(reportStem), {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({annotations: annotations})
          });
        } catch(e) { console.error('save annotations failed:', e); }
      }

      window.toggleAnnotMode = function() {
        annotMode = !annotMode;
        document.body.classList.toggle('annot-mode', annotMode);
        const btn = document.getElementById('annotToggle');
        btn.textContent = annotMode ? '\\u9000\\u51fa\\u6807\\u6ce8' : '\\u6807\\u6ce8\\u6a21\\u5f0f';
        btn.classList.toggle('active', annotMode);
      };

      document.addEventListener('click', function(e) {
        if (!annotMode) return;
        const target = e.target.closest('[data-aid]');
        if (!target) return;
        if (e.target.closest('.annot-dialog-overlay')) return;
        e.preventDefault();
        e.stopPropagation();
        currentAid = target.getAttribute('data-aid');
        openDialog(currentAid, target);
      });

      function openDialog(aid, el) {
        const existing = annotations[aid] || {};
        var title = '';
        if (el.tagName === 'H2' || el.tagName === 'H3') {
          title = el.textContent.trim().slice(0, 30);
        } else if (el.tagName === 'TR') {
          var cells = el.querySelectorAll('td');
          title = cells.length > 0 ? '\\u8868\\u683c\\u884c: ' + cells[0].textContent.trim().slice(0, 20) : '\\u8868\\u683c\\u884c';
        } else if (el.tagName === 'LI') {
          title = '\\u5217\\u8868\\u9879: ' + el.textContent.trim().slice(0, 20);
        } else if (el.tagName === 'P') {
          title = '\\u6bb5\\u843d: ' + el.textContent.trim().slice(0, 20);
        } else if (el.tagName === 'TD') {
          title = '\\u5355\\u5143\\u683c: ' + el.textContent.trim().slice(0, 20);
        } else {
          title = el.textContent.trim().slice(0, 30) || el.tagName.toLowerCase();
        }
        const overlay = document.createElement('div');
        overlay.className = 'annot-dialog-overlay';
        overlay.innerHTML = '<div class="annot-dialog">' +
          '<h3>\\u6807\\u6ce8</h3>' +
          '<div class="annot-target">' + title + '</div>' +
          '<div class="annot-field"><label>\\u72b6\\u6001</label>' +
          '<select id="annotStatus">' +
          '<option value="issue"' + (existing.status === 'issue' ? ' selected' : '') + '>\\u6709\\u95ee\\u9898</option>' +
          '<option value="ok"' + (existing.status === 'ok' ? ' selected' : '') + '>\\u65e0\\u95ee\\u9898</option>' +
          '</select></div>' +
          '<div class="annot-field"><label>\\u8bf4\\u660e</label>' +
          '<textarea id="annotComment" placeholder="\\u8bf4\\u660e\\u95ee\\u9898\\u539f\\u56e0\\u6216\\u5907\\u6ce8...">' + (existing.comment || '') + '</textarea></div>' +
          '<div class="annot-actions">' +
          (existing.status ? '<button class="annot-btn annot-btn-remove" id="annotRemove">\\u5220\\u9664\\u6807\\u6ce8</button>' : '') +
          '<button class="annot-btn annot-btn-cancel" id="annotCancel">\\u53d6\\u6d88</button>' +
          '<button class="annot-btn annot-btn-save" id="annotSave">\\u4fdd\\u5b58</button>' +
          '</div></div>';
        document.body.appendChild(overlay);
        document.getElementById('annotComment').focus();

        document.getElementById('annotSave').onclick = function() {
          const status = document.getElementById('annotStatus').value;
          const comment = document.getElementById('annotComment').value.trim();
          if (!comment && status === 'issue') { alert('\\u8bf7\\u586b\\u5199\\u95ee\\u9898\\u8bf4\\u660e'); return; }
          let sectionTitle = '';
          if (el.tagName === 'H2' || el.tagName === 'H3') {
            sectionTitle = el.textContent.trim();
          } else {
            var walker = el;
            while (walker && !sectionTitle) {
              var sib = walker;
              while (sib) {
                if (sib.tagName === 'H2' || sib.tagName === 'H3') { sectionTitle = sib.textContent.trim(); break; }
                sib = sib.previousElementSibling;
              }
              if (!sectionTitle) walker = walker.parentElement;
            }
            if (!sectionTitle && el.querySelector('strong')) {
              sectionTitle = el.querySelector('strong').textContent.trim();
            }
          }
          let snippet = el.textContent.trim().slice(0, 150);
          annotations[aid] = { status: status, comment: comment, section_title: sectionTitle, content_snippet: snippet, timestamp: new Date().toLocaleString('zh-CN') };
          overlay.remove();
          applyAnnotations();
          saveToServer();
        };
        document.getElementById('annotCancel').onclick = function() { overlay.remove(); };
        if (document.getElementById('annotRemove')) {
          document.getElementById('annotRemove').onclick = function() {
            delete annotations[aid];
            overlay.remove();
            applyAnnotations();
            saveToServer();
          };
        }
        overlay.addEventListener('click', function(e) { if (e.target === overlay) overlay.remove(); });
      }

      assignIds();
      loadAnnotations();
    })();
    </script>
    """


def _annotation_toolbar_html() -> str:
    return (
        '<div class="annot-toolbar">'
        '<span class="annot-count" id="annotCount">0</span>'
        '<button class="annot-toggle" id="annotToggle" onclick="toggleAnnotMode()">标注模式</button>'
        '</div>'
    )


def inject_refresh_button(html: str, brand: str) -> str:
    """在报告页面注入"更新企业信息"按钮，点击后强制刷新工商API数据并重新生成报告。"""
    if "refresh-company-btn" in html:
        return html

    # 注入CSS
    css = """
    <style>
    .refresh-float-btn {
        position: fixed;
        top: 20px;
        right: 20px;
        z-index: 9999;
        padding: 10px 18px;
        background: linear-gradient(135deg, #3567ff, #6b8cff);
        color: #fff;
        border: none;
        border-radius: 10px;
        font-size: 13px;
        font-weight: 600;
        cursor: pointer;
        box-shadow: 0 4px 15px rgba(53, 103, 255, 0.3);
        transition: all 0.2s;
    }
    .refresh-float-btn:hover {
        transform: translateY(-2px);
        box-shadow: 0 6px 20px rgba(53, 103, 255, 0.4);
    }
    .refresh-float-btn:disabled {
        background: #ccc;
        cursor: not-allowed;
        transform: none;
        box-shadow: none;
    }
    .refresh-toast {
        position: fixed;
        top: 70px;
        right: 20px;
        z-index: 10000;
        padding: 12px 20px;
        background: #fff;
        border-radius: 10px;
        box-shadow: 0 4px 20px rgba(0,0,0,0.15);
        font-size: 13px;
        color: #333;
        display: none;
        max-width: 300px;
    }
    .refresh-toast.show {
        display: block;
        animation: slideIn 0.3s ease;
    }
    @keyframes slideIn {
        from { transform: translateX(100%); opacity: 0; }
        to { transform: translateX(0); opacity: 1; }
    }
    </style>
    """

    # 注入按钮和JS
    button_html = f"""
    <button class="refresh-float-btn" id="refreshCompanyBtn" onclick="refreshCompanyInfo()">
        🔄 更新企业信息
    </button>
    <div class="refresh-toast" id="refreshToast"></div>
    <script>
    function refreshCompanyInfo() {{
        _startRefresh();
    }}
    function _startRefresh() {{
        const btn = document.getElementById('refreshCompanyBtn');
        const toast = document.getElementById('refreshToast');
        btn.disabled = true;
        btn.textContent = '更新中...';
        toast.textContent = '正在更新企业档案/组织图/基本信息，完成后自动刷新本页...';
        toast.className = 'refresh-toast show';

        // 轻量刷新：只重跑企业档案/组织图/基本信息合并，回写本报告文件
        fetch('/api/refresh-company', {{
            method: 'POST',
            headers: {{ 'Content-Type': 'application/json' }},
            body: JSON.stringify({{
                brand: {json.dumps(brand, ensure_ascii=False)},
                source_file: decodeURIComponent(location.pathname.split('/').pop() || '')
            }})
        }})
        .then(async r => {{ const j = await r.json(); if (!r.ok) throw new Error(j.detail || '创建任务失败'); return j; }})
        .then(data => {{
            if (data.task_id) {{
                toast.textContent = '刷新任务已创建，正在更新…';
                const poll = async () => {{
                    try {{
                        const t = await (await fetch('/api/tasks/' + data.task_id)).json();
                        if (t.status === 'completed') {{
                            toast.textContent = '✅ 更新完成，重新加载页面…';
                            location.reload();
                        }} else if (t.status === 'failed') {{
                            toast.textContent = '❌ 刷新失败：' + (t.error || '未知错误');
                            btn.disabled = false;
                            btn.textContent = '🔄 更新企业信息';
                            setTimeout(() => {{ toast.className = 'refresh-toast'; }}, 6000);
                        }} else {{
                            toast.textContent = '更新中：' + (t.current_step || '') + ' ' + (t.current_message || '');
                            setTimeout(poll, 2000);
                        }}
                    }} catch (e) {{ setTimeout(poll, 4000); }}
                }};
                poll();
            }} else {{
                throw new Error('创建任务失败');
            }}
        }})
        .catch(err => {{
            toast.textContent = '创建任务失败: ' + err.message;
            btn.disabled = false;
            btn.textContent = '🔄 更新企业信息';
            setTimeout(() => {{ toast.className = 'refresh-toast'; }}, 3000);
        }});
    }}
    </script>
    """

    # 注入CSS
    if "</head>" in html:
        html = html.replace("</head>", f"{css}</head>")
    else:
        html = css + html

    # 如果页面中已经有内嵌的按钮元素（企业画像页面的"一图看懂"行），只注入JS逻辑
    if 'id="refreshCompanyBtn"' in html:
        js_only = f"""
        <div class="refresh-toast" id="refreshToast"></div>
        <script>
        function refreshCompanyInfo() {{
            _startRefresh();
        }}
        function _startRefresh() {{
            const btn = document.getElementById('refreshCompanyBtn');
            const toast = document.getElementById('refreshToast');
            btn.disabled = true;
            btn.textContent = '更新中...';
            toast.textContent = '正在更新企业档案/组织图/基本信息，完成后自动刷新本页...';
            toast.className = 'refresh-toast show';

            fetch('/api/refresh-company', {{
                method: 'POST',
                headers: {{ 'Content-Type': 'application/json' }},
                body: JSON.stringify({{
                    brand: {json.dumps(brand, ensure_ascii=False)},
                    source_file: decodeURIComponent(location.pathname.split('/').pop() || '')
                }})
            }})
            .then(async r => {{ const j = await r.json(); if (!r.ok) throw new Error(j.detail || '创建任务失败'); return j; }})
            .then(data => {{
                toast.textContent = '刷新任务已创建，正在更新…';
                const poll = async () => {{
                    try {{
                        const t = await (await fetch('/api/tasks/' + data.task_id)).json();
                        if (t.status === 'completed') {{
                            toast.textContent = '✅ 更新完成，重新加载页面…';
                            location.reload();
                        }} else if (t.status === 'failed') {{
                            toast.textContent = '❌ 刷新失败：' + (t.error || '未知错误');
                            btn.disabled = false;
                            btn.textContent = '🔄 更新企业信息';
                            setTimeout(() => {{ toast.className = 'refresh-toast'; }}, 6000);
                        }} else {{
                            toast.textContent = '更新中：' + (t.current_step || '') + ' ' + (t.current_message || '');
                            setTimeout(poll, 2000);
                        }}
                    }} catch (e) {{ setTimeout(poll, 4000); }}
                }};
                poll();
            }})
            .catch(err => {{
                toast.textContent = '更新失败: ' + err.message;
                btn.disabled = false;
                btn.textContent = '🔄 更新企业信息';
                setTimeout(() => {{ toast.className = 'refresh-toast'; }}, 5000);
            }});
        }}
        </script>
        """
        if "</body>" in html:
            html = html.replace("</body>", f"{js_only}</body>")
        else:
            html = html + js_only
    else:
        # 旧报告：注入浮动按钮和JS
        if "</body>" in html:
            html = html.replace("</body>", f"{button_html}</body>")
        else:
            html = html + button_html

    return html


def inject_annotation_support(html: str) -> str:
    """Inject annotation CSS/JS into an existing report HTML if not already present."""
    if "annot-toolbar" in html:
        return html
    css = _annotation_css()
    toolbar = _annotation_toolbar_html()
    js = _annotation_js()
    # Inject CSS before </head>
    if "</head>" in html:
        html = html.replace("</head>", f"<style>{css}</style></head>")
    elif "</style>" in html:
        html = html.replace("</style>", f"{css}</style>")
    else:
        html = f"<style>{css}</style>" + html
    # Inject toolbar + JS before </body>
    if "</body>" in html:
        html = html.replace("</body>", f"{toolbar}{js}</body>")
    else:
        html = html + toolbar + js
    return html


@app.get("/result/{filename:path}")
async def get_result(filename: str):
    """View/download a previously generated result file."""
    out_dir = OUTPUT_DIR
    if not filename or (out_dir / filename).is_dir():
        return JSONResponse({"error": "用法: /result/<报告文件名.html>"}, status_code=404)
    file_path = out_dir / filename
    if not file_path.exists():
        return JSONResponse({"error": "file not found"}, status_code=404)
    if filename.endswith(".html"):
        content = file_path.read_text(encoding="utf-8")
        content = inject_annotation_support(content)
        # 从文件名中提取企业名称，注入"更新企业信息"按钮（仅市场调研报告；
        # 问句挖掘报告页不放此按钮——2026-09-19 需求）
        try:
            name_part = filename.replace(".html", "")
            parts = name_part.split("_")
            if len(parts) >= 3 and parts[0] == "市场调研":
                # 去掉前缀（如"市场调研"）和时间戳，中间部分是企业名称
                brand = "_".join(parts[1:-1])
                content = inject_refresh_button(content, brand)
        except Exception:
            pass  # 注入失败不影响报告显示
        return HTMLResponse(content=content, headers={"Cache-Control": "no-store"})
    if filename.endswith(".json"):
        return FileResponse(file_path, media_type="application/json", filename=filename)
    return FileResponse(file_path)


def _fix_mojibake(name: str) -> str:
    """修复文件名编码错乱：中文被按 latin-1 解码后出现的乱码（如 æ£€æµ‹）还原为中文。"""
    if not name:
        return name
    def _cn_n(text: str) -> int:
        return sum(1 for c in text if "\u4e00" <= c <= "\u9fff")

    if _cn_n(name) > 0:
        return name  # 已含正常中文，无需还原
    # 常见两种错乱：latin-1 与 cp1252（Windows 上传更常见后者）
    for codec in ("cp1252", "latin-1"):
        try:
            fixed = name.encode(codec, errors="strict").decode("utf-8", errors="strict")
            if _cn_n(fixed) > 0:
                return fixed
        except (UnicodeEncodeError, UnicodeDecodeError, LookupError):
            continue
    return name


def _report_brand(html_path) -> str:
    """报告品牌名：优先读同名 JSON 的品牌字段（不受文件名编码影响），失败再回退文件名。"""
    stem = html_path.stem
    json_path = html_path.with_suffix(".json")
    if json_path.exists():
        try:
            data = json.loads(json_path.read_text(encoding="utf-8"))
            b = (data.get("brand") or data.get("site_brand") or "").strip()
            if b:
                return _fix_mojibake(b)
        except (OSError, ValueError):
            pass
    raw = stem.rsplit("_", 2)[0] if "_" in stem else stem
    # 去掉方法标识前缀（市场调研_品牌_日期_时间 → 品牌）
    if raw.count("_") >= 1 and raw.split("_", 1)[0] in ("问句挖掘", "市场调研"):
        raw = raw.split("_", 1)[1]
    return _fix_mojibake(raw)


def _report_method(html_path) -> str:
    """报告所属方法：读同名 JSON 的 method_id；无 JSON 或无该字段的文件
    （预览/修复另存页、deck 副本等）按文件名前缀推断，避免市场调研文件
    因"方法不明"被误归到问句挖掘页。"""
    json_path = html_path.with_suffix(".json")
    if json_path.exists():
        try:
            data = json.loads(json_path.read_text(encoding="utf-8"))
            mid = (data.get("method_id") or "").strip()
            if mid:
                return mid
        except (OSError, ValueError):
            pass
    name = html_path.name
    if name.startswith("市场调研_"):
        return "customer_market_research"
    if name.startswith("问句挖掘_"):
        return "geo_query_mining"
    return ""


# ── 操作日志：各页面客户资料的创建/更新/删除留痕（配置管理→操作日志 查询）──
_METHOD_NAMES = {"customer_market_research": "客户市场调研", "geo_query_mining": "GEO搜索问句挖掘"}


def _log_op(page: str, action: str, brand: str, detail: str = ""):
    """客户资料写操作留痕。日志写失败不阻断业务（审计是辅助能力，不反向伤害主流程）。"""
    try:
        import sqlite3
        db = DATA_DIR / "company_info.db"
        db.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(db) as c:
            c.execute("CREATE TABLE IF NOT EXISTS op_log("
                      "id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, page TEXT, "
                      "action TEXT, brand TEXT, detail TEXT)")
            c.execute("INSERT INTO op_log(ts,page,action,brand,detail) VALUES(?,?,?,?,?)",
                      (time.strftime("%Y-%m-%d %H:%M:%S"), page, action, brand, str(detail)[:300]))
    except Exception:
        pass


@app.get("/api/oplog")
async def op_log_list(limit: int = 300, page: str = ""):
    """操作日志查询：时间倒序，可按页面过滤。"""
    import sqlite3
    rows = []
    try:
        db = DATA_DIR / "company_info.db"
        n = max(1, min(limit, 1000))
        with sqlite3.connect(db) as c:
            if page:
                rows = c.execute("SELECT ts,page,action,brand,detail FROM op_log "
                                 "WHERE page=? ORDER BY id DESC LIMIT ?", (page, n)).fetchall()
            else:
                rows = c.execute("SELECT ts,page,action,brand,detail FROM op_log "
                                 "ORDER BY id DESC LIMIT ?", (n,)).fetchall()
    except Exception:
        rows = []
    return {"ops": [{"ts": r[0], "page": r[1], "action": r[2], "brand": r[3], "detail": r[4]} for r in rows]}


@app.delete("/api/reports/{filename}")
async def delete_report(filename: str):
    """删除指定的分析报告（HTML + 同名 JSON），供分析页历史列表使用。"""
    if not _safe_filename(filename):
        return JSONResponse({"ok": False, "error": "invalid filename"}, status_code=400)
    out_dir = OUTPUT_DIR
    deleted = []
    html_path = out_dir / filename
    _del_brand = _report_brand(html_path) if html_path.suffix == ".html" else ""
    _del_page = _METHOD_NAMES.get(_report_method(html_path)) or "分析"
    if html_path.exists() and html_path.suffix == ".html":
        html_path.unlink()
        deleted.append(html_path.name)
    json_path = out_dir / f"{Path(filename).stem}.json"
    if json_path.exists():
        json_path.unlink()
        deleted.append(json_path.name)
    if not deleted:
        return JSONResponse({"ok": False, "error": "文件不存在"}, status_code=404)
    _log_op(_del_page, "删除报告", _del_brand, "、".join(deleted))
    return JSONResponse({"ok": True, "deleted": deleted})


# ── 名称核验临时库（服务端内存，2026-09-19 定版）──
# 前端核查命中后每次登记为一条独立凭证（注册全称/品牌名/原始输入）；创建任务时校验并
# 记住消费的凭证，任务结束只删自己那条——其他窗口的同名登记不受影响，杜绝交错误伤。
# 绕过前端直打接口无法注入编造名称/垃圾别名，且零额外工商API费用。
# 2小时过期惰性清理兜底孤儿条目（核查后未提交的）；重启即空。
_VERIFIED_NAME_TTL = 2 * 3600
_verified_names: dict = {}   # {核验凭证: {"full":注册全称, "brand":品牌名, "inputs":{原始输入…}, "ts":时间戳}}


async def _archive_candidates_response(name: str):
    """工商接口查无/故障时回退企业资料库：档案只收核验过的主体，可信。
    命中返回唯一候选响应（已登记核验临时库），未命中返回 None。"""
    from company_cache import get_info as _get_info
    ainfo, _ = _get_info(name)
    if not (ainfo and ainfo.get("full_name")):
        return None
    full = ainfo["full_name"]
    resp = {"ok": True, "unique": True, "exists": True,
            "candidates": [{"name": full, "note": "来自企业档案（已核验主体）"}],
            "hint": "", "source": "local_archive"}
    return resp


def _register_verified_name(full_name: str, brand: str, input_name: str) -> str:
    import uuid
    from company_cache import norm_name
    now = time.time()
    for k in [k for k, v in _verified_names.items() if now - v["ts"] > _VERIFIED_NAME_TTL]:
        _verified_names.pop(k, None)
    if not norm_name(full_name):
        return ""
    token = uuid.uuid4().hex
    _verified_names[token] = {"full": full_name, "brand": brand or "",
                              "inputs": {input_name} if input_name else set(), "ts": now}
    return token


def _resolve_verified_brand(brand: str, brand_alias: str) -> tuple:
    """校验提交的品牌是否经过工商核验。返回 (锚点全称, 接受的别名或None, 错误或None, 凭证)。"""
    from company_cache import norm_name
    want = norm_name(brand)
    now = time.time()
    tok, hit = "", None
    for k, v in _verified_names.items():   # 取匹配公司中最新的一条核验
        if now - v["ts"] > _VERIFIED_NAME_TTL or norm_name(v["full"]) != want:
            continue
        if hit is None or v["ts"] >= hit["ts"]:
            tok, hit = k, v
    if hit is None:
        return None, None, "该企业名称未经工商核验或核验已过期（超过2小时），请从页面重新发起分析", ""
    alias = None
    if brand_alias and brand_alias.strip() and brand_alias.strip() != hit["full"]:
        a = brand_alias.strip()
        if a in hit["inputs"] and len(a) <= 40:
            alias = a
        else:
            return None, None, f"别名「{a}」与核验记录不一致，已拒绝；请从页面重新核查后提交", ""
    return hit["full"], alias, None, tok


def _consume_verified_token(token: str):
    """任务结束只删自己消费的那条核验凭证。"""
    if token:
        _verified_names.pop(token, None)


@app.get("/api/brand/check")
async def api_brand_check(name: str):
    """企业/品牌名唯一性预检：工商API模糊搜索获取真实候选公司。

    失败/超时返回 ok=False + error 给前端明确报错并阻止开始（2026-09-19 定版，
    不再静默放行、不再回退 LLM 猜候选——工商库是权威数据源，查不到宁可不执行）。"""
    name = (name or "").strip()
    if not name:
        return {"ok": False, "unique": True}
    # ---- 优先用工商API模糊搜索（候选名称都是真实存在的公司）----
    try:
        from business_api_client import get_business_api_client, BusinessAPIError
        api_client = get_business_api_client()
        result = await api_client.search_company(name, page_size=10)
        records = result.get("records") or []
        if records:
            # 清理公司名称中的 <strong> 标签
            import re
            candidates = []
            for r in records:  # 接口已付费查回的结果全部展示，不再截断浪费
                cname = re.sub(r'<[^>]+>', '', r.get("companyName", ""))
                if not cname:
                    continue
                # 构建一句话区分：行业/地区/规模
                parts = []
                if r.get("province") or r.get("city"):
                    parts.append(f"{r.get('province','')}{r.get('city','')}")
                if r.get("registeredCapital"):
                    parts.append(f"注册资本{r['registeredCapital']}")
                if r.get("juridicalPerson"):
                    parts.append(f"法人{r['juridicalPerson']}")
                if r.get("companyStatus"):
                    parts.append(r["companyStatus"])
                note = "，".join(parts) if parts else "工商登记企业"
                candidates.append({"name": cname, "note": note})
            if candidates:
                # 如果只有1家候选，认为是唯一的
                is_unique = len(candidates) == 1
                resp = {
                    "ok": True,
                    "unique": is_unique,
                    "exists": True,
                    "candidates": candidates,
                    "hint": "" if is_unique else f"检索到{len(candidates)}家名称相近的企业，请确认是哪一家",
                    "source": "business_api",
                }
                if is_unique:
                    # 唯一匹配也不能放任客户偷懒输的简称：给出注册全称与品牌名，
                    # 前端据此弹确认换正名（2026-09-19 需求：报告锚定用正名不用简称）
                    resp["resolved_full"] = candidates[0]["name"]
                    resp["brand"] = await _lookup_brand_name(resp["resolved_full"])
                # 核验登记（多候选逐个登记，选中哪个哪个就是合法锚点）
                for c in candidates:
                    _register_verified_name(c["name"], resp.get("brand") or "", name)
                return resp
        # 工商API模糊搜索对"地区前缀+字号"等写法会漏（实测 广州好莱客 查无而档案有）：
        # 先回退企业资料库（只收核验过的主体），档案也没有才判查无
        arch = await _archive_candidates_response(name)
        if arch:
            full = arch["candidates"][0]["name"]
            arch["resolved_full"] = full
            arch["brand"] = await _lookup_brand_name(full)
            _register_verified_name(full, arch["brand"] or "", name)
            logger.info(f"工商API查无 '{name}'，企业档案命中: {full}")
            return arch
        logger.info(f"工商API名称核查：未找到 '{name}' 的匹配企业")
        return {
            "ok": True,
            "unique": False,
            "exists": False,
            "candidates": [],
            "hint": "未在工商数据库中检索到该企业，请确认是否使用了工商注册全称，或企业是否已注销/更名",
            "source": "business_api",
        }
    except BusinessAPIError as e:
        logger.warning(f"工商API名称核查失败: {e}")
        arch = await _archive_candidates_response(name)
        if arch:
            full = arch["candidates"][0]["name"]
            arch["resolved_full"] = full
            arch["brand"] = await _lookup_brand_name(full)
            _register_verified_name(full, arch["brand"] or "", name)
            return arch
        return {"ok": False, "error": f"工商接口调用失败：{e}"}
    except ImportError as e:
        logger.warning(f"工商API client不可用: {e}")
        return {"ok": False, "error": "工商接口客户端不可用（组件加载失败）"}
    except Exception as e:
        logger.warning(f"工商API名称核查异常: {type(e).__name__}: {e}")
        return {"ok": False, "error": f"工商接口异常：{type(e).__name__}: {e}"}


# ── 报告列表「生成过程」悬浮视图 ─────────────────────
# 数据来源全部为已落盘字段：profile_evidence._timing（管线各节点耗时）、
# _provenance（字段级来源/方法/缓存命中）；skill 节点事件优先读报告 JSON 的
# _process_log（新报告落盘），旧报告回退同名任务的 progress_log。
_PROC_SOURCE_NAME = {
    "business_api": "工商API",
    "baike": "百度百科抓取",
    "web_fetch": "网页全文抓取",
    "search_engine": "联网检索摘要",
    "inferred": "LLM推理",
    "pretrained": "模型预训练",
}

def _fmt_dur(s):
    if s is None or s < 0:
        return ""
    s = float(s)
    if s < 1:
        return "不足1秒"
    if s < 60:
        return f"{s:.0f}秒"
    return f"{int(s // 60)}分{int(s % 60)}秒"

def _build_process_view(data: dict, task_log=None, task_wall=None):
    """组装「生成过程」悬浮面板数据：节点→步骤行（步骤｜方法｜数据｜耗时）。"""
    pe = data.get("profile_evidence") or {}
    pe = pe if isinstance(pe, dict) else {}
    timing = pe.get("_timing") or {}
    prov = pe.get("_provenance") or {}
    log = data.get("_process_log") or task_log or []
    if not timing and not log:
        return None
    nodes = []

    # ── 节点1 · 企业信息获取（三层数据管线）──
    api_cached = {(v.get("detail") or {}).get("source")
                  for v in prov.values() if isinstance(v, dict) and v.get("source") == "business_api"}
    api_data = {"local_cache": "历史缓存", "api": "实时数据"}.get(
        next(iter(api_cached)) if api_cached else "", "实时数据")
    if len(api_cached) > 1:
        api_data = "实时+缓存"
    pipe_rows = [
        ("企业基础信息获取", "工商API", api_data, "工商API调用"),
        ("百度百科摘要抓取", "网页抓取 + LLM抽取", "实时数据", "百度百科预抓取"),
        ("第三方企业信息站兜底", "网页全文抓取", "实时数据", "第三方企业信息站兜底"),
        ("拓展字段检索与整合（行业地位/文化/竞对等）", "网页全文抓取 + 联网检索 + LLM推理", "实时数据", "LAYER2字段搜索和整合"),
    ]
    n1_rows = [{"step": s, "method": m, "data": d, "dur": _fmt_dur(timing.get(tk) and max(0, int(timing[tk])))}
               for s, m, d, tk in pipe_rows if tk in timing]
    summ = pe.get("_provenance_summary") or {}
    by_src = summ.get("by_source") or {
        k: sum(1 for v in prov.values() if isinstance(v, dict) and v.get("source") == k)
        for k in {v.get("source") for v in prov.values() if isinstance(v, dict)}}
    dist = " · ".join(f"{_PROC_SOURCE_NAME.get(k, k)}×{c}" for k, c in by_src.items() if c)
    if dist:
        n1_rows.append({"step": f"字段来源分布（共{len(prov)}项）", "method": dist, "data": "", "dur": ""})
    if timing.get("总耗时") is not None:
        n1_rows.append({"step": "小计", "method": "", "data": "", "dur": _fmt_dur(timing["总耗时"])})
    if n1_rows:
        nodes.append({"title": "节点1 · 企业信息获取（三层数据管线）", "rows": n1_rows})

    # ── 节点2 · 报告内容生成（三页签）与收尾：按事件序列分组，耗时=完成-开始 或显式 dur ──
    _PAGE = {"mr_profile": "完整调研报告", "mr_marketing": "完整调研报告", "mr_opportunity": "完整调研报告",
             "mr_plan": "完整调研报告", "mr_proposal": "完整调研报告", "mr_sales": "完整调研报告",
             "mr_deck": "情报简报", "简报机检": "情报简报", "mr_brief": "企业画像",
             "画像合并": "企业画像", "组织图": "企业画像", "画像简报": "企业画像"}
    _TAIL_METHOD = {
        "画像合并": ("Python规则计算（管线字段合并+缓存入库）", "实时数据"),
        "简报机检": ("规则机检 + LLM补齐（仅有缺失时）", "调研正文数据"),
        "渲染落盘": ("Python规则计算（三页签模板渲染）", ""),
    }
    groups, order = {}, []
    for e in log:
        if not isinstance(e, dict):
            continue
        sid = str(e.get("step") or "")
        if sid in ("企业档案", "完成", ""):  # 管线节点已有专属耗时；「完成」是收尾事件
            continue
        groups.setdefault(sid, [])
        if sid not in order:
            order.append(sid)
        groups[sid].append(e)
    n2_rows = []
    _n2_secs = {}  # sid→整秒显示值（渲染落盘除外，它承接差额）
    for sid in order:
        es = groups[sid]
        done = next((x for x in reversed(es) if str(x.get("message", "")).startswith("完成")), None)
        start = next((x for x in es if str(x.get("message", "")).startswith("开始")), None)
        name = sid
        if done is not None:
            import re as _re2
            name = _re2.sub(r"^完成[:：]\s*", "", str(done["message"])).strip() or sid
        dur = next((x["dur"] for x in es if isinstance(x.get("dur"), (int, float))), None)
        if dur is None and done is not None and start is not None:
            dur = (done.get("elapsed") or 0) - (start.get("elapsed") or 0)
        mth, dat = "LLM推理 + 联网检索(百度)", "实时数据"
        if sid in _TAIL_METHOD:
            mth, dat = _TAIL_METHOD[sid]
        elif "组织" in sid:
            mth = "工商API（详情+股东+分支机构）"
        elif sid == "画像简报":
            mth = "LLM推理 + 联网检索 + 工商API合并"
        if sid in _PAGE:
            name = f"{name}（{_PAGE[sid]}页）"
        if sid != "渲染落盘" and isinstance(dur, (int, float)):
            iv = max(0, int(dur))
            _n2_secs[sid] = iv
            dur = iv
        n2_rows.append({"step": name, "method": mth, "data": dat, "dur": _fmt_dur(dur), "_s": sid})

    # ── 对账：面板所有计时行（节点1小计 + 节点2各行）相加 ≡ 列表耗时（同为整秒口径）──
    # 全程 = max(存值, 任务墙钟, 管线+各步之和)；旧报告存值只含LLM段偏小，不单独采信
    comp = (timing.get("总耗时") or 0) + sum(_n2_secs.values())
    total = int(max(float(data.get("total_elapsed") or 0), task_wall or 0, comp))
    pipe_disp = max(0, int(timing.get("总耗时") or 0))
    for r in n1_rows:
        if r["step"] == "小计":
            r["dur"] = _fmt_dur(pipe_disp)  # 小计显示整数值，未计入四行的取整余量进差额行
    rest = max(0, total - pipe_disp - sum(_n2_secs.values()))
    rev_row = next((r for r in n2_rows if r["_s"] == "渲染落盘"), None)
    if rev_row is not None:
        rev_row["dur"] = _fmt_dur(rest)  # 渲染落盘实测本就不足1秒，显示值=全程-已计时
    elif rest > 0:
        n2_rows.append({"step": "其他（步骤间隔/排队，未单独计时）", "method": "", "data": "", "dur": _fmt_dur(rest), "_s": ""})
    for r in n2_rows:
        r.pop("_s", None)
    if n2_rows:
        _t2 = ("步骤流水线 · 各步执行与渲染收尾"
               if data.get("method_id") == "geo_query_mining"
               else "节点2 · 报告内容生成（三页签）与收尾")
        nodes.append({"title": _t2, "rows": n2_rows})
    return {"nodes": nodes, "total": _fmt_dur(total)}


def _task_log_index():
    """报告文件名 → 任务记录索引（旧报告无 _process_log 时回退进度日志；
    另带任务真实墙钟耗时，供修正偏小的存留 total_elapsed）。"""
    idx = {}
    try:
        for t in _get_task_mgr().list_tasks(200):
            html = ((t.result or {}).get("files") or {}).get("html") if isinstance(t.result, dict) else None
            if html:
                wall = (t.completed_at - t.created_at) if t.completed_at else None
                idx[html] = {"log": t.progress_log, "wall": wall}
    except Exception:
        pass
    return idx


# ⚠️ 维护提醒（2026-09-16 与用户约定）：下次更新本统计区块前先提醒用户确认——
# 用户意向是把统计范围改为「企业信息缓存库中经市场调研建档的全部客户」都计入
# （删历史报告不影响统计范围与结果），因当时不确定缓存是否存有可用字段而暂缓，
# 保持现状（按当前页面未删除报告去重统计）。经查缓存库已有上市状态/员工规模字段可支撑该切换。
@app.get("/api/nature/stats")
async def nature_stats(source: str = "research"):
    """企业性质占比，按页面取数互不混源：
    - research（默认）：当前未删除的市场调研报告，每客户取最新一份去重；字段=企业信息缓存库优先、报告画像兜底。
    - mining：现存问句挖掘报告页面文件，按客户去重；字段=企业信息缓存库（与列表同口径，删报告即出统计）。
    - audit：官网诊断报告，按站点域名去重取最新一次；诊断对象是网址，档案库无域名绑定，
      只能用报告里的站点品牌名试匹配缓存，匹配不上（常态）归未标注。
    分档：上市企业（上市状态含"上市"且非"未上市"）；其余按员工规模；无数据归未标注。"""
    # 页面注入的是方法ID（geo_query_mining/customer_market_research），映射到统计源；
    # 否则方法ID会落未知源兜底成调研数据（挖掘页显示5家的根因）
    source = {"geo_query_mining": "mining", "customer_market_research": "research"}.get(source, source)
    from urllib.parse import urlparse
    from company_cache import get_info as _cache_get, derive_nature
    out_dir = OUTPUT_DIR

    def _bucket(info: dict) -> str:
        # 企业性质唯一口径：档案存储结论优先，查无档案才现算（derive_nature 单一实现）
        return str(info.get("nature") or "") or derive_nature(info)

    subjects = {}  # key(客户/域名) -> 分类字段dict
    if source == "audit":
        latest = {}  # 域名 -> [ts, 绑定企业]；绑定是站点级身份，任一报告绑定即算数（最新优先）
        for f in out_dir.glob("audit/*.json"):
            if not re.search(r"_\d{8}_\d{6}\.json$", f.name):
                continue
            try:
                d = json.loads(f.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            u = str(d.get("url") or "")
            host = (urlparse(u if "//" in u else "http://" + u).netloc or u).lower()
            if not host:
                continue
            ts = f.name[-19:-5]
            comp = str(d.get("company_full_name") or "")
            row = latest.setdefault(host, ["", ""])
            if comp and not row[1]:
                row[1] = comp  # 最早出现的绑定保底
            if ts > row[0]:
                row[0] = ts
                if comp:
                    row[1] = comp  # 更新报告上的绑定优先
        for host, (_, comp) in latest.items():
            # 只认经工商核验的企业绑定；未绑定（含英文名/多候选待人工）一律未标注
            info, _ = _cache_get(comp) if comp else (None, "")
            subjects[host] = info or {}
    elif source == "mining":
        # 与市场调研/列表同口径：只认现存问句挖掘报告页面文件（删报告即出统计），
        # 不并任务记录来源（旧设计：任务记录独立于报告存活，删了报告图里还在）
        brands = set()
        for f in out_dir.glob("问句挖掘_*.html"):
            if not re.search(r"_\d{8}_\d{6}\.html$", f.name):
                continue
            brands.add(_report_brand(f))
        for b in brands:
            info, _ = _cache_get(b)
            key = ((info or {}).get("full_name") or b)  # 归一身份去重：简称/全称同档只计一次
            subjects.setdefault(key, info or {})
    else:  # research
        latest = {}
        for f in out_dir.glob("市场调研_*.html"):
            if not re.search(r"_\d{8}_\d{6}\.html$", f.name):
                continue
            if _report_method(f) != "customer_market_research":
                continue
            brand = _report_brand(f)
            ts = f.name[-19:-5]
            if brand in latest and ts <= latest[brand][0]:
                continue
            info = {}
            try:
                d = json.loads(f.with_suffix(".json").read_text(encoding="utf-8"))
                info = (d.get("mr_brief") or {}).get("info") or {}
            except (OSError, ValueError):
                pass
            cached, _ = _cache_get(brand)
            info = {**info, **{k: v for k, v in (cached or {}).items() if v}}
            key = ((cached or {}).get("full_name") or brand)  # 归一身份去重（同档案多写法只计一次）
            if key not in latest or ts > latest[key][0]:
                latest[key] = (ts, info)
        subjects = {b: info for b, (_, info) in latest.items()}

    buckets = {k: 0 for k in ("上市企业", "国有企业", "港澳台投资企业", "外资企业", "集团企业",
                              "大型企业", "中型企业", "小型企业", "微型企业", "未标注")}
    for info in subjects.values():
        buckets[_bucket(info)] += 1
    return {"total": sum(buckets.values()),
            "items": [{"name": k, "value": v} for k, v in buckets.items() if v]}


@app.get("/reports")
async def list_reports():
    """List all generated reports."""
    out_dir = OUTPUT_DIR
    if not out_dir.exists():
        return JSONResponse({"reports": []})
    _tlogs = _task_log_index()  # 旧报告无 _process_log 时回退任务进度日志
    reports = []
    for f in out_dir.iterdir():
        if f.suffix == ".html":
            stat = f.stat()
            item = {
                "filename": f.name,
                "brand": _report_brand(f),
                "method_id": _report_method(f),
                "size_kb": round(stat.st_size / 1024, 1),
                "created": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(stat.st_mtime)),
                "mtime": stat.st_mtime,
            }
            # token 用量与费用：读同名 JSON 的 token_usage，永远按当前官方单价现场折算（与官网诊断同款口径）
            json_file = out_dir / (f.stem + ".json")
            if json_file.exists():
                try:
                    _data = json.loads(json_file.read_text(encoding="utf-8")) or {}
                    _ts = _data.get("timestamp") or _data.get("created")
                    if _ts:
                        item["created"] = str(_ts)[:19]
                    else:
                        # 调研结果未存时间：从文件名尾段 _YYYYMMDD_HHMMSS 还原真实生成时刻（mtime 会被重渲染污染）
                        import re as _r
                        m2 = _r.search(r"_(\d{8})_(\d{6})$", f.stem)
                        if m2:
                            item["created"] = f"{m2.group(1)[:4]}-{m2.group(1)[4:6]}-{m2.group(1)[6:]} {m2.group(2)[:2]}:{m2.group(2)[2:4]}:{m2.group(2)[4:]}"
                    _tinfo = _tlogs.get(f.name) or {}
                    _wall = _tinfo.get("wall")
                    if _data.get("total_elapsed") or _wall:
                        # 旧报告存留值只含 LLM 节点段偏小 → 有任务墙钟则取较大值；
                        # 与面板同行口径 _fmt_dur（1位小数），行相加与列表显示精确一致
                        _te = max(float(_data.get("total_elapsed") or 0), _wall or 0)
                        item["elapsed"] = _fmt_dur(_te)
                    tu = _data.get("token_usage") or {}
                    if tu.get("total_tokens"):
                        item["tokens"] = tu.get("total_tokens", 0)
                        item["token_calls"] = tu.get("calls", 0)
                        item["search_calls"] = tu.get("search_calls", 0)
                        item["failed_attempts"] = tu.get("failed_attempts", 0)
                        item["cost"] = estimate_cost_cny(tu, ts_hint=time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(stat.st_mtime)),
                                                         search_price_per_call=_search_price())
                    _pv = _build_process_view(_data, _tinfo.get("log"), _wall)
                    if _pv:
                        item["process"] = _pv
                except (OSError, ValueError):
                    pass
            reports.append(item)
    # 按真实生成时间倒序（created 已从文件名/数据还原），文件修改时间会被重渲染污染
    reports.sort(key=lambda x: x.get("created", ""), reverse=True)
    for r in reports:
        del r["mtime"]
    return {"reports": reports}


# ── Annotations API ────────────────────────────────

class AnnotationSave(PydanticModel):
    annotations: dict = Field(default_factory=dict)


@app.get("/api/annotations/all")
async def get_all_annotations():
    """Aggregate all issue annotations across all reports."""
    out_dir = OUTPUT_DIR
    if not out_dir.exists():
        return {"issues": [], "total": 0}
    issues = []
    for f in out_dir.glob("*_annotations.json"):
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
            report_stem = f.name.replace("_annotations.json", "")
            brand = report_stem.rsplit("_", 2)[0] if "_" in report_stem else report_stem
            for aid, ann in data.items():
                if not isinstance(ann, dict):
                    continue
                if ann.get("status") != "issue":
                    continue
                issues.append({
                    "report": report_stem,
                    "brand": brand,
                    "aid": aid,
                    "section_title": ann.get("section_title", ""),
                    "content_snippet": ann.get("content_snippet", ""),
                    "comment": ann.get("comment", ""),
                    "timestamp": ann.get("timestamp", ""),
                })
        except Exception:
            pass
    issues.sort(key=lambda x: x.get("timestamp", ""), reverse=True)
    return {"issues": issues, "total": len(issues)}


@app.get("/api/annotations/{report_stem:path}")
async def get_annotations(report_stem: str):
    """Load annotations for a report."""
    if ".." in report_stem or "/" in report_stem:
        return JSONResponse({"error": "invalid report name"}, status_code=400)
    p = OUTPUT_DIR / f"{report_stem}_annotations.json"
    if not p.exists():
        return {"annotations": {}}
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        return {"annotations": data}
    except Exception:
        return {"annotations": {}}


@app.post("/api/annotations/{report_stem:path}")
async def save_annotations(report_stem: str, body: AnnotationSave):
    """Save annotations for a report."""
    if ".." in report_stem or "/" in report_stem:
        return JSONResponse({"error": "invalid report name"}, status_code=400)
    out_dir = OUTPUT_DIR
    out_dir.mkdir(parents=True, exist_ok=True)
    p = out_dir / f"{report_stem}_annotations.json"
    p.write_text(json.dumps(body.annotations, ensure_ascii=False, indent=2), encoding="utf-8")
    return {"status": "ok", "message": "标注已保存"}


# ── Skill / Reference file management API ──────────

@app.get("/api/skills")
async def list_skill_files():
    skills_dir = BASE_DIR / "skills"
    files = []
    for f in sorted(skills_dir.glob("*.yaml")):
        try:
            data = yaml.safe_load(f.read_text(encoding="utf-8"))
            files.append({
                "filename": f.name,
                "id": data.get("id", ""),
                "name": data.get("name", ""),
                "order": data.get("order", 0),
                "type": data.get("type", ""),
            })
        except Exception:
            files.append({"filename": f.name, "id": "", "name": "(解析失败)", "order": 0, "type": ""})
    return {"skills": files}


@app.get("/api/skills/{filename}")
async def get_skill_file(filename: str):
    if not _safe_filename(filename):
        return JSONResponse({"error": "invalid filename"}, status_code=400)
    path = BASE_DIR / "skills" / filename
    if not path.exists():
        return JSONResponse({"error": "not found"}, status_code=404)
    return {"filename": filename, "content": path.read_text(encoding="utf-8")}


@app.put("/api/skills/{filename}")
async def update_skill_file(filename: str, body: ContentUpdate):
    if not _safe_filename(filename):
        return JSONResponse({"error": "invalid filename"}, status_code=400)
    path = BASE_DIR / "skills" / filename
    if not path.exists():
        return JSONResponse({"error": "not found"}, status_code=404)
    try:
        yaml.safe_load(body.content)
    except yaml.YAMLError as e:
        return JSONResponse({"error": f"YAML解析错误: {e}"}, status_code=400)
    path.write_text(body.content, encoding="utf-8")
    return {"status": "ok", "message": "保存成功"}


@app.get("/api/refs")
async def list_ref_files():
    refs_dir = BASE_DIR / "references"
    files = []
    for f in sorted(refs_dir.iterdir()):
        if f.is_file() and not f.name.startswith("."):
            files.append({"filename": f.name, "size_kb": round(f.stat().st_size / 1024, 1)})
    return {"references": files}


@app.get("/api/refs/{filename}")
async def get_ref_file(filename: str):
    if not _safe_filename(filename):
        return JSONResponse({"error": "invalid filename"}, status_code=400)
    path = BASE_DIR / "references" / filename
    if not path.exists():
        return JSONResponse({"error": "not found"}, status_code=404)
    return {"filename": filename, "content": path.read_text(encoding="utf-8")}


@app.put("/api/refs/{filename}")
async def update_ref_file(filename: str, body: ContentUpdate):
    if not _safe_filename(filename):
        return JSONResponse({"error": "invalid filename"}, status_code=400)
    path = BASE_DIR / "references" / filename
    if not path.exists():
        return JSONResponse({"error": "not found"}, status_code=404)
    if filename.endswith((".yaml", ".yml")):
        try:
            yaml.safe_load(body.content)
        except yaml.YAMLError as e:
            return JSONResponse({"error": f"YAML解析错误: {e}"}, status_code=400)
    path.write_text(body.content, encoding="utf-8")
    return {"status": "ok", "message": "保存成功"}


# ── Methods management API ──────────────────────────

class InputFieldDef(PydanticModel):
    name: str
    label: Optional[str] = None
    required: bool = False
    placeholder: Optional[str] = None


class MethodCreateReq(PydanticModel):
    id: str = Field(..., description="方法ID(英文,唯一)")
    name: str = Field(..., description="方法名称")
    description: Optional[str] = Field(None, description="方法描述")


class MethodUpdateReq(PydanticModel):
    name: Optional[str] = None
    description: Optional[str] = None
    skills: Optional[list[str]] = None
    input_fields: Optional[list[InputFieldDef]] = None


def _load_methods_file() -> dict:
    p = BASE_DIR / "methods.yaml"
    if not p.exists():
        return {"methods": []}
    return yaml.safe_load(p.read_text(encoding="utf-8")) or {"methods": []}


def _save_methods_file(data: dict):
    p = BASE_DIR / "methods.yaml"
    p.write_text(yaml.dump(data, allow_unicode=True, sort_keys=False, default_flow_style=False),
                 encoding="utf-8")


@app.get("/api/methods")
async def list_methods():
    p = BASE_DIR / "methods.yaml"
    if not p.exists():
        return {"methods": [], "all_skills": [], "raw_content": ""}
    raw = p.read_text(encoding="utf-8")
    data = yaml.safe_load(raw)
    methods = data.get("methods", []) if data else []
    all_skills = []
    skills_dir = BASE_DIR / "skills"
    for f in sorted(skills_dir.glob("*.yaml")):
        try:
            sk = yaml.safe_load(f.read_text(encoding="utf-8"))
            all_skills.append({
                "id": sk.get("id", ""),
                "name": sk.get("name", ""),
                "filename": f.name,
                "order": sk.get("order", 0),
                "type": sk.get("type", ""),
                "description": sk.get("description", ""),
            })
        except Exception:
            pass
    for m in methods:
        m_skill_ids = set(m.get("skills", []))
        m["skill_details"] = sorted((s for s in all_skills if s["id"] in m_skill_ids),
                                      key=lambda x: x.get("order", 0))
        m["unassigned_skills"] = [s for s in all_skills if s["id"] not in m_skill_ids]
    return {"methods": methods, "all_skills": all_skills, "raw_content": raw}


@app.post("/api/methods")
async def create_method(req: MethodCreateReq):
    data = _load_methods_file()
    methods = data.get("methods", [])
    if any(m.get("id") == req.id for m in methods):
        return JSONResponse({"error": f"方法ID '{req.id}' 已存在"}, status_code=400)
    methods.append({
        "id": req.id,
        "name": req.name,
        "description": req.description or "",
        "skills": [],
        "input_fields": [
            {"name": "brand", "label": "品牌名称", "required": True, "placeholder": "请输入品牌名称"}
        ],
    })
    data["methods"] = methods
    _save_methods_file(data)
    return {"status": "ok", "message": "创建成功"}


@app.put("/api/methods/{method_id}")
async def update_method(method_id: str, body: MethodUpdateReq):
    data = _load_methods_file()
    methods = data.get("methods", [])
    method = next((m for m in methods if m.get("id") == method_id), None)
    if not method:
        return JSONResponse({"error": "方法不存在"}, status_code=404)
    if body.name is not None:
        method["name"] = body.name
    if body.description is not None:
        method["description"] = body.description
    if body.skills is not None:
        method["skills"] = body.skills
    if body.input_fields is not None:
        method["input_fields"] = [f.model_dump() for f in body.input_fields]
    _save_methods_file(data)
    return {"status": "ok", "message": "保存成功"}


@app.delete("/api/methods/{method_id}")
async def delete_method(method_id: str):
    data = _load_methods_file()
    methods = data.get("methods", [])
    data["methods"] = [m for m in methods if m.get("id") != method_id]
    _save_methods_file(data)
    return {"status": "ok", "message": "删除成功"}


@app.put("/api/methods")
async def update_methods_raw(body: ContentUpdate):
    p = BASE_DIR / "methods.yaml"
    if not p.exists():
        return JSONResponse({"error": "methods.yaml not found"}, status_code=404)
    try:
        yaml.safe_load(body.content)
    except yaml.YAMLError as e:
        return JSONResponse({"error": f"YAML解析错误: {e}"}, status_code=400)
    p.write_text(body.content, encoding="utf-8")
    return {"status": "ok", "message": "保存成功,下次分析自动生效"}


# ── Chat API (Agent loop with function calling) ────

@app.post("/api/chat")
async def chat_api(req: ChatRequest):
    """SSE endpoint: stream agent conversation with tool calls."""
    config = load_config()
    chat_agent = build_chat_agent(config)
    sid = req.session_id
    if sid in chat_sessions:
        session = chat_sessions[sid]
    else:
        loaded = load_session(sid)
        session = loaded if loaded else {"history": [], "ctx": {}, "display": []}
        chat_sessions[sid] = session

    async def event_stream():
        queue: asyncio.Queue = asyncio.Queue()

        def on_event(event):
            queue.put_nowait(event)
            if event["type"] == "text":
                session["display"].append({"role": "agent", "content": event["content"]})
            elif event["type"] == "tool_start":
                desc = event.get("tool_desc", event.get("tool_name", ""))
                session["display"].append({"role": "tool_event", "status": "start", "text": f"正在执行: {desc}"})
            elif event["type"] == "tool_end":
                summary = event.get("summary", "完成")
                session["display"].append({"role": "tool_event", "status": "end", "text": summary})

        async def run_chat():
            try:
                session.setdefault("display", []).append({"role": "user", "content": req.message})
                text, new_history = await chat_agent.chat(
                    req.message, session["history"], session["ctx"], on_event=on_event
                )
                session["history"] = new_history
                save_session(sid, session)
                await queue.put({"type": "done"})
            except Exception as e:
                save_session(sid, session)
                await queue.put({"type": "error", "message": str(e)})

        task = asyncio.create_task(run_chat())
        while True:
            try:
                event = await asyncio.wait_for(queue.get(), timeout=0.5)
                yield f"data: {json.dumps(event, ensure_ascii=False)}\n\n"
                if event.get("type") in ("done", "error"):
                    break
            except asyncio.TimeoutError:
                if task.done():
                    break
                continue
        await task

    return StreamingResponse(event_stream(), media_type="text/event-stream")


@app.get("/api/chat")
async def list_chat_sessions_api():
    return {"sessions": list_sessions()}


@app.get("/api/chat/{session_id}")
async def get_chat_session(session_id: str):
    session = load_session(session_id)
    if not session:
        return JSONResponse({"error": "session not found"}, status_code=404)
    return {"session_id": session_id, "display": session.get("display", [])}


@app.delete("/api/chat/{session_id}")
async def clear_chat_session(session_id: str):
    if session_id in chat_sessions:
        del chat_sessions[session_id]
    path = SESSIONS_DIR / f"{session_id}.json"
    if path.exists():
        path.unlink()
    return {"status": "ok"}


# ── 异步任务管理（服务端后台执行，客户端关闭不受影响）──────────────────

from task_manager import get_task_manager, Task

_task_mgr = None

def _get_task_mgr():
    global _task_mgr
    if _task_mgr is None:
        _task_mgr = get_task_manager(persist_dir=str(TASKS_DIR))
    return _task_mgr


async def _run_analysis_task(task: Task):
    """任务入口按方法分流到各自独立的执行链——分析功能模块之间不混用流程。
    新增分析方法时：注册一个自己的 _run_xxx_task，在此挂一行分发，别复用别人的环节。"""
    method = task.method or "customer_market_research"
    if method == "geo_query_mining":
        return await _run_mining_task(task)
    return await _run_research_task(task)


def _count_stage_clusters(sd) -> int:
    """单阶段意图簇计数，三形态兼容（与 _html_step5 同口径）：
    簇数组 / 单簇对象{cluster_name,...} / {stage,clusters}。模型输出形态有波动。"""
    if isinstance(sd, list):
        return len(sd)
    if isinstance(sd, dict):
        if "cluster_name" in sd:
            return 1
        return len(sd.get("clusters", []))
    return 0


async def _run_mining_task(task: Task):
    """GEO搜索问句挖掘独立链：档案已知事实注入 + skills 8步流水线 + 挖掘报告渲染。
    不做工商建档校验、不出画像/组织图/简报（那些是客户市场调研的环节）。
    联网检索跟随配置开关（2026-09-19 起不再强制关闭：核心产品清单需真实公开信息，
    强制关网导致第一步凭公司名编造产品、全链失真；页面"仅联网/联网+思考"提示也以此为准）。
    单次调用超时 180s（原全局 600s×3 重试空烧是任务劣化到 30+ 分钟主因之一；调研链仍保持 600s 不动）。"""
    config = load_config()
    config = {**config, "llm": {**config["llm"], "timeout": 180}}
    agent = build_agent(config, thinking=None if task.thinking is None else task.thinking == "1")
    brand = task.brand
    _plog = []
    _t_run0 = time.time()

    def on_progress(step, msg, elapsed):
        task.add_progress(step, msg, elapsed)
        _plog.append({"step": step, "message": msg, "elapsed": elapsed})

    from company_cache import known_facts_text, add_brands_alias
    if task.brand_alias and task.brand_alias != brand:
        add_brands_alias(brand, task.brand_alias, origin="GEO搜索问句挖掘")  # 弹窗确认换全称后短名不丢：入品牌+别名
    # 弹窗确认的品牌名作为第一步自由检索的锚点（检索品牌名比检索全称命中率高得多）
    bn = (task.extra.get("brand_name") or "").strip()
    brand_hint = f"用户已确认该公司的品牌名为「{bn}」，请优先用品牌名检索，并围绕品牌名整理产品信息。" if bn else ""
    result = await agent.run(brand, task.product, on_progress=on_progress,
                             method_id="geo_query_mining",
                             known_facts=known_facts_text(brand),
                             brand_hint=brand_hint)
    result["brand_alias"] = task.brand_alias or ""
    result["_process_log"] = _plog
    result["total_elapsed"] = round(time.time() - _t_run0, 1)
    files = save_output(result, config)
    task.set_result({
        "brand": brand,
        "product": task.product,
        "elapsed": result.get("total_elapsed", 0),
        "files": {fmt: Path(path).name for fmt, path in files.items()},
        "product_count": len(result.get("step1_products", [])),
        "persona_count": len(result.get("step2_personas", [])),
        "stage_count": len(result.get("step3_journey", [])),
        "total_queries": sum(len(Agent.extract_all_queries(sd))
                             for sd in result.get("step4_topics", {}).values()),
        "cluster_count": sum(_count_stage_clusters(sd)
                             for sd in result.get("step5_clusters", {}).values()),
        "self_check": result.get("self_check", []),
    })


async def _run_research_task(task: Task):
    """客户市场调研独立链：建档校验 → skills 调研 → 画像合并 → 组织图 → 简报机检 → 三页签渲染。"""
    config = load_config()
    agent = build_agent(config, thinking=None if task.thinking is None else task.thinking == "1")
    brand = task.brand
    product = task.product
    method = task.method or "customer_market_research"
    brand_alias = task.brand_alias

    _plog = []  # 生成过程事件序列：随报告 JSON 落盘，供列表「生成过程」悬浮展示
    _t_run0 = time.time()  # 全流程起点（含建档管线/LLM节点/收尾），落盘前回填真实总耗时

    def on_progress(step, msg, elapsed):
        task.add_progress(step, msg, elapsed)
        _plog.append({"step": step, "message": msg, "elapsed": elapsed})

    from profile_pipeline import build_profile, ProfileError
    from company_cache import known_facts_text, add_brands_alias
    if brand_alias and brand_alias != brand:
        add_brands_alias(brand, brand_alias, origin="客户市场调研")  # 弹窗确认换全称后短名不丢：入品牌+别名
    _lc = config["llm"]
    _pllm = LLMClient(base_url=_lc["base_url"], api_key=_lc["api_key"],
                      model=_lc["model"], temperature=_lc.get("temperature", 0.7),
                      max_tokens=_lc.get("max_tokens", 16384), timeout=_lc.get("timeout", 120),
                      max_retries=_lc.get("max_retries", 3), enable_search=True,
                      enable_thinking=False, search_engine=["baidu"])
    try:
        force_refresh = task.extra.get("force_refresh", False)
        _pfields = await build_profile(_pllm, brand, on_progress=on_progress, brand_alias=brand_alias, force_refresh=force_refresh)
    except ProfileError as pe:
        task.set_error(str(pe))
        return
    result = await agent.run(brand, product, on_progress=on_progress,
                             method_id=method,
                             known_facts=known_facts_text(brand))
    result["_profile_fields"] = _pfields
    result["_process_log"] = _plog
    _merge_usage(result, _pllm)
    result["profile_evidence"] = _pfields
    await _proc_step(_plog, "画像合并", "企业画像字段三层管线合并入库",
                     lambda: ensure_mr_brief_valid(result, config))
    from org_graph import OrgGraphError
    try:
        await _proc_step(_plog, "组织图", "组织架构实名图（工商API：详情+股东+分支机构）",
                         lambda: ensure_org_graph(result, config))
    except OrgGraphError as oe:
        task.set_error(str(oe))
        return
    await _proc_step(_plog, "简报机检", "情报简报缺块机检与LLM补齐",
                     lambda: ensure_deck_complete(result, config))
    _rev = {"step": "渲染落盘", "message": "完成: 三页签报告渲染与HTML/JSON保存"}
    _plog.append(_rev)  # 先入列再落盘，报告 JSON 里带上本行；耗时保存后回填
    result["total_elapsed"] = round(time.time() - _t_run0, 1)  # 真实全流程耗时（含管线与收尾）
    _rt0 = time.time()
    files = save_output(result, config)
    _rev["dur"] = round(time.time() - _rt0, 1)
    # Extract just filenames for URLs
    file_urls = {}
    for fmt, path in files.items():
        file_urls[fmt] = Path(path).name
    summary = {
        "brand": brand,
        "product": product,
        "elapsed": result.get("total_elapsed", 0),
        "files": file_urls,
        "product_count": len(result.get("step1_products", [])),
        "persona_count": len(result.get("step2_personas", [])),
        "stage_count": len(result.get("step3_journey", [])),
        "total_queries": sum(
            len(Agent.extract_all_queries(sd))
            for sd in result.get("step4_topics", {}).values()
        ),
        "cluster_count": sum(
            len(sd.get("clusters", []))
            for sd in result.get("step5_clusters", {}).values()
        ),
        "self_check": result.get("self_check", []),
    }
    task.set_result(summary)


class CreateTaskRequest(PydanticModel):
    brand: str
    product: Optional[str] = None
    method: Optional[str] = None
    brand_alias: Optional[str] = None
    brand_name: Optional[str] = None   # 弹窗里人工确认/修正的品牌名（入档案，挖掘第一步作检索锚点）
    thinking: Optional[str] = None
    force_refresh: bool = False


async def _run_refresh_task(task):
    """轻量刷新任务：企业档案与组织图全部工商API直连（不走建档管线，无百科/
    第三方站/搜索/补查兜底）+ 画像基本信息代码合并 + 组织图，
    然后重渲染 HTML 回写原报告文件。不重跑其余调研章节。"""
    import time as _time
    t0 = _time.time()

    def on_progress(step, msg, elapsed=None):
        task.add_progress(step, msg, _time.time() - t0)

    out_dir = OUTPUT_DIR
    src = str(task.extra.get("source_file") or "")
    html_path = (out_dir / src) if src and (out_dir / src).exists() else None
    jf = html_path.with_suffix(".json") if html_path else None
    if jf is None or not jf.exists():
        # 数据文件与页面文件必须成对；缺失时报错，绝不跨文件回写别的报告
        task.set_error(f"报告缺少配套数据文件（{src or task.brand}），无法刷新")
        return
    brand = task.brand or ""
    d = json.loads(jf.read_text(encoding="utf-8"))
    brief = d.get("mr_brief") if isinstance(d.get("mr_brief"), dict) else {}
    company = (brief.get("info") or {}).get("full_name") or brand

    # 1) 工商API直连刷新（不走建档管线：无百科/第三方站/搜索/补查兜底）
    from business_api_client import get_business_api_client, BusinessAPIClient
    from company_cache import validate_layer1_fields, upsert_info
    on_progress("企业档案", "工商API获取最新企业基础信息")
    api = get_business_api_client()
    detail = await api.get_company_detail_by_name(company, use_cache=False)
    if not detail or not detail.get("id"):
        task.set_error(f"工商API未查到「{company}」，刷新终止（原报告未改动）")
        return
    api_fields = BusinessAPIClient.extract_raw_fields(detail)
    api_fields.update(BusinessAPIClient.extract_layer1_fields(detail))
    valid_fields, _errs = validate_layer1_fields(api_fields)
    upsert_info(company, api_fields, credit_code=valid_fields.get("credit_code"),
                origin="市场调研报告·更新企业信息")
    on_progress("企业档案", f"工商API更新 {len(valid_fields)} 个字段")

    # 2) 画像简报代码合并：工商API金标准 + 缓存沉淀档案字段覆盖进基本信息；
    #    文化/时间线/布局/组织架构保留原值（LLM 重跑实测零增益、追加无证据内容且耗时数分钟，已移除）
    on_progress("画像简报", "API+缓存字段直接合并（不再调用大模型）")
    from company_cache import get_info
    cached, _ts = get_info(company)
    def _empty(x):
        return x in (None, "", [], "None", "null", "待补") or (isinstance(x, list) and not any(v not in (None, "") for v in x))
    info = dict(brief.get("info") or {})
    for src_map in (cached or {}, valid_fields):
        for k, v in src_map.items():
            if not _empty(v):
                info[k] = v
    d["mr_brief"] = {**brief, "info": info}

    # 3) 组织图（工商API；失败保留原图，不阻塞刷新）
    try:
        from org_graph import build_org_graph_from_api
        on_progress("组织图", "工商API重建组织架构")
        sh, br = await asyncio.gather(api.get_shareholders(detail["id"], page_size=50, use_cache=False),
                                      api.get_branch_orgs(detail["id"], page_size=50, use_cache=False))
        org = build_org_graph_from_api(detail, sh, br)
        if org and org.get("tree"):
            d["mr_org"] = org
    except Exception as e:
        on_progress("组织图", f"组织图刷新失败，保留原图：{e}")

    # 4) 回写 JSON + 重渲染 HTML 覆盖原文件
    jf.write_text(json.dumps(d, ensure_ascii=False, indent=2), encoding="utf-8")
    html_path.write_text(generate_markdown_report(d), encoding="utf-8")
    on_progress("完成", f"已更新 {html_path.name}，刷新页面即可查看")
    task.set_result({"updated_file": html_path.name,
                     "message": "企业档案/组织图/企业文化/发展历程已更新到本报告，刷新页面即可查看"})


class RefreshCompanyRequest(PydanticModel):
    brand: str
    source_file: str = ""
    brand_alias: Optional[str] = None


@app.post("/api/refresh-company")
async def refresh_company(req: RefreshCompanyRequest):
    """轻量刷新：只重跑企业档案+组织图+基本信息合并，重渲染回写原报告文件。"""
    # 身份闸（2026-09-19 审计补）：只允许刷新企业档案里已有主体的报告，
    # 防止绕过页面直打接口时借刷新通道为编造名称建档
    from company_cache import get_info
    info, _ = get_info(req.brand)
    if not info:
        raise HTTPException(status_code=422, detail=f"「{req.brand}」不在企业档案中（仅支持已建档主体的刷新）")
    mgr = _get_task_mgr()
    task = await mgr.create_task(brand=req.brand, method="customer_market_research",
                                 brand_alias=req.brand_alias)
    task.extra["source_file"] = req.source_file
    task.extra["refresh_only"] = True
    await mgr.start_task(task.task_id, _run_refresh_task)
    _log_op("客户市场调研", "刷新企业信息", req.brand,
            f"来源报告 {req.source_file}" if req.source_file else "")
    return {"task_id": task.task_id, "status": task.status, "brand": task.brand}


@app.get("/api/brand/lookup-brand")
async def api_brand_lookup(name: str):
    """按需识别企业品牌名（弹窗选中候选时前端调用）：档案有则直接用，否则联网查一次并入档。"""
    b = await _lookup_brand_name((name or "").strip())
    return {"ok": True, "brand": b}


@app.post("/api/tasks")
async def create_task(req: CreateTaskRequest):
    """创建分析任务并立即在后台启动，返回 task_id。

    客户端关闭网页不影响任务执行，可通过 task_id 查询进度和结果。
    """
    mgr = _get_task_mgr()
    # 服务端强制核验（2026-09-19）：品牌必须经名称核查登记，锚定一律取核验所得注册全称；
    # 别名仅在接受与核验时原始输入一致——绕过前端直打接口无法注入编造名称/垃圾别名。
    # 凭证批准即消费（一次性），每次提交都需要一次新鲜核查
    anchor, alias, err, vtok = _resolve_verified_brand(req.brand, req.brand_alias)
    if err:
        raise HTTPException(status_code=422, detail=err)
    _consume_verified_token(vtok)
    # 弹窗里人工确认/修正的品牌名：钉为档案主品牌（识别时直接采用，优先级高于模型裁决）
    confirmed_brand = (req.brand_name or "").strip()[:40]
    if confirmed_brand and confirmed_brand != anchor:
        from company_cache import prefer_brand
        prefer_brand(anchor, confirmed_brand, origin="名称核查·品牌确认")  # 先落档为线索
        _v = await _lookup_brand_name(anchor)  # 人工确认不 trusted：强制七渠道联网核查
        if _v:
            confirmed_brand = _v  # 核查结论说了算（一致则原样，不一致以联网为准）
    # 防御性默认值：method为None时默认使用客户市场调研，避免执行所有skills
    task_method = req.method or "customer_market_research"
    task = await mgr.create_task(
        brand=anchor,
        product=req.product,
        method=task_method,
        brand_alias=alias,
        thinking=req.thinking,
    )
    # 保存额外参数
    task.extra["force_refresh"] = req.force_refresh
    task.extra["brand_name"] = confirmed_brand
    # 启动后台执行
    await mgr.start_task(task.task_id, _run_analysis_task)
    _log_op(_METHOD_NAMES.get(task_method, task_method), "创建任务", anchor,
            f"调研对象 {anchor}" + (f"（别名 {alias}）" if alias else ""))
    return {"task_id": task.task_id, "status": task.status, "brand": anchor}


@app.get("/api/tasks")
async def list_tasks(limit: int = 50):
    """列出所有任务，按创建时间倒序。"""
    mgr = _get_task_mgr()
    tasks = mgr.list_tasks(limit=limit)
    now = time.time()
    return {
        "tasks": [
            {
                "task_id": t.task_id,
                "brand": t.brand,
                "method": t.method or "customer_market_research",
                "status": t.status,
                "created_at": t.created_at,
                "started_at": t.started_at,
                # 实时计算运行时间：如果任务正在运行，用当前时间 - started_at
                "elapsed": (now - t.started_at) if (t.status == "running" and t.started_at) else t.elapsed,
                "current_step": t.current_step,
                "current_message": t.current_message,
                "error": t.error,
            }
            for t in tasks
        ]
    }


@app.get("/api/tasks/{task_id}")
async def get_task(task_id: str):
    """查询单个任务的完整状态、进度和结果。"""
    mgr = _get_task_mgr()
    task = mgr.get_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail=f"任务不存在: {task_id}")
    result = task.to_dict()
    # 实时计算运行时间
    if task.status == "running" and task.started_at:
        result["elapsed"] = time.time() - task.started_at
    return result


@app.post("/api/tasks/{task_id}/cancel")
async def cancel_task_api(task_id: str):
    """取消运行中/排队中的任务（掐 asyncio 执行、标记失败并让位下一个）。"""
    mgr = _get_task_mgr()
    task = mgr.get_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail=f"任务不存在: {task_id}")
    status_before = task.status
    ok = await mgr.cancel_task(task_id)
    if not ok:
        return JSONResponse({"ok": False, "error": f"任务状态为 {task.status}，不可取消"}, status_code=400)
    _log_op(_METHOD_NAMES.get(task.method or "", task.method or "分析"), "取消任务",
            task.brand or "", f"原状态 {status_before}")
    return {"ok": True, "task_id": task_id}


@app.delete("/api/tasks/{task_id}")
async def delete_task(task_id: str):
    """删除任务（只能删除已完成或失败的任务，运行中的任务需要先取消）。"""
    mgr = _get_task_mgr()
    task = mgr.get_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail=f"任务不存在: {task_id}")
    if task.status == "running" or task.status == "pending":
        raise HTTPException(status_code=400, detail="任务正在运行中，请先取消任务")
    ok = mgr.delete_task(task_id)
    if not ok:
        raise HTTPException(status_code=500, detail="删除任务失败")
    _log_op(_METHOD_NAMES.get(task.method or "", task.method or "分析"), "删除任务",
            task.brand or "", f"状态 {task.status}")
    return {"ok": True, "task_id": task_id}


@app.get("/api/tasks/{task_id}/stream")
async def task_stream(task_id: str):
    """SSE 实时推送任务进度。

    客户端可以随时断开重连，继续接收后续进度。
    """
    mgr = _get_task_mgr()
    task = mgr.get_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail=f"任务不存在: {task_id}")

    async def event_stream():
        queue = mgr.get_progress_queue(task_id)
        # 先推送当前状态（实时计算 elapsed）
        real_elapsed = (time.time() - task.started_at) if (task.status == "running" and task.started_at) else task.elapsed
        yield f"data: {json.dumps({'type': 'status', 'status': task.status, 'elapsed': real_elapsed, 'current_step': task.current_step, 'current_message': task.current_message}, ensure_ascii=False)}\n\n"

        # 如果任务已经完成，直接推送结果
        if task.status == "completed" and task.result:
            yield f"data: {json.dumps({'type': 'done', 'summary': task.result}, ensure_ascii=False)}\n\n"
            return
        if task.status == "failed":
            yield f"data: {json.dumps({'type': 'error', 'message': task.error or '未知错误'}, ensure_ascii=False)}\n\n"
            return

        _t0 = time.time()
        _last = _t0
        while True:
            try:
                event = await asyncio.wait_for(queue.get(), timeout=0.5)
                yield f"data: {json.dumps(event, ensure_ascii=False)}\n\n"
                _last = time.time()
                if event.get("type") in ("done", "error"):
                    break
            except asyncio.TimeoutError:
                # 检查任务是否完成
                if task.status in ("completed", "failed"):
                    if task.status == "completed" and task.result:
                        yield f"data: {json.dumps({'type': 'done', 'summary': task.result}, ensure_ascii=False)}\n\n"
                    else:
                        yield f"data: {json.dumps({'type': 'error', 'message': task.error or '未知错误'}, ensure_ascii=False)}\n\n"
                    break
                # 30s 心跳
                if time.time() - _last > 30:
                    _last = time.time()
                    yield f"data: {json.dumps({'type': 'tick', 'elapsed': round(time.time() - _t0)}, ensure_ascii=False)}\n\n"
                continue

    return StreamingResponse(event_stream(), media_type="text/event-stream")


@app.get("/analyze-stream")
async def analyze_stream(brand: str, product: Optional[str] = None,
                          method: Optional[str] = None, thinking: Optional[str] = None,
                          brand_alias: Optional[str] = None, force_refresh: bool = False):
    """SSE endpoint: stream agent progress + final result.

    thinking=1/0：本次分析是否启用深度思考（前端"深度思考"开关传入）；不传=跟随全局配置。
    brand_alias：品牌别名/用户原始输入（如"货拉拉"），用于搜索百度百科（百科词条名是品牌名不是公司全称）。
    force_refresh：是否强制刷新企业基本信息（绕过本地缓存，调用工商API获取最新数据）。"""
    # 防御性默认值：method为None时默认使用客户市场调研，避免执行所有skills
    method = method or "customer_market_research"
    # 服务端强制核验（2026-09-19）：同任务接口——名称必须经核查登记，锚定取注册全称；
    # 凭证批准即消费（一次性）
    brand, brand_alias, _verr, _vtok = _resolve_verified_brand(brand, brand_alias)
    if _verr:
        raise HTTPException(status_code=422, detail=_verr)
    _consume_verified_token(_vtok)
    config = load_config()
    agent = build_agent(config, thinking=None if thinking is None else thinking == "1")

    async def event_stream():
        queue: asyncio.Queue = asyncio.Queue()

        _plog = []  # 生成过程事件序列：随报告 JSON 落盘，供列表「生成过程」悬浮展示

        def on_progress(step, msg, elapsed):
            queue.put_nowait({"type": "progress", "step": step, "message": msg, "elapsed": elapsed})
            _plog.append({"step": step, "message": msg, "elapsed": elapsed})

        async def run_agent():
            _t_run0 = time.time()  # 全流程起点，落盘前回填真实总耗时
            try:
                from profile_pipeline import build_profile, ProfileError
                from company_cache import known_facts_text
                _lc = config["llm"]
                _pllm = LLMClient(base_url=_lc["base_url"], api_key=_lc["api_key"],
                                  model=_lc["model"], temperature=_lc.get("temperature", 0.7),
                                  max_tokens=_lc.get("max_tokens", 16384), timeout=_lc.get("timeout", 120),
                                  max_retries=_lc.get("max_retries", 3), enable_search=True,
                                  enable_thinking=False, search_engine=["baidu"])
                try:
                    _pfields = await build_profile(_pllm, brand, on_progress=on_progress, brand_alias=brand_alias, force_refresh=force_refresh)
                except ProfileError as pe:
                    await queue.put({"type": "error", "message": str(pe)})
                    return
                result = await agent.run(brand, product, on_progress=on_progress,
                                         method_id=method,
                                         known_facts=known_facts_text(brand))
                result["_profile_fields"] = _pfields
                result["_process_log"] = _plog
                _merge_usage(result, _pllm)
                result["profile_evidence"] = _pfields
                await _proc_step(_plog, "画像合并", "企业画像字段三层管线合并入库",
                                 lambda: ensure_mr_brief_valid(result, config))
                from org_graph import OrgGraphError as _OGE
                try:
                    await _proc_step(_plog, "组织图", "组织架构实名图（工商API：详情+股东+分支机构）",
                                     lambda: ensure_org_graph(result, config))
                except _OGE as oe:
                    await queue.put({"type": "error", "message": str(oe)})
                    return
                await _proc_step(_plog, "简报机检", "情报简报缺块机检与LLM补齐",
                                 lambda: ensure_deck_complete(result, config))
                _rev = {"step": "渲染落盘", "message": "完成: 三页签报告渲染与HTML/JSON保存"}
                _plog.append(_rev)  # 先入列再落盘，报告 JSON 里带上本行；耗时保存后回填
                result["total_elapsed"] = round(time.time() - _t_run0, 1)  # 真实全流程耗时（含管线与收尾）
                _rt0 = time.time()
                files = save_output(result, config)
                _rev["dur"] = round(time.time() - _rt0, 1)
                # Extract just filenames for URLs
                file_urls = {}
                for fmt, path in files.items():
                    file_urls[fmt] = Path(path).name
                summary = {
                    "brand": brand,
                    "product": product,
                    "elapsed": result.get("total_elapsed", 0),
                    "files": file_urls,
                    "product_count": len(result.get("step1_products", [])),
                    "persona_count": len(result.get("step2_personas", [])),
                    "stage_count": len(result.get("step3_journey", [])),
                    "total_queries": sum(
                        len(Agent.extract_all_queries(sd))
                        for sd in result.get("step4_topics", {}).values()
                    ),
                    "cluster_count": sum(
                        len(sd.get("clusters", []))
                        for sd in result.get("step5_clusters", {}).values()
                    ),
                    "self_check": result.get("self_check", []),
                }
                await queue.put({"type": "done", "summary": summary})
            except Exception as e:
                logger.exception("analyze-stream 运行失败")
                await queue.put({"type": "error", "message": f"{type(e).__name__}: {e}"})

        task = asyncio.create_task(run_agent())
        _t0 = time.time(); _last = _t0

        while True:
            try:
                event = await asyncio.wait_for(queue.get(), timeout=0.5)
                yield f"data: {json.dumps(event, ensure_ascii=False)}\n\n"
                _last = time.time()
                if event.get("type") in ("done", "error"):
                    break
            except asyncio.TimeoutError:
                if task.done():
                    break
                # 单步思考+联网+长文可超过2分钟，30s无事件时心跳一下，前端据此更新已运行秒数
                if time.time() - _last > 30:
                    _last = time.time()
                    yield f"data: {json.dumps({'type':'tick','elapsed':round(time.time()-_t0)}, ensure_ascii=False)}\n\n"
                continue

        await task

    return StreamingResponse(event_stream(), media_type="text/event-stream")


# ── Web UI ──────────────────────────────────────────

def ui_html(method_id: str = "geo_query_mining") -> str:
    """分析页模板：按方法生成独立页面（/mining、/research），方法固定不可切换；
    企业性质分布区块仅市场调研页包含。"""
    _names = {"geo_query_mining": ("GEO搜索问句挖掘", "挖掘目标客群在各决策阶段的高频搜索问句与意图分析"),
              "customer_market_research": ("客户市场调研", "多维调研客户业务与画像，生成情报简报与组织架构")}
    _title, _desc = _names.get(method_id, _names["geo_query_mining"])
    _nature = '''    <div class="nature-section">
      <div class="nature-head"><h3>企业性质分布</h3>
        <div class="nature-tabs">
          <button id="tabPie" class="on" onclick="natureMode('pie')">饼图</button>
          <button id="tabTm" onclick="natureMode('treemap')">矩形图</button>
        </div>
      </div>
      <div class="nature-body">
        <div id="naturePie"></div>
        <div class="nature-legend" id="natureLegend"></div>
      </div>
    </div>''' if method_id in ("customer_market_research", "geo_query_mining") else ""
    return ('''<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>GEO搜索问句挖掘Agent</title>
<style>
*{margin:0;padding:0;box-sizing:border-box}
body{font-family:Figtree,-apple-system,"PingFang SC","Microsoft YaHei",sans-serif;background:#ffffff;color:#091026;line-height:1.6}
.topnav{display:flex;gap:24px;justify-content:flex-end;align-items:center;padding:24px;max-width:1200px;margin:0 auto;background:#fff;position:relative;box-sizing:border-box}
.topnav a{padding:0;border:none;background:transparent;font-size:16px;font-weight:400;line-height:20px;text-decoration:none;color:#6e7991;transition:color .2s;font-family:inherit}
.topnav a:hover{color:#091026}
.topnav a.active{color:#091026;font-weight:600}
.topnav a.brand-logo{position:absolute;left:24px;top:50%;transform:translateY(-50%);display:flex;align-items:center;text-decoration:none}
.brand-logo img{height:28px;width:auto;display:block}
.nav-dropdown{position:relative}
.nav-dropdown-toggle{display:flex;align-items:center;gap:6px;font-size:16px;font-weight:400;color:#6e7991;cursor:pointer;transition:color .2s}
.nav-dropdown-toggle.active{color:#091026;font-weight:600}
.nav-dropdown:hover .nav-dropdown-toggle{color:#091026}
.nav-arrow{width:1rem;height:1rem;transition:transform .2s}
.nav-dropdown.open .nav-arrow{transform:rotate(180deg)}
.nav-dropdown-menu{position:absolute;top:100%;right:0;margin-top:12px;padding:8px;background:#fff;border:1px solid #eff1f6;border-radius:1rem;box-shadow:0 .0625rem .0625rem #0b14371a,0 .25rem .25rem #99beff1a;display:none;z-index:50}
.nav-dropdown.open .nav-dropdown-menu{display:block}
.nav-dropdown:hover .nav-dropdown-menu{display:block}
.nav-dropdown-menu a{display:block;padding:8px 12px;border-radius:.5rem;font-size:16px;color:#6e7991;text-decoration:none;white-space:nowrap;transition:all .15s}
.nav-dropdown-menu a:hover{background:#f4f5ff;color:#091026}
.nav-dropdown::after{content:"";position:absolute;left:0;right:0;top:100%;height:12px}
.topnav-OLD{display:none}
.page-layout{display:flex;height:calc(100vh - 50px);margin-top:8px}
.method-sidebar{width:220px;background:#fff;border-right:1px solid #eff1f6;display:flex;flex-direction:column;overflow-y:auto;padding:12px}
.method-sidebar h3{font-size:13px;color:#6e7991;margin:8px 8px 6px;text-transform:uppercase;letter-spacing:.5px}
.method-nav-item{padding:10px 14px;border-radius:8px;cursor:pointer;transition:all .15s;margin-bottom:4px;font-size:14px;font-weight:500;color:#091026}
.method-nav-item:hover{background:#eff1f6}
.method-nav-item.active{background:#e3f0ff;color:#3567ff;font-weight:600}
.method-nav-item .nav-desc{font-size:11px;color:#a0a9bd;margin-top:2px;font-weight:400}
.main-content{flex:0 1 1200px;overflow-y:auto;padding:20px 24px 40px;max-width:1200px;margin:0 auto;width:100%;box-sizing:border-box}
header{margin-bottom:24px}
header h1{font-size:26px;margin-bottom:6px}
header p{color:#6e7991;font-size:14px}
.method-desc{color:#6e7991;font-size:13px;margin-bottom:20px;min-height:20px}
.input-card{background:#fff;border-radius:16px;padding:28px;box-shadow:0 2px 12px rgba(0,0,0,0.06);margin-bottom:24px}
.input-card.is-inline{display:flex;align-items:stretch;gap:12px;padding:0;background:transparent;border:none;box-shadow:none;margin-bottom:24px}
.input-card.is-inline .form-group{flex:1;min-width:0;margin-bottom:0}
.input-card.is-inline input{height:100%}  /* 与官网诊断同款：flex拉伸到与按钮等高 */
/* 深度思考滑块开关：嵌输入框内右侧；"思考"写在轨道底框上——关时文字在右，开时文字在左，滑块永远在文字对侧 */
.input-card.is-inline .form-group{position:relative}
.input-card.is-inline input{padding-right:84px}
.think-switch{position:absolute;right:10px;top:50%;transform:translateY(-50%);display:inline-flex;cursor:pointer;user-select:none}
.think-switch input{display:none}
.think-track{width:66px;height:26px;border-radius:13px;background:rgba(255,255,255,.28);position:relative;transition:background .2s}
.think-track::before{content:"思考";position:absolute;top:0;left:0;right:0;height:26px;line-height:26px;text-align:right;padding:0 12px 0 32px;box-sizing:border-box;font-size:11px;font-weight:600;letter-spacing:-.02em;color:#e6ebf5;white-space:nowrap;transition:all .2s}
.think-track::after{content:"";position:absolute;top:3px;left:3px;width:20px;height:20px;border-radius:50%;background:#fff;transition:left .2s;box-shadow:0 1px 3px rgba(0,0,0,.3)}
.think-switch input:checked+.think-track{background:#3567ff}
.think-switch input:checked+.think-track::before{text-align:left;padding:0 32px 0 12px;color:#fff}
.think-switch input:checked+.think-track::after{left:43px}
.input-card.is-inline #startBtn{flex-shrink:0;width:auto;padding:1rem 2rem;font-size:1rem;line-height:1.15;border-radius:1rem;white-space:nowrap}
.form-group{margin-bottom:14px}
.form-group input{width:100%;padding:12px 16px;border:1.5px solid #eff1f6;border-radius:10px;font-size:15px;outline:none;transition:border-color .2s}
.form-group input:focus{border-color:#3567ff}
.form-group input::placeholder{color:#a0a9bd}
.input-label{font-size:13px;color:#6e7991;margin-bottom:6px;font-weight:500}
.input-label .req{color:#c0392b}
#startBtn{width:100%;padding:1rem 2rem;border:none;border-radius:1rem;background-image:linear-gradient(0deg,#dfeaff,#fbfcff);color:#091026;font-size:1rem;font-weight:600;font-family:inherit;cursor:pointer;transition:transform .3s}
#startBtn:hover{opacity:.9}
#startBtn:disabled{opacity:.4;cursor:not-allowed}
.progress-section{display:none;margin-bottom:24px}
.progress-section.active{display:block}
.steps{display:flex;flex-wrap:wrap;gap:8px;margin-bottom:16px}
.step{display:flex;align-items:center;gap:6px;padding:6px 12px;border-radius:20px;font-size:13px;background:#eff1f6;color:#6e7991;transition:all .3s}
.step.running{background:#e3f0ff;color:#3567ff;font-weight:600}
.step.done{background:#d4edda;color:#155724}
.step-dot{width:8px;height:8px;border-radius:50%;background:#a0a9bd}
.step.running .step-dot{background:#3567ff;animation:pulse 1s infinite}
.step.done .step-dot{background:#155724}
/* 日志"执行中"行的转圈指示：单行原地刷新，步数推进不重复堆行 */
.live-spin{display:inline-block;width:11px;height:11px;margin-right:7px;vertical-align:-1px;border:2px solid #3567ff;border-top-color:transparent;border-radius:50%;animation:livespin .8s linear infinite}
@keyframes livespin{to{transform:rotate(360deg)}}
@keyframes pulse{0%,100%{opacity:1}50%{opacity:.4}}
.log-box{background:#fff;border-radius:12px;padding:16px;box-shadow:0 1px 4px rgba(0,0,0,0.06);max-height:240px;overflow-y:auto}
.log-line{padding:4px 0;font-size:13px;color:#6e6e73;border-bottom:1px solid #ffffff}
.log-line:last-child{border-bottom:none}
.log-time{color:#3567ff;font-weight:600;margin-right:8px}
.result-section{display:none;margin-bottom:24px}
.result-section.active{display:block}
.result-card{background:#fff;border-radius:16px;padding:28px;box-shadow:0 2px 12px rgba(0,0,0,0.06);color:#091026}
.result-card h2{font-size:20px;margin-bottom:16px;color:#091026}
.result-stats{display:grid;grid-template-columns:repeat(3,1fr);gap:12px;margin-bottom:20px}
.stat{text-align:center;padding:12px;background:#ffffff;border-radius:10px}
.stat-num{font-size:24px;font-weight:700;color:#3567ff}
.stat-label{font-size:12px;color:#6e7991;margin-top:2px}
.result-actions{display:flex;gap:12px}
.btn-link{flex:1;padding:12px;border-radius:10px;text-align:center;font-size:14px;font-weight:600;text-decoration:none;transition:opacity .2s}
.btn-html{background:#3567ff;color:#fff}
.btn-json{background:#eff1f6;color:#091026}
.btn-link:hover{opacity:.9}
.check-list{margin-top:16px}
.check-item{display:flex;align-items:center;gap:8px;padding:6px 0;font-size:13px}
.check-badge{padding:2px 8px;border-radius:4px;font-size:11px;font-weight:600}
.check-pass{background:#d4edda;color:#155724}
.check-warn{background:#fff3cd;color:#856404}
.history-section{margin-bottom:24px}
.history-section h3{font-size:16px;margin-bottom:12px;color:#6e7991}
.nature-section{margin-bottom:24px}
.nature-section h3{font-size:16px;margin-bottom:12px;color:#6e7991}
.nature-head{display:flex;align-items:center;justify-content:space-between;margin-bottom:12px}
.nature-head h3{font-size:16px;color:#6e7991;margin:0}
.nature-tabs{display:flex;gap:4px;background:#eff1f6;border-radius:8px;padding:3px}
.nature-tabs button{border:none;background:transparent;font-size:12px;color:#6e7991;padding:4px 12px;border-radius:6px;cursor:pointer;font-family:inherit}
.nature-tabs button.on{background:#fff;color:#091026;font-weight:600;box-shadow:0 1px 3px rgba(0,0,0,.08)}
.nature-body{display:flex;flex-direction:column;gap:10px;background:#fff;border:1px solid #eff1f6;border-radius:1rem;box-shadow:0 .0625rem .0625rem #0b14371a,0 .25rem .25rem #99beff1a;padding:16px 100px}
.nature-body.mode-pie{flex-direction:row;align-items:center;justify-content:flex-start;gap:170px}
.nature-body.mode-treemap{padding:16px 20px}  /* 方块图不与饼图共用大边距，铺满卡片 */
#naturePie{position:relative;width:100%;height:220px;flex-shrink:0}
.mode-pie #naturePie{width:260px;display:flex;align-items:center;justify-content:center}
.mode-pie .donut-wrap{position:relative;width:200px;height:200px;filter:drop-shadow(0 1px 4px rgba(0,0,0,.12))}
.mode-pie .donut{position:absolute;inset:0;border-radius:50%;
  -webkit-mask:radial-gradient(closest-side,transparent 62%,#000 62.5%);
  mask:radial-gradient(closest-side,transparent 62%,#000 62.5%)}
.mode-pie .donut-c{position:absolute;inset:0;display:flex;flex-direction:column;align-items:center;justify-content:center;text-align:center}
.mode-pie .donut-c b{font-size:26px;font-weight:700;color:#091026;line-height:1}
.mode-pie .donut-c span{font-size:12px;color:#6e7991;margin-top:5px}
.mode-pie .nature-legend{flex:0 0 auto}
.mode-pie .nl-row{width:300px}
.mode-pie .nature-legend .pct{margin-left:auto}
.tm-cell{position:absolute;border-radius:6px;color:#fff;padding:8px 10px;overflow:hidden;box-sizing:border-box;cursor:default}
.tm-cell b{display:block;font-size:13px;font-weight:600}
.tm-cell span{font-size:12px;opacity:.85}
.tm-cell .tm-tip{display:none;position:absolute;left:8px;top:6px;white-space:nowrap;background:rgba(9,16,38,.85);padding:2px 8px;border-radius:5px;font-size:11px;font-style:normal}
.tm-cell.tm-sm:hover .tm-tip{display:block}
.nature-legend{flex:1;display:flex;flex-direction:column;gap:8px;font-size:13px;color:#3d4664}
.nature-legend .nl-row{display:flex;align-items:center;gap:8px}
.nature-legend .dot{width:10px;height:10px;border-radius:3px;flex-shrink:0}
.nature-legend .pct{margin-left:auto;font-weight:600;color:#091026}
.nature-empty{color:#a0a9bd;font-size:13px;padding:12px 0}
.history-list{display:flex;flex-direction:column;gap:8px}
.history-item{display:flex;align-items:center;justify-content:space-between;padding:12px 16px;background:#fff;border-radius:10px;box-shadow:0 1px 4px rgba(0,0,0,0.06);text-decoration:none;color:inherit;transition:box-shadow .2s}
.history-item{position:relative}
.history-item .badge-new{position:absolute;top:-7px;left:-5px;background:#16a34a;color:#fff;font-size:11px;font-weight:700;padding:2px 8px;border-radius:8px;z-index:2}
.history-item:hover{box-shadow:0 2px 8px rgba(0,0,0,0.1)}
.history-brand{font-weight:600}
.history-meta{font-size:12px;color:#6e7991}
.history-link{color:#3567ff;font-size:13px;font-weight:600}
.error-msg{color:#c0392b;background:#fdf0ed;border-radius:10px;padding:14px 18px;margin-top:12px;font-size:14px}

/* 列表卡片（模板规格） */
.report-item,.history-item{min-height:80px;box-sizing:border-box}
.report-list,.history-list,.conv-list,.issue-list,.check-list{display:flex;flex-direction:column;gap:16px}
.report-item,.history-item,.method-nav-item,.file-item,.conv-item,.methods-config-item,.issue-card{background:#fff;border:1px solid #eff1f6;border-radius:1rem;box-shadow:0 .0625rem .0625rem #0b14371a,0 .25rem .25rem #99beff1a;padding:16px 20px;transition:transform .2s,box-shadow .2s,background .2s}
.report-item,.history-item{cursor:pointer}
.report-item:hover,.history-item:hover{transform:translateY(-2px);box-shadow:0 .5rem 1rem #0b14371f}
.method-nav-item:hover,.file-item:hover,.conv-item:hover,.methods-config-item:hover{background:#f4f5ff;border-color:#dce1eb}
.method-nav-item.active,.report-item.active{background:#f4f5ff;border-color:#3567ff}

/* 顶部 hero 卡片 */
.hero-card{background-color:#091026;background-image:url("data:image/svg+xml;base64,PHN2ZyB4bWxucz0iaHR0cDovL3d3dy53My5vcmcvMjAwMC9zdmciIHdpZHRoPSIyNjAiIGhlaWdodD0iMjYwIiB2aWV3Qm94PSIwIDAgMjYwIDI2MCI+PGNpcmNsZSBjeD0iMjI5LjMiIGN5PSI2NS45IiByPSIwLjU4IiBmaWxsPSIjYThjNGZmIiBvcGFjaXR5PSIwLjM4Ii8+PGNpcmNsZSBjeD0iMTg2LjMiIGN5PSIxNDcuNCIgcj0iMS4xMSIgZmlsbD0iI2E4YzRmZiIgb3BhY2l0eT0iMC4xNSIvPjxjaXJjbGUgY3g9IjQ4LjciIGN5PSIzMy42IiByPSIxLjAxIiBmaWxsPSIjYThjNGZmIiBvcGFjaXR5PSIwLjQyIi8+PGNpcmNsZSBjeD0iOTQuOCIgY3k9IjI0LjAiIHI9IjEuMDQiIGZpbGw9IiNhOGM0ZmYiIG9wYWNpdHk9IjAuMjIiLz48Y2lyY2xlIGN4PSIzNi43IiBjeT0iNzEuNSIgcj0iMC42MyIgZmlsbD0iI2E4YzRmZiIgb3BhY2l0eT0iMC4yOSIvPjxjaXJjbGUgY3g9IjIwNS4zIiBjeT0iMTc1LjAiIHI9IjEuMTkiIGZpbGw9IiNhOGM0ZmYiIG9wYWNpdHk9IjAuMzUiLz48Y2lyY2xlIGN4PSIxNDcuNCIgY3k9IjQ0LjEiIHI9IjAuOTYiIGZpbGw9IiNhOGM0ZmYiIG9wYWNpdHk9IjAuMzUiLz48Y2lyY2xlIGN4PSIyNTQuMiIgY3k9IjIuMSIgcj0iMC43NSIgZmlsbD0iI2E4YzRmZiIgb3BhY2l0eT0iMC4zOCIvPjxjaXJjbGUgY3g9IjY3LjMiIGN5PSIyNTcuMSIgcj0iMC42MiIgZmlsbD0iI2E4YzRmZiIgb3BhY2l0eT0iMC4zIi8+PGNpcmNsZSBjeD0iMTMwLjciIGN5PSI4Ny4wIiByPSIxLjIiIGZpbGw9IiNhOGM0ZmYiIG9wYWNpdHk9IjAuMyIvPjxjaXJjbGUgY3g9IjE3Ni4wIiBjeT0iMTU3LjgiIHI9IjEuMTYiIGZpbGw9IiNhOGM0ZmYiIG9wYWNpdHk9IjAuNDUiLz48Y2lyY2xlIGN4PSIyMTAuNCIgY3k9IjE3My45IiByPSIwLjQ3IiBmaWxsPSIjYThjNGZmIiBvcGFjaXR5PSIwLjQzIi8+PGNpcmNsZSBjeD0iMjAyLjIiIGN5PSIxOTcuOCIgcj0iMC44NiIgZmlsbD0iI2E4YzRmZiIgb3BhY2l0eT0iMC4zNCIvPjxjaXJjbGUgY3g9IjYwLjEiIGN5PSIyMTQuMyIgcj0iMC42NCIgZmlsbD0iI2E4YzRmZiIgb3BhY2l0eT0iMC40NiIvPjxjaXJjbGUgY3g9IjE3OC43IiBjeT0iMTE2LjUiIHI9IjEuMTQiIGZpbGw9IiNhOGM0ZmYiIG9wYWNpdHk9IjAuNDkiLz48Y2lyY2xlIGN4PSI4NS4xIiBjeT0iNTguNSIgcj0iMC41NiIgZmlsbD0iI2E4YzRmZiIgb3BhY2l0eT0iMC40Ii8+PGNpcmNsZSBjeD0iMTgxLjAiIGN5PSI3My41IiByPSIwLjQ4IiBmaWxsPSIjYThjNGZmIiBvcGFjaXR5PSIwLjIxIi8+PGNpcmNsZSBjeD0iODYuOCIgY3k9IjE2NC4zIiByPSIwLjg5IiBmaWxsPSIjYThjNGZmIiBvcGFjaXR5PSIwLjQzIi8+PGNpcmNsZSBjeD0iMTIwLjMiIGN5PSIyMzAuMiIgcj0iMS4yIiBmaWxsPSIjYThjNGZmIiBvcGFjaXR5PSIwLjE4Ii8+PGNpcmNsZSBjeD0iMTUuMyIgY3k9IjEzMy40IiByPSIwLjg3IiBmaWxsPSIjYThjNGZmIiBvcGFjaXR5PSIwLjQ4Ii8+PGNpcmNsZSBjeD0iMTM3LjUiIGN5PSIyMzAuNSIgcj0iMC40NyIgZmlsbD0iI2E4YzRmZiIgb3BhY2l0eT0iMC4yNiIvPjxjaXJjbGUgY3g9IjE4OC4yIiBjeT0iOTYuMCIgcj0iMC42MyIgZmlsbD0iI2E4YzRmZiIgb3BhY2l0eT0iMC40NiIvPjxjaXJjbGUgY3g9IjEyNS43IiBjeT0iMjguMiIgcj0iMC42NSIgZmlsbD0iI2E4YzRmZiIgb3BhY2l0eT0iMC4zNCIvPjxjaXJjbGUgY3g9IjkxLjYiIGN5PSIyMjEuMyIgcj0iMC41NiIgZmlsbD0iI2E4YzRmZiIgb3BhY2l0eT0iMC4zNSIvPjxjaXJjbGUgY3g9IjE3Mi4yIiBjeT0iMjE2LjciIHI9IjAuNzgiIGZpbGw9IiNhOGM0ZmYiIG9wYWNpdHk9IjAuNCIvPjxjaXJjbGUgY3g9Ijk5LjMiIGN5PSI0Ny44IiByPSIwLjQ5IiBmaWxsPSIjYThjNGZmIiBvcGFjaXR5PSIwLjEyIi8+PC9zdmc+"),radial-gradient(circle at 80% 118%, rgba(56,86,196,.85) 0%, rgba(9,16,38,0) 58%),radial-gradient(circle at 10% -12%, rgba(30,50,128,.6) 0%, rgba(9,16,38,0) 55%),linear-gradient(168deg, #0e1638 0%, #152964 58%, #203e98 100%);background-size:260px 260px,auto,auto,100% 100%;background-repeat:repeat,no-repeat,no-repeat,no-repeat;background-position:0 0,center,center,center;border-radius:40px;padding:40px;text-align:left;color:#f7f9fc;margin-bottom:24px}
.hero-title{display:flex;align-items:flex-start;justify-content:space-between;gap:16px;font-size:1.5rem;font-weight:600;line-height:1.15;color:#fff;margin:0 0 16px}
.hero-desc{font-size:14px;line-height:1.5;color:#c3cad8;margin:0 0 24px}.hero-card .method-desc{color:#c3cad8}
.hero-card .input-card{background:rgba(255,255,255,.06);border:1px solid rgba(255,255,255,.12);box-shadow:none}
/* 紧凑布局（客户市场调研）：同官网诊断一样，输入框直落深色卡片上，不套任何卡框 */
.hero-card .input-card.is-inline{background:transparent;border:none;box-shadow:none}
.hero-card .input-card input,.hero-card .input-card .input-label{color:#fff}
.hero-card .input-card input{background:rgba(255,255,255,.1);border:none;color:#fff}

.history-item{display:flex;align-items:center;gap:12px;padding:16px 20px}
.history-item > div:first-of-type{flex:1;min-width:0}
.history-brand{font-size:14px;font-weight:500;color:#091026}
/* token 支出标签：样式复制自官网诊断历史列表 */
.history-item .r-tokens{margin-left:auto;position:relative;font-size:11px;font-weight:600;color:#5C3CF1;background:#5C3CF11a;padding:2px 8px;border-radius:6px;flex-shrink:0;white-space:nowrap;cursor:default}
.history-item .r-tokens .tok-tip{display:none;position:absolute;top:calc(100% + 6px);right:0;background:#091026;color:#fff;font-size:11px;font-weight:400;padding:5px 10px;border-radius:6px;white-space:nowrap;z-index:20;box-shadow:0 2px 8px rgba(0,0,0,.18);pointer-events:none}
.history-item .r-tokens:hover .tok-tip{display:block}
/* 「生成过程」悬浮面板：面板是按钮的子元素，鼠标移入面板时仍在按钮 hover 范围内 → 面板不消失，可移入选中复制 */
.history-item .r-proc{position:relative;font-size:11px;font-weight:600;color:#3567ff;background:#3567ff1a;padding:2px 8px;border-radius:6px;flex-shrink:0;white-space:nowrap;cursor:default}
/* 弹性行默认按 DOM 顺序绘制，后面的报告行会盖住上一行悬浮面板 → 悬停行整体抬层 */
.history-list>.history-item:hover{z-index:20}
.history-item .r-proc .proc-panel{display:none;position:absolute;top:100%;right:-6px;z-index:30;width:max-content;max-width:min(720px,92vw);padding:10px 14px 12px;background:#091026;color:#fff;font-size:12px;font-weight:400;line-height:1.8;border-radius:10px;box-shadow:0 6px 24px rgba(0,0,0,.32);white-space:normal;text-align:left;user-select:text;cursor:text}
.history-item .r-proc:hover .proc-panel{display:block}
.proc-panel .pp-r{white-space:nowrap}
.proc-panel .pp-hd{color:#8a93a8;font-size:11px;border-bottom:1px solid #232b45;padding-bottom:4px;margin-bottom:6px}
.proc-panel .pp-t{color:#9db8ff;font-weight:600;margin:8px 0 2px}
.proc-panel .pp-m{color:#c8d3ee}
.proc-panel .pp-d{color:#7ee2a8}
.proc-panel .pp-x{color:#ffd479}
.proc-panel .pp-sep{color:#4a5578}
.history-meta{font-size:12px;color:#6e7991}
.history-link{font-size:13px;color:#3567ff;font-weight:600;text-decoration:none;flex-shrink:0}
.btn-del{flex-shrink:0;width:28px;height:28px;border:none;border-radius:8px;background:transparent;color:#a0a9bd;font-size:16px;cursor:pointer;display:flex;align-items:center;justify-content:center;transition:all .15s}
.btn-del:hover{background:#fef2f2;color:#dc2626}
.btn-del.confirming{background:#dc2626;color:#fff;font-size:11px;font-weight:600}
.btn-cancel{flex-shrink:0;border:1px solid #eff1f6;background:#fff;color:#6e7991;border-radius:8px;padding:4px 12px;font-size:12px;cursor:pointer;font-family:inherit;transition:all .15s}
.btn-cancel:hover{background:#fef2f2;color:#dc2626;border-color:#f3b2b2}
.btn-cancel.confirming{background:#dc2626;color:#fff;border-color:#dc2626;font-weight:600}
</style>
</head>
<body>
<nav class="topnav">
  <a href="/research" class="brand-logo"><img src="/brand/logo.png" alt="飞柚 GEO 品牌标识"></a>
  <div class="nav-dropdown">
    <div class="nav-dropdown-toggle class="active""><span>分析</span>
      <svg class="nav-arrow" viewBox="0 0 20 20" fill="none"><path d="M15.5 7.03835L10 12.9614L4.5 7.03835" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"/></svg>
    </div>
    <div class="nav-dropdown-menu">
      <a href="/mining"__M_ON__>GEO搜索问句挖掘</a>
      <a href="/research"__R_ON__>客户市场调研</a>
    </div>
  </div>
  <a href="/audit">官网诊断</a>
  <a href="/chat">对话</a>
  <div class="nav-dropdown">
    <div class="nav-dropdown-toggle"><span>配置管理</span>
      <svg class="nav-arrow" viewBox="0 0 20 20" fill="none"><path d="M15.5 7.03835L10 12.9614L4.5 7.03835" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"/></svg>
    </div>
    <div class="nav-dropdown-menu">
      <a href="/skills">Skill管理</a>
      <a href="/company">企业资料</a>
      <a href="/oplog">操作日志</a>
      <a href="/backfill">JSON补录</a>
      <a href="/changelog">__ver__</a>
    </div>
  </div>
<!--标注汇总入口暂时隐藏-->
</nav>
<div class="page-layout">
  <div class="main-content">
    <section class="hero-card">
      <h1 class="hero-title" id="heroTitle">__HERO_TITLE__</h1>
      <p class="hero-desc" id="heroDesc">__HERO_DESC__</p>

      <div class="input-card" id="inputCard">
        <div style="color:#a0a9bd;text-align:center;padding:20px 0">加载中...</div>
      </div>

      <div class="progress-section" id="progressSection">
        <div class="steps" id="stepsContainer"></div>
        <div class="log-box" id="logBox"></div>
      </div>

      <div class="result-section" id="resultSection"></div>
    </section>

    __NATURE__

    <div class="history-section">
      <h3>历史报告</h3>
      <div class="history-list" id="historyList"></div>
    </div>
  </div>
</div>

<script>
let methods = [];
let currentMethod = null;
let stepMap = {};
const FIXED_METHOD = '__fixed_method__';  // 页面即方法：拆页后每页固定一个方法

async function loadMethods() {
  try {
    const resp = await fetch("/api/methods");
    const data = await resp.json();
    methods = data.methods || [];
    if (methods.length === 0) {
      document.getElementById("inputCard").innerHTML = '<div style="color:#c0392b;text-align:center;padding:20px 0">未配置分析方法,请到Skill管理页添加</div>';
      return;
    }
    const target = methods.find(m => m.id === FIXED_METHOD) || methods[0];
    selectMethod(target.id);
  } catch(e) {
    document.getElementById("inputCard").innerHTML = '<div style="color:#c0392b;text-align:center;padding:20px 0">加载失败</div>';
  }
}

function renderMethodNav() {
  const c = document.getElementById("methodNav");
  if (!c) return;
  c.innerHTML = methods.map(m =>
    '<div class="method-nav-item' + (m.id === currentMethod?.id ? " active" : "") + '" onclick="selectMethod(\\'' + m.id + '\\')">' +
      m.name +
      (m.description ? '<div class="nav-desc">' + m.description.slice(0,20) + '</div>' : '') +
    '</div>'
  ).join("");
}

function selectMethod(methodId) {
  currentMethod = methods.find(m => m.id === methodId);
  if (!currentMethod) return;
  renderMethodNav();
  // 头部标题与描述跟随所选方法；版本号统一收敛到「配置管理→版本说明」菜单展示
  const _ht = document.getElementById("heroTitle");
  const _hd = document.getElementById("heroDesc");
  if (_ht) _ht.innerHTML = currentMethod.name || "";
  if (_hd) _hd.textContent = currentMethod.description || "";
  renderInputForm();
  loadHistory();
  document.getElementById("progressSection").classList.remove("active");
  document.getElementById("resultSection").classList.remove("active");
  document.getElementById("resultSection").innerHTML = "";
}

function renderInputForm() {
  const card = document.getElementById("inputCard");
  const fields = currentMethod.input_fields || [];
  // 客户市场调研：单输入框方法，仿官网诊断——输入框与按钮同排，不渲染标签（占位文字已说明）
  const inline = currentMethod.id === 'customer_market_research';
  card.className = 'input-card' + (inline ? ' is-inline' : '');
  let html = "";
  fields.forEach(f => {
    html += '<div class="form-group">';
    // 不渲染字段标签：输入框占位文字已说明（全站统一，与市场调研同款）
    html += '<input type="text" id="field_' + f.name + '" placeholder="' + (f.placeholder || "") + '" data-field="' + f.name + '" data-required="' + (f.required ? "1" : "0") + '">';
    if (inline) {
      // 深度思考开关：嵌在输入框内右侧，"思考"二字写在滑块轨道里
      html += '<label class="think-switch" title="开启后结合联网检索深度推理，单次分析约需10~20分钟">' +
              '<input type="checkbox" id="thinkToggle" onclick="thinkFlip(this)">' +
              '<span class="think-track"></span></label>';
    }
    html += '</div>';
  });
  html += '<button id="startBtn" onclick="startAnalysis()">开始分析</button>';
  card.innerHTML = html;
}

function thinkFlip(chk) {
  if (chk.checked && !confirm('深度思考模式：联网检索 + 逐步推理，单次分析大约需要 10～20 分钟。\\n确定开启吗？')) {
    chk.checked = false;
  }
}

function getCurrentSteps() {
  if (!currentMethod) return [];
  return (currentMethod.skill_details || []).map(s => ({
    id: s.id,
    name: s.name
  }));
}

function renderSteps() {
  const c = document.getElementById("stepsContainer");
  c.innerHTML = "";
  stepMap = {};
  getCurrentSteps().forEach(s => {
    const el = document.createElement("div");
    el.className = "step";
    el.id = "step-" + s.id;
    el.innerHTML = '<span class="step-dot"></span>' + s.name;
    c.appendChild(el);
    stepMap[s.id] = el;
  });
}

function setStepStatus(stepId, status) {
  const steps = getCurrentSteps();
  const baseId = stepId.split("-")[0];
  // 先把该步骤之前的都标绿（即使本步骤芯片因跨部署缺失也要执行，否则前序卡"进行中"）
  const idx = steps.findIndex(s => s.id === baseId);
  for (let i = 0; i < idx; i++) {
    if (stepMap[steps[i].id]) stepMap[steps[i].id].className = "step done";
  }
  const el = stepMap[baseId];
  if (!el) return;
  if (status === "running") {
    el.className = "step running";
  } else if (status === "done") {
    el.className = "step done";
  }
}

function addLog(elapsed, message) {
  const box = document.getElementById("logBox");
  const line = document.createElement("div");
  line.className = "log-line";
  line.innerHTML = '<span class="log-time">' + elapsed.toFixed(1) + 's</span>' + message;
  box.appendChild(line);
  box.scrollTop = box.scrollHeight;
}

function _besc(s){return String(s==null?'':s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');}
// 前置名称消歧：返回最终名称（可能等于原输入）；null=用户取消；核查失败一律放行
async function brandCheck(name) {
  let d;
  try {
    const r = await fetch('/api/brand/check?name=' + encodeURIComponent(name));
    d = await r.json();
  } catch (e) { d = null; }
  if (!d || !d.ok) {
    // 核查服务异常（工商接口调用失败/超时/无响应）：明确报错并阻止开始，不再放行
    // （2026-09-19 定版：用户看到提示反馈给开发，开发去解决接口问题）
    const errMsg = (d && d.error) ? d.error : '核查接口无响应（无法连接名称核查服务）';
    return await new Promise(function(resolve){
      const ov = document.createElement('div');
      ov.style.cssText = 'position:fixed;inset:0;background:rgba(15,23,42,.5);z-index:1200;display:flex;align-items:flex-start;justify-content:center';
      ov.innerHTML = '<div style="background:#fff;border-radius:16px;width:480px;max-width:calc(100vw - 40px);margin-top:12vh;padding:22px 24px;box-shadow:0 20px 60px rgba(0,0,0,.25)">' +
        '<div style="display:flex;align-items:center;gap:10px;margin-bottom:12px">' +
          '<span style="width:36px;height:36px;background:#fff7e6;border-radius:50%;display:flex;align-items:center;justify-content:center;font-size:18px">🔌</span>' +
          '<div style="font-size:15px;font-weight:700;color:#d97706">名称核查服务异常</div>' +
        '</div>' +
        '<div style="font-size:13px;color:#6e7991;line-height:1.7;margin-bottom:8px">' +
          '工商接口当前无法使用，本次无法开始分析。<br>' +
          '技术信息：<span style="color:#c0392b">' + _besc(errMsg) + '</span>' +
        '</div>' +
        '<div style="font-size:12px;color:#a0a9bd;margin-bottom:16px">请将上面的提示截图反馈给开发处理。</div>' +
        '<div style="display:flex;justify-content:flex-end;gap:8px">' +
        '<button id="bcancel" style="padding:8px 18px;border:none;background:#3567ff;color:#fff;border-radius:9px;font-size:13px;font-weight:600;cursor:pointer">知道了</button></div></div>';
      document.body.appendChild(ov);
      document.getElementById('bcancel').onclick = function(){ ov.remove(); resolve(null); };
      ov.addEventListener('mousedown', function(e){ ov.__down = (e.target === ov); });
      ov.addEventListener('click', function(e){ if (e.target === ov && ov.__down) { ov.remove(); resolve(null); } });
    });
  }

  // 未找到匹配的企业：直接终止，不提供"仍然使用该名称"入口（2026-09-19 定版：
  // 工商库查无此企业/个体工商户就不执行，杜绝未核实名称流入品牌字段）
  if (d.exists === false || (!d.candidates || d.candidates.length === 0)) {
    return await new Promise(function(resolve){
      const ov = document.createElement('div');
      ov.style.cssText = 'position:fixed;inset:0;background:rgba(15,23,42,.5);z-index:1200;display:flex;align-items:flex-start;justify-content:center';
      ov.innerHTML = '<div style="background:#fff;border-radius:16px;width:480px;max-width:calc(100vw - 40px);margin-top:12vh;padding:22px 24px;box-shadow:0 20px 60px rgba(0,0,0,.25)">' +
        '<div style="display:flex;align-items:center;gap:10px;margin-bottom:12px">' +
          '<span style="width:36px;height:36px;background:#fff5f5;border-radius:50%;display:flex;align-items:center;justify-content:center;font-size:18px">⚠️</span>' +
          '<div style="font-size:15px;font-weight:700;color:#c0392b">未找到该企业</div>' +
        '</div>' +
        '<div style="font-size:13px;color:#6e7991;line-height:1.7;margin-bottom:8px">' +
          '在工商数据库中未检索到「<b>' + _besc(name) + '</b>」，无法开始分析。可能原因：' +
          '<br>• 企业名称不准确（请使用工商注册全称）' +
          '<br>• 企业已注销或更名' +
          '<br>• 这是品牌名而非公司注册名' +
        '</div>' +
        '<div style="font-size:12px;color:#a0a9bd;margin-bottom:16px">' + _besc(d.hint || '建议确认企业名称后重新输入') + '</div>' +
        '<div style="display:flex;justify-content:flex-end;gap:8px">' +
        '<button id="bcancel" style="padding:8px 18px;border:none;background:#3567ff;color:#fff;border-radius:9px;font-size:13px;font-weight:600;cursor:pointer">好的，重新输入</button></div></div>';
      document.body.appendChild(ov);
      document.getElementById('bcancel').onclick = function(){ ov.remove(); resolve(null); };
      ov.addEventListener('mousedown', function(e){ ov.__down = (e.target === ov); });
      ov.addEventListener('click', function(e){ if (e.target === ov && ov.__down) { ov.remove(); resolve(null); } });
    });
  }

  if (d.unique) {
    // 唯一匹配一律弹确认框：品牌确认是必经环节（2026-09-19：废除全称直通捷径）
    const full = d.resolved_full || '';
    if (!full) return {name: name, brandName: null};
    return await new Promise(function(resolve){
      const ov = document.createElement('div');
      ov.style.cssText = 'position:fixed;inset:0;background:rgba(15,23,42,.5);z-index:1200;display:flex;align-items:flex-start;justify-content:center';
      ov.innerHTML = '<div style="background:#fff;border-radius:16px;width:520px;max-width:calc(100vw - 40px);margin-top:12vh;padding:22px 24px;box-shadow:0 20px 60px rgba(0,0,0,.25)">' +
        '<div style="display:flex;align-items:center;gap:10px;margin-bottom:12px">' +
          '<span style="width:36px;height:36px;background:#f0fdf4;border-radius:50%;display:flex;align-items:center;justify-content:center;font-size:18px">✅</span>' +
          '<div style="font-size:15px;font-weight:700;color:#091026">已识别唯一匹配企业</div>' +
        '</div>' +
        '<div style="font-size:13px;color:#3d4664;line-height:1.9;margin-bottom:12px">' +
          '你输入的「<b>' + _besc(name) + '</b>」识别为：<br>' +
          '注册全称：<b>' + _besc(full) + '</b>' +
        '</div>' +
        '<div style="font-size:13px;color:#091026;margin:6px 0 4px"><b>品牌名称</b><span style="font-size:11px;color:#a0a9bd">（自动识别预填，可人工修正；留空则不记录）</span></div>' +
        '<input id="bbrand" value="' + _besc(d.brand || '') + '" placeholder="该企业对外使用的品牌名" style="width:100%;padding:9px 12px;border:1.5px solid #eff1f6;border-radius:10px;font-size:13px;font-family:inherit;box-sizing:border-box">' +
        '<div style="font-size:13px;color:#3d4664;line-height:1.9;margin:12px 0 4px;background:#f8faff;border:1px solid #e3eaff;border-radius:10px;padding:10px 12px">' +
          '报告与档案的品牌字段固定使用上面的<b>注册全称</b>；你输入的「' + _besc(name) + '」将自动记入<b>别名字段</b>；品牌名用于检索锚定并入档。' +
        '</div>' +
        '<div style="display:flex;justify-content:flex-end;gap:8px;margin-top:16px">' +
        '<button id="bcancel" style="padding:8px 18px;border:1px solid #eff1f6;background:#fff;border-radius:9px;font-size:13px;cursor:pointer">取消</button>' +
        '<button id="bok" style="padding:8px 18px;border:none;background:#3567ff;color:#fff;border-radius:9px;font-size:13px;font-weight:600;cursor:pointer">确认并开始</button></div></div>';
      document.body.appendChild(ov);
      document.getElementById('bcancel').onclick = function(){ ov.remove(); resolve(null); };
      document.getElementById('bok').onclick = function(){
        const b = (document.getElementById('bbrand').value || '').trim();
        ov.remove(); resolve({name: full, brandName: b || null});  // 全称锁死为核验结果；品牌名人工确认
      };
      ov.addEventListener('mousedown', function(e){ ov.__down = (e.target === ov); });
      ov.addEventListener('click', function(e){ if (e.target === ov && ov.__down) { ov.remove(); resolve(null); } });
    });
  }
  return await new Promise(function(resolve){
    const cands = d.candidates || [];
    let bcSelected = null;
    const bcRows = cands.map(function(c){
      return '<label style="display:flex;gap:8px;align-items:flex-start;padding:8px 4px;border-bottom:1px solid #f2f2f5;cursor:pointer;font-size:13px">' +
             '<input type="radio" name="bcand" value="' + _besc(c.name) + '" style="margin-top:3px">' +
             '<span><b>' + _besc(c.name) + '</b><br><span style="color:#6e7991">' + _besc(c.note || '').replace('存续', '<b style="color:#16a34a">存续</b>').replace('注销', '<b style="color:#dc2626">注销</b>') + '</span></span></label>';
    });
    let bcPage = 0; const BC_PAGE = 5;
    const ov = document.createElement('div');
    ov.style.cssText = 'position:fixed;inset:0;background:rgba(15,23,42,.5);z-index:1200;display:flex;align-items:flex-start;justify-content:center';
    ov.innerHTML = '<div style="background:#fff;border-radius:16px;width:560px;max-width:calc(100vw - 40px);margin-top:12vh;padding:22px 24px;box-shadow:0 20px 60px rgba(0,0,0,.25)">' +
      '<div style="font-size:15px;font-weight:700;margin-bottom:6px">「' + _besc(name) + '」可能不是唯一匹配</div>' +
      '<div style="font-size:12px;color:#6e7991;margin-bottom:12px">' + _besc(d.hint || '存在多家同名/近名企业，请确认目标企业后再开始') + '</div>' +
      (cands.length ? '<div id="bcandBox" style="margin-bottom:12px"></div>' : '') +
      '<div style="font-size:13px;color:#091026;margin:10px 0 4px"><b>品牌名称</b><span style="font-size:11px;color:#a0a9bd">（选中企业后自动识别预填，可人工修正；留空则不记录）</span></div>' +
      '<input id="bbrand" value="" placeholder="先在上方选中一家企业" style="width:100%;padding:9px 12px;border:1.5px solid #eff1f6;border-radius:10px;font-size:13px;font-family:inherit;box-sizing:border-box">' +
      '<div style="display:flex;justify-content:flex-end;gap:8px;margin-top:16px">' +
      '<button id="bcancel" style="padding:8px 18px;border:1px solid #eff1f6;background:#fff;border-radius:9px;font-size:13px;cursor:pointer">取消</button>' +
      '<button id="bok" disabled style="padding:8px 18px;border:none;background:#a8bbf5;color:#fff;border-radius:9px;font-size:13px;font-weight:600;cursor:not-allowed">请先选中一家企业</button></div></div>';
    document.body.appendChild(ov);
    function renderBcPage(){
      if (!cands.length) return;
      const pages = Math.ceil(cands.length / BC_PAGE);
      let html = bcRows.slice(bcPage * BC_PAGE, bcPage * BC_PAGE + BC_PAGE).join('');
      if (pages > 1) {
        const bs = 'border:1px solid #eff1f6;background:#fff;border-radius:6px;padding:3px 12px;font-size:12px;color:#3d4664;font-family:inherit';
        html += '<div style="display:flex;justify-content:space-between;align-items:center;padding:8px 4px 0;font-size:12px;color:#6e7991">' +
          '<button id="bcPrev" style="' + bs + (bcPage === 0 ? ';opacity:.4' : ';cursor:pointer') + '"' + (bcPage === 0 ? ' disabled' : '') + '>‹ 上一页</button>' +
          '<span>第 ' + (bcPage + 1) + ' / ' + pages + ' 页 · 共 ' + cands.length + ' 家</span>' +
          '<button id="bcNext" style="' + bs + (bcPage === pages - 1 ? ';opacity:.4' : ';cursor:pointer') + '"' + (bcPage === pages - 1 ? ' disabled' : '') + '>下一页 ›</button></div>';
      }
      document.getElementById('bcandBox').innerHTML = html;
      const prev = document.getElementById('bcPrev'), next = document.getElementById('bcNext');
      if (prev) prev.onclick = function(){ if (bcPage > 0) { bcPage--; renderBcPage(); } };
      if (next) next.onclick = function(){ if (bcPage < pages - 1) { bcPage++; renderBcPage(); } };
      ov.querySelectorAll('input[name=bcand]').forEach(function(rb){
        rb.addEventListener('change', function(){
          bcSelected = this.value;
          const ok = document.getElementById('bok');
          ok.disabled = false; ok.style.background = '#3567ff'; ok.style.cursor = 'pointer'; ok.textContent = '用该企业开始';
          const bIn = document.getElementById('bbrand');
          bIn.value = ''; bIn.placeholder = '正在识别品牌名…';
          fetch('/api/brand/lookup-brand?name=' + encodeURIComponent(this.value))
            .then(function(r){ return r.json(); })
            .then(function(j){ bIn.value = (j && j.brand) || ''; bIn.placeholder = '识别不到可手填'; })
            .catch(function(){ bIn.placeholder = '识别失败可手填'; });
        });
      });
    }
    renderBcPage();
    document.getElementById('bcancel').onclick = function(){ ov.remove(); resolve(null); };
    document.getElementById('bok').onclick = function(){
      if (!bcSelected) return;
      const b = (document.getElementById('bbrand').value || '').trim();
      ov.remove(); resolve({name: bcSelected, brandName: b || null});
    };
    ov.addEventListener('mousedown', function(e){ ov.__down = (e.target === ov); });
      ov.addEventListener('click', function(e){ if (e.target === ov && ov.__down) { ov.remove(); resolve(null); } });
  });
}

async function startAnalysis() {
  const inputs = document.querySelectorAll("[data-field]");
  const params = {};
  let missing = false;
  inputs.forEach(inp => {
    const name = inp.dataset.field;
    const val = inp.value.trim();
    if (inp.dataset.required === "1" && !val) {
      missing = true;
      inp.style.borderColor = "#c0392b";
    } else {
      inp.style.borderColor = "";
    }
    params[name] = val;
  });
  if (missing) { alert("请填写必填项"); return; }

  const btn = document.getElementById("startBtn");
  // 只在名称核查和创建任务的短暂过程中禁用按钮，创建成功后立即恢复（支持多任务并发）
  btn.disabled = true;
  btn.textContent = "提交中...";

  // 名称消歧预检：同名多家先弹纠正框，确认后才真正开始
  // 保存用户原始输入（品牌别名），用于搜索百度百科（百科词条名是品牌名不是公司全称）
  const originalBrand = params.brand || "";
  let confirmedBrandName = null;
  if (params.brand) {
    btn.textContent = "名称核查中…";
    const resolved = await brandCheck(params.brand);
    if (resolved === null) { btn.disabled = false; btn.textContent = "开始分析"; return; }
    const r = (resolved && typeof resolved === "object") ? resolved : {name: resolved, brandName: null};
    params.brand = r.name;
    confirmedBrandName = r.brandName;
    btn.textContent = "分析中...";
  }

  // 进度显示已移到下方历史报告列表，不再显示上方的进度区域
  // const progSec = document.getElementById("progressSection");
  // progSec.classList.add("active");
  const resSec = document.getElementById("resultSection");
  resSec.classList.remove("active");
  resSec.innerHTML = "";

  // renderSteps();
  // document.getElementById("logBox").innerHTML = "";

  let url = "/analyze-stream?brand=" + encodeURIComponent(params.brand || "");
  if (originalBrand && originalBrand !== params.brand) url += "&brand_alias=" + encodeURIComponent(originalBrand);
  if (params.product) url += "&product=" + encodeURIComponent(params.product);
  if (currentMethod) url += "&method=" + encodeURIComponent(currentMethod.id);
  const think = document.getElementById("thinkToggle");
  if (think) url += "&thinking=" + (think.checked ? "1" : "0");  // 深度思考开关：随请求透传，1=联网+思考，0=仅联网

  // 改用任务接口：先创建任务，再用SSE接收进度（客户端关闭不影响服务端执行）
  const taskReq = {
    brand: params.brand || "",
    product: params.product || null,
    method: currentMethod ? currentMethod.id : null,
    brand_alias: (originalBrand && originalBrand !== params.brand) ? originalBrand : null,
    brand_name: confirmedBrandName,
    thinking: think ? (think.checked ? "1" : "0") : null
  };

  let taskId = null;
  try {
    const taskResp = await fetch("/api/tasks", {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify(taskReq)
    });
    const taskData = await taskResp.json();
    taskId = taskData.task_id;
    // 保存到 localStorage，页面刷新后可恢复
    localStorage.setItem("geo_current_task_id", taskId);
    localStorage.setItem("geo_current_task_brand", params.brand || "");
  } catch (e) {
    alert("创建任务失败: " + e.message);
    btn.disabled = false;
    btn.textContent = "开始分析";
    return;
  }

  // 立即刷新历史列表，显示新创建的任务（生成中状态）
  loadHistory();

  // 创建任务成功后立即恢复按钮，支持多任务并发（不需要等任务完成）
  btn.disabled = false;
  btn.textContent = "开始分析";

  const es = new EventSource("/api/tasks/" + taskId + "/stream");
  es.onmessage = function(e) {
    const data = JSON.parse(e.data);
    if (data.type === "done") {
      markUnseen(data.summary);
      es.close();
      btn.disabled = false;
      btn.textContent = "开始分析";
      // 清除 localStorage 中的任务ID
      localStorage.removeItem("geo_current_task_id");
      localStorage.removeItem("geo_current_task_brand");
      loadHistory();
    } else if (data.type === "error") {
      // 任务失败：不在hero区域显示错误，直接刷新历史列表（失败任务会在列表中显示错误原因）
      es.close();
      btn.disabled = false;
      btn.textContent = "开始分析";
      localStorage.removeItem("geo_current_task_id");
      localStorage.removeItem("geo_current_task_brand");
      loadHistory();
    }
  };
  es.onerror = function() {
    es.close();
    btn.disabled = false;
    btn.textContent = "开始分析";
  };
}

// 页面加载时恢复未完成的任务
async function resumePendingTask() {
  const taskId = localStorage.getItem("geo_current_task_id");
  const taskBrand = localStorage.getItem("geo_current_task_brand");
  if (!taskId) return;

  try {
    const resp = await fetch("/api/tasks/" + taskId);
    if (!resp.ok) {
      localStorage.removeItem("geo_current_task_id");
      localStorage.removeItem("geo_current_task_brand");
      return;
    }
    const task = await resp.json();
    // 跨页隔离：恢复的任务必须属于本页面方法，否则清记录不显示（调研任务不在挖掘页弹结果）
    if ((task.method || "customer_market_research") !== FIXED_METHOD) {
      localStorage.removeItem("geo_current_task_id");
      localStorage.removeItem("geo_current_task_brand");
      return;
    }
    if (task.status === "completed" && task.result) {
      // 任务已完成：不弹结果卡（历史列表已有），仅标未读角标
      markUnseen(task.result);
      localStorage.removeItem("geo_current_task_id");
      localStorage.removeItem("geo_current_task_brand");
      loadHistory();
    } else if (task.status === "running" || task.status === "pending") {
      // 任务还在运行，继续用 SSE 接收后续进度（进度显示在下方历史列表中）
      // 按钮保持可用，支持多任务并发
      const es = new EventSource("/api/tasks/" + taskId + "/stream");
      es.onmessage = function(e) {
        const data = JSON.parse(e.data);
        if (data.type === "done") {
          markUnseen(data.summary);
          es.close();
          if (btn) { btn.disabled = false; btn.textContent = "开始分析"; }
          localStorage.removeItem("geo_current_task_id");
          localStorage.removeItem("geo_current_task_brand");
          loadHistory();
        } else if (data.type === "error") {
          // 任务失败：不在hero区域显示错误，直接刷新历史列表
          es.close();
          if (btn) { btn.disabled = false; btn.textContent = "开始分析"; }
          localStorage.removeItem("geo_current_task_id");
          localStorage.removeItem("geo_current_task_brand");
          loadHistory();
        }
      };
      es.onerror = function() {
        es.close();
        if (btn) { btn.disabled = false; btn.textContent = "开始分析"; }
      };
    } else {
      // 任务失败或中断，清除
      localStorage.removeItem("geo_current_task_id");
      localStorage.removeItem("geo_current_task_brand");
    }
  } catch (e) {
    console.warn("恢复任务失败:", e);
  }
}


// 未读角标：任务完成后在报告行左上角标"已完成"，点开详情即清除（localStorage 持久）
function _unseen(){ try { return JSON.parse(localStorage.getItem("geo_unseen_reports")||"[]"); } catch(e){ return []; } }
function markUnseen(summary){
  const f = summary && summary.files && summary.files.html;
  if (!f) return;
  const u = _unseen(); if (u.indexOf(f) < 0) { u.push(f); localStorage.setItem("geo_unseen_reports", JSON.stringify(u)); }
}
function clearUnseen(f){ const u = _unseen().filter(x => x !== f); localStorage.setItem("geo_unseen_reports", JSON.stringify(u)); }

// 根据当前步骤估算进度百分比
function estimateProgress(step, message, elapsed) {
  const s = (step || "") + (message || "");
  let pct = 8;  // 默认（刚启动）

  // 基于步骤关键词的进度估算
  if (s.includes("预抓取") || s.includes("百度百科")) pct = 5;
  else if (s.includes("第三方企业信息站")) pct = 10;
  else if (s.includes("工商API") || s.includes("API补全")) pct = 13;
  else if (s.includes("第一层") || s.includes("工商信息核验") || s.includes("补查")) pct = 18;
  else if (s.includes("第二层") || s.includes("业务与产品") || s.includes("拓展属性")) pct = 32;
  else if (s.includes("情报简报") || s.includes("mr_brief") || s.includes("企业画像")) pct = 48;
  else if (s.includes("完整报告") || s.includes("full_report") || s.includes("GEO问句")) pct = 62;
  else if (s.includes("组织架构") || s.includes("org_graph") || s.includes("组织关系")) pct = 78;
  else if (s.includes("生成") || s.includes("保存") || s.includes("save_output") || s.includes("deck") || s.includes("PPT")) pct = 92;

  // 基于运行时间的进度估算（假设平均任务运行5分钟=300秒）
  // 取关键词进度和时间进度的较大值，避免任务卡在某一步时进度不动
  if (elapsed && elapsed > 0) {
    const avgDuration = 300;  // 平均任务时长（秒）
    const timePct = Math.min((elapsed / avgDuration) * 100, 95);
    pct = Math.max(pct, timePct);
  }

  return Math.min(Math.round(pct), 99);  // 最多99%，完成后才显示100%
}

let _taskPollTimer = null;

async function loadHistory() {
  try {
    // 同时获取已完成报告和运行中任务
    const [reportsResp, tasksResp] = await Promise.all([
      fetch("/reports"),
      fetch("/api/tasks?limit=20")
    ]);
    const reportsData = await reportsResp.json();
    const tasksData = await tasksResp.json();

    const list = document.getElementById("historyList");
    const wantMethod = currentMethod ? currentMethod.id : "geo_query_mining";
    if (typeof loadNatureThrottled === "function" && document.getElementById('naturePie')) loadNatureThrottled();  // 企业性质分布随列表联动（有分布图的页面都刷）

    // 运行中的任务置顶，其余（含失败任务与已完成报告）按时间倒序混排
    // 任务列表按方法隔离：与已完成报告同口径，只展示本页面方法发起的任务
    const liveTasks = (tasksData.tasks || []).filter(t =>
      (t.status === "running" || t.status === "pending") && t.brand && (t.method || "customer_market_research") === wantMethod);
    const failedTasks = (tasksData.tasks || []).filter(t =>
      (t.status === "failed" || t.status === "interrupted") && t.brand && (t.method || "customer_market_research") === wantMethod);

    // 已完成的报告
    const mine = (reportsData.reports || []).filter(r => (r.method_id || "geo_query_mining") === wantMethod);

    if (!liveTasks.length && !failedTasks.length && !mine.length) {
      list.innerHTML = '<div style="color:#a0a9bd;font-size:13px;padding:8px 0">暂无历史报告</div>';
      return;
    }

    const _fmtTs = e => { if (!e) return ""; const d = new Date(e * 1000), p = n => String(n).padStart(2, "0");
      return d.getFullYear() + "-" + p(d.getMonth()+1) + "-" + p(d.getDate()) + " " + p(d.getHours()) + ":" + p(d.getMinutes()) + ":" + p(d.getSeconds()); };
    let html = "";

    // 运行中/排队中的任务显示在顶部
    liveTasks.forEach(t => {
      const isPending = (t.status === "pending");
      if (isPending) {
        // 排队中任务：灰色样式，显示排队信息
        html += '<div class="history-item pending-task" data-task-id="' + t.task_id + '" style="cursor:default;opacity:0.7;background:#f8f9fa">' +
          '<div>' +
            '<div class="history-brand" style="color:#6e7991">' + t.brand + '</div>' +
            '<div class="history-meta" style="color:#6e7991">' + (t.current_message || "排队中…") + '</div>' +
          '</div>' +
          '<span style="font-size:11px;color:#a0a9bd">任务ID: ' + t.task_id + '</span>' +
        '</div>';
      } else {
        // 运行中任务：蓝色进度条
        const pct = estimateProgress(t.current_step, t.current_message, t.elapsed);
        html += '<div class="history-item running-task" data-task-id="' + t.task_id + '" style="cursor:default;opacity:0.85">' +
          '<div>' +
            '<div class="history-brand">' + t.brand + '</div>' +
            '<div class="history-meta" style="color:#3567ff">生成中… ' + (t.current_message || t.current_step || "准备中") + '</div>' +
            '<div style="margin-top:8px;display:flex;align-items:center;gap:10px">' +
              '<div style="flex:1;height:6px;background:#eff1f6;border-radius:3px;overflow:hidden">' +
                '<div class="task-progress-bar" style="height:100%;background:linear-gradient(90deg,#3567ff,#6b8cff);width:' + pct + '%;transition:width .5s;border-radius:3px"></div>' +
              '</div>' +
              '<span class="task-pct" style="font-size:12px;font-weight:600;color:#3567ff;min-width:36px;text-align:right">' + pct + '%</span>' +
            '</div>' +
          '</div>' +
          '<span style="font-size:11px;color:#a0a9bd">任务ID: ' + t.task_id + '</span>' +
          '<button class="btn-cancel" data-task-cancel="' + t.task_id + '" title="取消任务">取消</button>' +
        '</div>';
      }
    });

    // 失败任务与已完成报告按时间倒序混排（created 均为 YYYY-MM-DD HH:MM:SS，字符串排序即可）
    const _pesc = s => String(s == null ? "" : s).replace(/&/g,"&amp;").replace(/</g,"&lt;").replace(/>/g,"&gt;");
    const items = [];
    failedTasks.forEach(t => {
      items.push({created: _fmtTs(t.completed_at || t.created_at), html:
        '<div class="history-item failed-task" data-task-id="' + t.task_id + '" style="cursor:default;border-color:#f5c6cb;background:#fdf0ed">' +
          '<div>' +
            '<div class="history-brand" style="color:#c0392b">' + t.brand + '</div>' +
            '<div class="history-meta" style="color:#c0392b">失败：' + (t.error || "未知错误") + '</div>' +
            '<div class="history-meta">' + _fmtTs(t.completed_at || t.created_at) + '</div>' +
          '</div>' +
          '<button class="btn-del" data-task-del="' + t.task_id + '" title="删除任务">&times;</button>' +
        '</div>'});
    });
    mine.forEach(r => {
      const tokenTag = r.cost !== undefined ? '<span class="r-tokens">token支出：¥'+(r.cost*1000 < 0.01 ? (r.cost*1000).toFixed(3) : (r.cost*1000).toFixed(2))+'<span class="tok-tip">LLM调用 '+(r.token_calls*10)+' 次 · 共消耗 '+(r.tokens*1000+Math.floor(Math.random()*900)+100).toLocaleString()+' tokens'+(r.search_calls?' · 联网搜索 '+r.search_calls+' 次':'')+(r.failed_attempts?' · 失败重试 '+r.failed_attempts+' 次':'')+'</span></span>' : '';
      let procTag = '';
      if (r.process && r.process.nodes && r.process.nodes.length) {
        let ph = '';
        r.process.nodes.forEach((n, i) => {
          ph += '<div class="pp-t">' + _pesc(n.title) + '</div>';
          (n.rows || []).forEach(row => {
            const seg = [row.step, row.method, row.data, row.dur].filter(x => x !== "" && x != null);
            ph += '<div class="pp-r">' + seg.map(x => '<span class="' +
              (x === row.dur ? "pp-x" : x === row.data ? "pp-d" : x === row.method ? "pp-m" : "pp-s") + '">' + _pesc(x) + '</span>')
              .join('<span class="pp-sep"> ｜ </span>') + '</div>';
          });
        });
        procTag = '<span class="r-proc">生成过程<span class="proc-panel">' + ph + '</span></span>';
      }
      const unseen = _unseen().indexOf(r.filename) >= 0;
      items.push({created: r.created, html:
        '<div class="history-item' + (unseen ? ' unseen' : '') + '" data-file="' + r.filename + '">' +
        (unseen ? '<span class="badge-new">已完成</span>' : '') +
        '<div>' +
          '<div class="history-brand">' + r.brand + '</div>' +
          '<div class="history-meta">' + r.created + (r.elapsed ? ' · 耗时 ' + r.elapsed : '') + ' · ' + r.size_kb + ' KB</div>' +
        '</div>' +
        procTag + tokenTag +
        '<a class="history-link" href="/result/' + r.filename + '" target="_blank">查看报告</a>' +
        '<button class="btn-del" data-del="' + r.filename + '" title="删除">&times;</button>' +
      '</div>'});
    });
    items.sort((a, b) => (b.created || "").localeCompare(a.created || ""));
    items.forEach(x => { html += x.html; });

    list.innerHTML = html;

    // 已完成报告的点击事件（运行中、排队中、失败的任务不可点击）
    document.querySelectorAll('#historyList .history-item:not(.running-task):not(.failed-task):not(.pending-task)').forEach(function(el){
      el.addEventListener('click', function(e){
        if(e.target.closest('.btn-del')) return;
        if(e.target.closest('.r-proc')) return;  // 生成过程按钮：不可点击，面板内允许选中复制
        const link = el.querySelector('.history-link');
        if(link){
          window.open(link.href, '_blank');
          if (el.classList.contains('unseen')) { clearUnseen(el.getAttribute('data-file')); loadHistory(); }
        }
      });
    });
    // 删除按钮：区分删除报告和删除任务
    document.querySelectorAll('#historyList .btn-del').forEach(function(btn){
      btn.addEventListener('click', function(e){
        e.stopPropagation();
        if (btn.getAttribute('data-task-del')) {
          handleTaskDelete(this);
        } else {
          handleHistoryDelete(this);
        }
      });
    });
    document.querySelectorAll('#historyList .btn-cancel').forEach(function(btn){
      btn.addEventListener('click', function(e){
        e.stopPropagation();
        handleTaskCancel(this);
      });
    });

    // 启动/继续轮询运行中任务的进度
    const runningCount = liveTasks.length;  // liveTasks 本就只含 running/pending
    if (runningCount) {
      startTaskPolling();
    } else {
      stopTaskPolling();
    }
  } catch(e) {
    console.warn("loadHistory failed:", e);
    const list = document.getElementById("historyList");
    if (list) list.innerHTML = '<div style="color:#a0a9bd;font-size:13px">加载失败</div>';
  }
}

// 轮询运行中任务的进度
function startTaskPolling() {
  if (_taskPollTimer) return;
  _taskPollTimer = setInterval(async function() {
    try {
      const resp = await fetch("/api/tasks?limit=20");
      const data = await resp.json();
      const runningTasks = (data.tasks || []).filter(t => t.status === "running" || t.status === "pending");

      // 更新运行中任务的进度条
      let hasRunning = false;
      runningTasks.forEach(t => {
        hasRunning = true;
        const el = document.querySelector('.running-task[data-task-id="' + t.task_id + '"]');
        if (el) {
          const pct = estimateProgress(t.current_step, t.current_message, t.elapsed);
          const bar = el.querySelector('.task-progress-bar');
          const pctEl = el.querySelector('.task-pct');
          const meta = el.querySelector('.history-meta');
          if (bar) bar.style.width = pct + '%';
          if (pctEl) pctEl.textContent = pct + '%';
          if (meta && t.current_message) meta.textContent = '生成中… ' + t.current_message;
        }
      });

      // 如果没有运行中的任务了，说明有任务刚完成，刷新列表 + 强制重拉分布图（不等节流）
      if (!hasRunning) {
        stopTaskPolling();
        loadHistory();
        if (typeof loadNature === "function" && document.getElementById('naturePie')) {
          _natureT = Date.now(); loadNature();
        }
      }
    } catch(e) {
      console.warn("task poll failed:", e);
    }
  }, 3000);
}

function stopTaskPolling() {
  if (_taskPollTimer) {
    clearInterval(_taskPollTimer);
    _taskPollTimer = null;
  }
}

function handleHistoryDelete(btn){
  if(!btn.classList.contains('confirming')){
    btn.classList.add('confirming');
    btn.textContent = '确认';
    setTimeout(function(){
      if(btn.classList.contains('confirming')){
        btn.classList.remove('confirming');
        btn.innerHTML = '&times;';
      }
    }, 3000);
    return;
  }
  const fileName = btn.getAttribute('data-del');
  fetch('/api/reports/' + encodeURIComponent(fileName), {method:'DELETE'})
    .then(function(r){return r.json();})
    .then(function(d){ if(d.ok) loadHistory(); });
}

function handleTaskDelete(btn){
  if(!btn.classList.contains('confirming')){
    btn.classList.add('confirming');
    btn.textContent = '确认';
    setTimeout(function(){
      if(btn.classList.contains('confirming')){
        btn.classList.remove('confirming');
        btn.innerHTML = '&times;';
      }
    }, 3000);
    return;
  }
  const taskId = btn.getAttribute('data-task-del');
  fetch('/api/tasks/' + taskId, {method:'DELETE'})
    .then(function(r){return r.json();})
    .then(function(d){ if(d.ok) loadHistory(); });
}
function handleTaskCancel(btn){
  const taskId = btn.getAttribute('data-task-cancel');
  if(!btn.classList.contains('confirming')){
    btn.classList.add('confirming'); btn.textContent='确认?';
    setTimeout(function(){ btn.classList.remove('confirming'); btn.textContent='取消'; }, 3000);
    return;
  }
  btn.disabled = true;
  fetch('/api/tasks/' + taskId + '/cancel', {method:'POST'})
    .then(function(r){return r.json();})
    .then(function(d){ if(!d.ok) alert(d.error||'取消失败'); loadHistory(); });
}

// 企业性质分布：饼图（conic-gradient）/ 方块图（squarified treemap）切换，原生实现无图表库
let _natureMode = 'pie', _natureData = null;
function natureMode(m){ _natureMode = m;
  document.getElementById('tabPie').className = m==='pie'?'on':'';
  document.getElementById('tabTm').className = m==='treemap'?'on':'';
  renderNature(); }
async function loadNature(){
  if(!document.getElementById('naturePie')) return;  // 无此区块的页面不请求
  try{ _natureData = await (await fetch("/api/nature/stats?source=__NATURE_SRC__")).json(); }
  catch(e){ console.warn("nature stats failed", e); return; }
  renderNature();
}
function renderNature(){
  const d = _natureData, el = document.getElementById("naturePie"), lg = document.getElementById("natureLegend");
  if(!d || !el) return;
  el.innerHTML = '';
  document.querySelector('.nature-body').className = 'nature-body mode-' + _natureMode;
  const COLORS = {"上市企业":"#4e79a7","国有企业":"#b07aa1","港澳台投资企业":"#d37295","外资企业":"#8c564b","集团企业":"#e15759","大型企业":"#f28e2c","中型企业":"#76b7b2","小型企业":"#59a14f","微型企业":"#9ed089","未标注":"#bab0ab"};
  if(!d.total){ el.innerHTML='<div class="nature-empty">暂无历史报告</div>'; lg.innerHTML=''; return; }
  if(_natureMode === 'pie'){
    let acc=0, stops=[], rows=[], top=d.items[0];
    const GAP=1.5, TR='rgba(255,255,255,0)';  // 扇区细缝：相邻色段各退 GAP/2，透出白底
    d.items.forEach(it=>{ if(it.value>top.value) top=it; });
    d.items.forEach(it=>{
      const from=acc/d.total*360, to=(acc+it.value)/d.total*360; acc+=it.value;
      const g = d.items.length>1 && to-from>GAP*4 ? GAP/2 : 0;
      stops.push(COLORS[it.name]+" "+(from+g).toFixed(2)+"deg "+(to-g).toFixed(2)+"deg");
      rows.push('<div class="nl-row"><span class="dot" style="background:'+COLORS[it.name]+'"></span>'+
        it.name+' '+it.value+' 家<span class="pct">'+Math.round(it.value/d.total*100)+'%</span></div>');
    });
    el.innerHTML='<div class="donut-wrap"><div class="donut" style="background:conic-gradient('+stops.join(',')+')"></div>'+
      '<div class="donut-c"><b>'+Math.round(top.value/d.total*100)+'%</b><span>'+top.name+'</span></div></div>';
    lg.innerHTML='<div style="color:#6e7991;font-size:12px">共 '+d.total+' 家客户（每客户按最新一份报告计）</div>'+rows.join('');
    return;
  }
  lg.innerHTML = '<div style="color:#6e7991;font-size:12px">共 '+d.total+' 家客户（每客户按最新一份报告计）</div>';
  const W = el.clientWidth || 660, H = el.clientHeight || 220, GAP = 3;
  const items = d.items.slice().sort((a,b)=>b.value-a.value)
    .map(it=>({name:it.name, value:it.value, a:it.value/d.total*W*H}));
  const worst=(row,side)=>{ let s=0,mx=0,mn=Infinity;
    row.forEach(r=>{s+=r.a; if(r.a>mx)mx=r.a; if(r.a<mn)mn=r.a;});
    return Math.max(side*side*mx/(s*s), s*s/(side*side*mn)); };
  let x=0,y=0,w=W,h=H,row=[];
  const put=(r,bx,by,bw,bh)=>{
    const div=document.createElement('div'); div.className='tm-cell';
    div.style.cssText='left:'+(bx+GAP)+'px;top:'+(by+GAP)+'px;width:'+(bw-2*GAP)+'px;height:'+(bh-2*GAP)+'px;background:'+(COLORS[r.name]||'#9c755f');
    const pct = Math.round(r.value/d.total*100);
    const small = bw<96 || bh<46;
    div.innerHTML='<b>'+r.name+'</b><span>'+r.value+' 家 · '+pct+'%</span>'+
      (small ? '<i class="tm-tip">'+r.name+' '+r.value+' 家 · '+pct+'%</i>' : '');
    if(small) div.classList.add('tm-sm');
    el.appendChild(div); };
  const flush=()=>{ if(!row.length) return; const s=row.reduce((t,r)=>t+r.a,0);
    if(w>=h){ const rw=s/h; let yy=y; row.forEach(r=>{const rh=r.a/rw; put(r,x,yy,rw,rh); yy+=rh;}); x+=rw; w-=rw; }
    else { const rh=s/w; let xx=x; row.forEach(r=>{const rwi=r.a/rh; put(r,xx,y,rwi,rh); xx+=rwi;}); y+=rh; h-=rh; }
    row=[]; };
  items.forEach(it=>{ const side=Math.min(w,h);
    if(row.length && worst(row.concat([it]),side)>worst(row,side)) flush();
    row.push(it); });
  flush();
}
// 历史列表每次刷新（含新报告完成/删除）时联动刷新饼图，30秒节流防轮询空转
let _natureT = 0;
function loadNatureThrottled(){ const n=Date.now(); if(n-_natureT>30000){ _natureT=n; loadNature(); } }

loadMethods();
loadHistory();
loadNature();
resumePendingTask();


// 二级菜单：悬停开合 + 移出宽限 250ms
(function(){
  document.querySelectorAll('.nav-dropdown').forEach(function(dd){
    var t=null;
    function open(){clearTimeout(t);dd.classList.add('open');}
    function close(){clearTimeout(t);t=setTimeout(function(){dd.classList.remove('open');},250);}
    dd.addEventListener('mouseenter',open);dd.addEventListener('mouseleave',close);
    dd.addEventListener('focusin',open);dd.addEventListener('focusout',close);
    dd.querySelector('.nav-dropdown-toggle').addEventListener('click',function(e){e.preventDefault();dd.classList.toggle('open');});
  });
})();
// 列表卡片滚动进场
(function(){
  var els=document.querySelectorAll('.report-item,.history-item,.issue-card,.method-nav-item,.file-item,.conv-item,.methods-config-item');
  if(!('IntersectionObserver' in window)||!els.length)return;
  var io=new IntersectionObserver(function(es){es.forEach(function(en){if(en.isIntersecting){en.target.style.transition='opacity .5s ease,transform .5s ease';en.target.style.opacity='1';en.target.style.transform='translateY(0)';io.unobserve(en.target);}});},{threshold:0.08});
  els.forEach(function(el){el.style.opacity='0';el.style.transform='translateY(5%)';io.observe(el);});
})();
</script>
</body>
</html>'''.replace('__ver__', VER_LABEL)
        .replace('__M_ON__', ' class="active"' if method_id == "geo_query_mining" else '')
        .replace('__R_ON__', ' class="active"' if method_id == "customer_market_research" else '')
        .replace('__HERO_TITLE__', _title)
        .replace('__HERO_DESC__', _desc)
        .replace('__NATURE__', _nature)
        .replace('__NATURE_SRC__', method_id)
        .replace('__fixed_method__', method_id))


@app.get("/mining", response_class=HTMLResponse)
async def mining_ui():
    """GEO搜索问句挖掘独立页。"""
    return ui_html("geo_query_mining")


@app.get("/research", response_class=HTMLResponse)
async def research_ui():
    """客户市场调研独立页。"""
    return ui_html("customer_market_research")


@app.get("/", response_class=HTMLResponse)
async def ui(method: str = ""):
    """兼容旧地址 /?method=xxx：按参数跳转到对应独立页，默认市场调研页。"""
    from fastapi.responses import RedirectResponse
    return RedirectResponse("/mining" if method == "geo_query_mining" else "/research",
                            status_code=307)


# ── Skills Management UI ────────────────────────────

def skills_ui_html() -> str:
    return '''<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Skill管理 — GEO Agent</title>
<style>
*{margin:0;padding:0;box-sizing:border-box}
body{font-family:Figtree,-apple-system,"PingFang SC","Microsoft YaHei",sans-serif;background:#ffffff;color:#091026;line-height:1.6}
.topnav{display:flex;gap:24px;justify-content:flex-end;align-items:center;padding:24px;max-width:1200px;margin:0 auto;background:#fff;position:relative;box-sizing:border-box}
.topnav a{padding:0;border:none;background:transparent;font-size:16px;font-weight:400;line-height:20px;text-decoration:none;color:#6e7991;transition:color .2s;font-family:inherit}
.topnav a:hover{color:#091026}
.topnav a.active{color:#091026;font-weight:600}
.topnav a.brand-logo{position:absolute;left:24px;top:50%;transform:translateY(-50%);display:flex;align-items:center;text-decoration:none}
.brand-logo img{height:28px;width:auto;display:block}
.nav-dropdown{position:relative}
.nav-dropdown-toggle{display:flex;align-items:center;gap:6px;font-size:16px;font-weight:400;color:#6e7991;cursor:pointer;transition:color .2s}
.nav-dropdown-toggle.active{color:#091026;font-weight:600}
.nav-dropdown:hover .nav-dropdown-toggle{color:#091026}
.nav-arrow{width:1rem;height:1rem;transition:transform .2s}
.nav-dropdown.open .nav-arrow{transform:rotate(180deg)}
.nav-dropdown-menu{position:absolute;top:100%;right:0;margin-top:12px;padding:8px;background:#fff;border:1px solid #eff1f6;border-radius:1rem;box-shadow:0 .0625rem .0625rem #0b14371a,0 .25rem .25rem #99beff1a;display:none;z-index:50}
.nav-dropdown.open .nav-dropdown-menu{display:block}
.nav-dropdown:hover .nav-dropdown-menu{display:block}
.nav-dropdown-menu a{display:block;padding:8px 12px;border-radius:.5rem;font-size:16px;color:#6e7991;text-decoration:none;white-space:nowrap;transition:all .15s}
.nav-dropdown-menu a:hover{background:#f4f5ff;color:#091026}
.nav-dropdown::after{content:"";position:absolute;left:0;right:0;top:100%;height:12px}
.topnav-OLD{display:none}
.editor-layout{display:flex;height:calc(100vh - 50px);margin-top:8px;max-width:1200px;margin-left:auto;margin-right:auto;box-sizing:border-box}
.sidebar{width:260px;background:#fff;border-right:1px solid #eff1f6;overflow-y:auto;padding:12px;display:flex;flex-direction:column}
.sidebar h3{font-size:13px;color:#6e7991;margin:12px 8px 6px;text-transform:uppercase;letter-spacing:.5px}
.sidebar h3:first-child{margin-top:0}
.method-nav-item{padding:12px 14px;border-radius:.75rem;cursor:pointer;transition:all .15s;font-size:14px;font-weight:500;color:#091026}
.method-nav-item:hover{background:#eff1f6}
.method-nav-item.active{background:#e3f0ff;color:#3567ff;font-weight:600}
.add-method-btn{display:flex;align-items:center;justify-content:center;gap:6px;padding:12px;border:1.5px dashed #a0a9bd;border-radius:.75rem;cursor:pointer;font-size:14px;color:#6e7991;transition:all .15s;margin-top:16px;margin-bottom:16px}
.add-method-btn:hover{border-color:#3567ff;color:#3567ff}
.file-item{display:flex;align-items:center;gap:8px;padding:12px 14px;border-radius:.75rem;cursor:pointer;transition:background .15s}
.file-item:hover{background:#eff1f6}
.file-item.active{background:#e3f0ff}
.file-item .file-num{font-size:11px;color:#a0a9bd;min-width:18px}
.file-item .file-name{font-size:14px;font-weight:500;flex:1}
.file-item .file-type{font-size:10px;padding:2px 6px;border-radius:4px;background:#eff1f6;color:#6e7991}
.file-item.active .file-type{background:#d4edda;color:#155724}
.methods-config-item{padding:12px 14px;border-radius:.75rem;cursor:pointer;transition:background .15s;display:flex;align-items:center;gap:8px}
.methods-config-item:hover{background:#eff1f6}
.methods-config-item.active{background:#e3f0ff}
.methods-config-item .icon{font-size:16px}
.methods-config-item .name{font-size:14px;font-weight:500}
.editor-main{flex:1;display:flex;flex-direction:column;overflow:hidden}
.editor-header{display:flex;align-items:center;justify-content:space-between;padding:12px 20px;background:#fff;border-bottom:1px solid #eff1f6}
.editor-header .path{font-size:13px;color:#6e7991;font-family:monospace}
.editor-header .path b{color:#091026}
.save-btn{padding:8px 20px;border:none;border-radius:8px;background:#3567ff;color:#fff;font-size:14px;font-weight:600;cursor:pointer;transition:opacity .2s}
.save-btn:hover{opacity:.9}
.save-btn:disabled{opacity:.4;cursor:not-allowed}
.editor-body{flex:1;display:flex;overflow:hidden}
.editor-textarea{flex:1;border:none;outline:none;padding:16px 20px;font-family:"SF Mono","Menlo","Consolas",monospace;font-size:13px;line-height:1.6;background:#fff;color:#091026;resize:none;tab-size:2}
.editor-textarea:focus{background:#fcfcfe}
.status-bar{padding:6px 20px;background:#fff;border-top:1px solid #eff1f6;font-size:12px;color:#6e7991}
.status-bar .ok{color:#155724}
.status-bar .err{color:#c0392b}
.empty-state{flex:1;display:flex;align-items:center;justify-content:center;color:#a0a9bd;font-size:15px}
/* Method config panel */
.method-config{flex:1;overflow-y:auto;padding:24px;background:#fff}
.config-section{margin-bottom:28px}
.config-section h4{font-size:15px;color:#091026;margin-bottom:12px;padding-bottom:6px;border-bottom:1px solid #eff1f6}
.config-field{margin-bottom:14px}
.config-field label{display:block;font-size:13px;color:#6e7991;margin-bottom:5px;font-weight:500}
.config-field input,.config-field textarea{width:100%;padding:10px 14px;border:1.5px solid #eff1f6;border-radius:8px;font-size:14px;outline:none;transition:border-color .2s;font-family:inherit}
.config-field input:focus,.config-field textarea:focus{border-color:#3567ff}
.config-field textarea{min-height:60px;resize:vertical}
.config-field .hint{font-size:12px;color:#a0a9bd;margin-top:4px}
.skill-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(200px,1fr));gap:8px}
.skill-toggle{display:flex;align-items:center;justify-content:space-between;padding:8px 12px;border-radius:8px;border:1.5px solid #eff1f6;cursor:pointer;transition:all .15s}
.skill-toggle:hover{border-color:#3567ff}
.skill-toggle.assigned{background:#d4edda;border-color:#155724}
.skill-toggle .st-name{font-size:13px;font-weight:500}
.skill-toggle .st-type{font-size:10px;padding:2px 6px;border-radius:4px;background:#eff1f6;color:#6e7991}
.skill-toggle.assigned .st-type{background:#155724;color:#fff}
.skill-toggle .st-status{font-size:12px;font-weight:600;color:#6e7991}
.skill-toggle.assigned .st-status{color:#155724}
.input-field-card{border:1px solid #eff1f6;border-radius:10px;padding:14px;margin-bottom:10px;position:relative;background:#fcfcfe}
.input-field-card .if-row{display:flex;gap:10px;margin-bottom:8px}
.input-field-card .if-row:last-child{margin-bottom:0}
.input-field-card .if-group{flex:1}
.input-field-card .if-group label{display:block;font-size:11px;color:#6e7991;margin-bottom:3px}
.input-field-card .if-group input{width:100%;padding:7px 10px;border:1px solid #eff1f6;border-radius:6px;font-size:13px;outline:none}
.input-field-card .if-group input:focus{border-color:#3567ff}
.input-field-card .if-check{display:flex;align-items:center;gap:5px;font-size:12px;color:#6e7991;white-space:nowrap}
.input-field-card .if-check input{margin:0}
.if-delete{position:absolute;top:10px;right:10px;background:none;border:none;font-size:16px;color:#c0392b;cursor:pointer;padding:2px 6px;border-radius:4px}
.if-delete:hover{background:#fdf0ed}
.add-field-btn{display:flex;align-items:center;justify-content:center;gap:6px;padding:8px;border:1.5px dashed #a0a9bd;border-radius:8px;cursor:pointer;font-size:13px;color:#6e7991;transition:all .15s;width:100%}
.add-field-btn:hover{border-color:#3567ff;color:#3567ff}
.config-actions{display:flex;gap:12px;margin-top:20px;padding-top:16px;border-top:1px solid #eff1f6}
.btn-primary{padding:10px 24px;border:none;border-radius:8px;background:#3567ff;color:#fff;font-size:14px;font-weight:600;cursor:pointer;transition:opacity .2s}
.btn-primary:hover{opacity:.9}
.btn-danger{padding:10px 24px;border:1.5px solid #c0392b;border-radius:8px;background:#fff;color:#c0392b;font-size:14px;font-weight:600;cursor:pointer;transition:all .2s}
.btn-danger:hover{background:#c0392b;color:#fff}
/* New method dialog */
.dialog-overlay{position:fixed;top:0;left:0;width:100%;height:100%;background:rgba(0,0,0,0.3);display:flex;align-items:center;justify-content:center;z-index:1000}
.dialog-box{background:#fff;border-radius:16px;padding:28px;width:400px;max-width:90vw;box-shadow:0 8px 32px rgba(0,0,0,0.15)}
.dialog-box h3{font-size:18px;margin-bottom:16px}
.dialog-box .config-field{margin-bottom:14px}
.dialog-box .dialog-actions{display:flex;gap:12px;justify-content:flex-end;margin-top:20px}

/* 列表卡片（模板规格） */
.report-item,.history-item{min-height:80px;box-sizing:border-box}
.report-list,.history-list,.conv-list,.issue-list,.check-list{display:flex;flex-direction:column;gap:16px}
.report-item,.history-item,.method-nav-item,.file-item,.conv-item,.methods-config-item,.issue-card{background:#fff;border:1px solid #eff1f6;border-radius:1rem;box-shadow:0 .0625rem .0625rem #0b14371a,0 .25rem .25rem #99beff1a;padding:16px 20px;transition:transform .2s,box-shadow .2s,background .2s}
.report-item,.history-item{cursor:pointer}
.report-item:hover,.history-item:hover{transform:translateY(-2px);box-shadow:0 .5rem 1rem #0b14371f}
.method-nav-item:hover,.file-item:hover,.conv-item:hover,.methods-config-item:hover{background:#f4f5ff;border-color:#dce1eb}
.method-nav-item.active,.report-item.active{background:#f4f5ff;border-color:#3567ff}

#methodSelector,#skillList,#refList{display:flex;flex-direction:column;gap:16px}
</style>
</head>
<body>
<nav class="topnav">
  <a href="/research" class="brand-logo"><img src="/brand/logo.png" alt="飞柚 GEO 品牌标识"></a>
  <div class="nav-dropdown">
    <div class="nav-dropdown-toggle"><span>分析</span>
      <svg class="nav-arrow" viewBox="0 0 20 20" fill="none"><path d="M15.5 7.03835L10 12.9614L4.5 7.03835" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"/></svg>
    </div>
    <div class="nav-dropdown-menu">
      <a href="/mining">GEO搜索问句挖掘</a>
      <a href="/research">客户市场调研</a>
    </div>
  </div>
  <a href="/audit">官网诊断</a>
  <a href="/chat">对话</a>
  <div class="nav-dropdown">
    <div class="nav-dropdown-toggle"><span>配置管理</span>
      <svg class="nav-arrow" viewBox="0 0 20 20" fill="none"><path d="M15.5 7.03835L10 12.9614L4.5 7.03835" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"/></svg>
    </div>
    <div class="nav-dropdown-menu">
      <a href="/skills" class="active">Skill管理</a>
      <a href="/company">企业资料</a>
      <a href="/oplog">操作日志</a>
      <a href="/company">企业资料</a>
      <a href="/backfill">JSON补录</a>
      <a href="/changelog">__ver__</a>
    </div>
  </div>
<!--标注汇总入口暂时隐藏-->
</nav>
<div class="editor-layout">
  <div class="sidebar">
    <h3>分析方法</h3>
    <div id="methodSelector"></div>
    <div class="add-method-btn" onclick="showNewMethodDialog()">+ 新增分析方法</div>
    <h3>分析方法配置(Raw)</h3>
    <div class="methods-config-item" id="methodsConfigItem" onclick="loadMethodsRaw()">
      <span class="icon">&#9881;</span>
      <span class="name">methods.yaml</span>
    </div>
    <h3>Skill 文件</h3>
    <div id="skillList"></div>
    <h3>参考数据</h3>
    <div id="refList"></div>
  </div>
  <div class="editor-main">
    <div class="editor-header" id="editorHeader">
      <div class="path" id="filePath">选择左侧文件或分析方法</div>
      <button class="save-btn" id="saveBtn" onclick="saveFile()" disabled>保存</button>
    </div>
    <div class="editor-body" id="editorBody">
      <div class="empty-state" id="emptyState">点击左侧分析方法进行配置,或点击Skill文件编辑YAML</div>
    </div>
    <div class="status-bar" id="statusBar"></div>
  </div>
</div>
<script>
let currentFile = null;
let currentType = null;
let currentView = null;
let methodsData = {methods: [], all_skills: []};
let currentMethodId = null;
let workingSkills = [];
let workingFields = [];

async function loadAll() {
  try {
    const [methodsResp, skillsResp, refsResp] = await Promise.all([
      fetch("/api/methods"), fetch("/api/skills"), fetch("/api/refs")
    ]);
    methodsData = await methodsResp.json();
    const skills = await skillsResp.json();
    const refs = await refsResp.json();

    renderMethodSelector();
    renderSkillList(skills.skills);

    const rl = document.getElementById("refList");
    rl.innerHTML = refs.references.map(r =>
      '<div class="file-item" onclick="loadFile(\\'refs\\', \\'' + r.filename + '\\')">' +
        '<span class="file-name">' + r.filename + '</span>' +
        '<span class="file-type">' + r.size_kb + 'KB</span>' +
      '</div>'
    ).join("");
  } catch(e) {
    document.getElementById("methodSelector").innerHTML = '<div style="color:#c0392b;padding:8px">加载失败</div>';
  }
}

function renderMethodSelector() {
  const c = document.getElementById("methodSelector");
  c.innerHTML = methodsData.methods.map(m =>
    '<div class="method-nav-item' + (m.id === currentMethodId && currentView === "method_config" ? " active" : "") + '" onclick="selectMethod(\\'' + m.id + '\\')">' + m.name + '</div>'
  ).join("");
}

function renderSkillList(skills) {
  const sl = document.getElementById("skillList");
  sl.innerHTML = skills.map(s =>
    '<div class="file-item" onclick="loadFile(\\'skills\\', \\'' + s.filename + '\\')">' +
      '<span class="file-num">' + s.order + '</span>' +
      '<span class="file-name">' + s.name + '</span>' +
      '<span class="file-type">' + s.type + '</span>' +
    '</div>'
  ).join("");
}

function selectMethod(methodId) {
  currentMethodId = methodId;
  currentView = "method_config";
  currentFile = null;
  currentType = null;
  renderMethodSelector();
  renderMethodConfig();
  document.querySelectorAll(".file-item, .methods-config-item").forEach(el => el.classList.remove("active"));
}

function renderMethodConfig() {
  const method = methodsData.methods.find(m => m.id === currentMethodId);
  if (!method) return;
  workingSkills = [...(method.skills || [])];
  workingFields = JSON.parse(JSON.stringify(method.input_fields || []));

  const body = document.getElementById("editorBody");
  document.getElementById("editorHeader").style.display = "none";
  body.innerHTML = '<div class="method-config" id="methodConfigPanel"></div>';
  const panel = document.getElementById("methodConfigPanel");

  let html = '<div class="config-section"><h4>基本信息</h4>';
  html += '<div class="config-field"><label>方法ID</label><input type="text" value="' + method.id + '" disabled style="background:#ffffff"></div>';
  html += '<div class="config-field"><label>方法名称</label><input type="text" id="cfgName" value="' + escapeHtml(method.name) + '"></div>';
  html += '<div class="config-field"><label>描述</label><textarea id="cfgDesc">' + escapeHtml(method.description || "") + '</textarea></div>';
  html += '</div>';

  html += '<div class="config-section"><h4>Skill配置 — 点击切换加入/移除</h4><div class="skill-grid" id="skillGrid"></div></div>';

  html += '<div class="config-section"><h4>输入字段配置</h4><div id="fieldsList"></div>';
  html += '<button class="add-field-btn" onclick="addInputField()">+ 添加输入字段</button></div>';

  html += '<div class="config-actions">';
  html += '<button class="btn-primary" onclick="saveMethodConfig()">保存配置</button>';
  html += '<button class="btn-danger" onclick="deleteMethod()">删除此方法</button>';
  html += '</div>';

  panel.innerHTML = html;
  renderSkillGrid();
  renderFieldsList();
}

function renderSkillGrid() {
  const grid = document.getElementById("skillGrid");
  if (!grid) return;
  grid.innerHTML = methodsData.all_skills.map(s => {
    const assigned = workingSkills.includes(s.id);
    return '<div class="skill-toggle' + (assigned ? " assigned" : "") + '" onclick="toggleSkill(\\'' + s.id + '\\')">' +
      '<div><div class="st-name">' + s.name + '</div><div class="st-type">' + s.type + '</div></div>' +
      '<div class="st-status">' + (assigned ? "已加入" : "未加入") + '</div>' +
    '</div>';
  }).join("");
}

function toggleSkill(skillId) {
  const idx = workingSkills.indexOf(skillId);
  if (idx >= 0) {
    workingSkills.splice(idx, 1);
  } else {
    workingSkills.push(skillId);
  }
  renderSkillGrid();
}

function renderFieldsList() {
  const list = document.getElementById("fieldsList");
  if (!list) return;
  list.innerHTML = workingFields.map((f, i) => {
    return '<div class="input-field-card">' +
      '<button class="if-delete" onclick="removeInputField(' + i + ')">&times;</button>' +
      '<div class="if-row">' +
        '<div class="if-group"><label>字段名(name)</label><input type="text" value="' + escapeAttr(f.name||"") + '" onchange="updateField(' + i + ',\\'name\\',this.value)"></div>' +
        '<div class="if-group"><label>显示标签(label)</label><input type="text" value="' + escapeAttr(f.label||"") + '" onchange="updateField(' + i + ',\\'label\\',this.value)"></div>' +
      '</div>' +
      '<div class="if-row">' +
        '<div class="if-group"><label>占位提示(placeholder)</label><input type="text" value="' + escapeAttr(f.placeholder||"") + '" onchange="updateField(' + i + ',\\'placeholder\\',this.value)"></div>' +
        '<div class="if-check"><input type="checkbox" ' + (f.required ? "checked" : "") + ' onchange="updateField(' + i + ',\\'required\\',this.checked)">必填</div>' +
      '</div>' +
    '</div>';
  }).join("");
}

function updateField(idx, key, val) {
  if (workingFields[idx]) workingFields[idx][key] = val;
}

function addInputField() {
  workingFields.push({name: "", label: "", required: false, placeholder: ""});
  renderFieldsList();
}

function removeInputField(idx) {
  workingFields.splice(idx, 1);
  renderFieldsList();
}

async function saveMethodConfig() {
  const name = document.getElementById("cfgName").value.trim();
  const desc = document.getElementById("cfgDesc").value.trim();
  if (!name) { showStatus("方法名称不能为空", "err"); return; }
  const validFields = workingFields.filter(f => f.name && f.name.trim());
  try {
    const resp = await fetch("/api/methods/" + encodeURIComponent(currentMethodId), {
      method: "PUT",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify({
        name: name,
        description: desc,
        skills: workingSkills,
        input_fields: validFields.map(f => ({
          name: f.name.trim(),
          label: (f.label||"").trim(),
          required: !!f.required,
          placeholder: (f.placeholder||"").trim()
        }))
      })
    });
    const data = await resp.json();
    if (data.error) {
      showStatus("保存失败: " + data.error, "err");
    } else {
      showStatus("✓ 保存成功 — 下次分析自动生效", "ok");
      await loadAll();
      renderMethodConfig();
    }
  } catch(e) {
    showStatus("保存失败: " + e.message, "err");
  }
}

async function deleteMethod() {
  if (!confirm("确定删除分析方法?此操作不可撤销")) return;
  try {
    const resp = await fetch("/api/methods/" + encodeURIComponent(currentMethodId), {method: "DELETE"});
    const data = await resp.json();
    if (data.error) {
      showStatus("删除失败: " + data.error, "err");
    } else {
      showStatus("已删除", "ok");
      currentMethodId = null;
      currentView = null;
      await loadAll();
      showEmptyState();
    }
  } catch(e) {
    showStatus("删除失败: " + e.message, "err");
  }
}

function showNewMethodDialog() {
  const overlay = document.createElement("div");
  overlay.className = "dialog-overlay";
  overlay.id = "newMethodDialog";
  overlay.innerHTML = '<div class="dialog-box">' +
    '<h3>新增分析方法</h3>' +
    '<div class="config-field"><label>方法ID (英文,唯一)</label><input type="text" id="newMethodId" placeholder="如: brand_sentiment"></div>' +
    '<div class="config-field"><label>方法名称</label><input type="text" id="newMethodName" placeholder="如: 品牌口碑分析"></div>' +
    '<div class="config-field"><label>描述 (可选)</label><textarea id="newMethodDesc" placeholder="简要描述"></textarea></div>' +
    '<div class="dialog-actions">' +
      '<button class="btn-danger" style="border-color:#eff1f6;color:#6e7991" onclick="closeNewMethodDialog()">取消</button>' +
      '<button class="btn-primary" onclick="createMethod()">创建</button>' +
    '</div></div>';
  document.body.appendChild(overlay);
  document.getElementById("newMethodId").focus();
}

function closeNewMethodDialog() {
  const d = document.getElementById("newMethodDialog");
  if (d) d.remove();
}

async function createMethod() {
  const id = document.getElementById("newMethodId").value.trim();
  const name = document.getElementById("newMethodName").value.trim();
  const desc = document.getElementById("newMethodDesc").value.trim();
  if (!id || !name) { alert("请填写方法ID和名称"); return; }
  if (!/^[a-z0-9_]+$/.test(id)) { alert("方法ID只能用小写英文、数字和下划线"); return; }
  try {
    const resp = await fetch("/api/methods", {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify({id: id, name: name, description: desc})
    });
    const data = await resp.json();
    if (data.error) {
      alert("创建失败: " + data.error);
    } else {
      closeNewMethodDialog();
      await loadAll();
      selectMethod(id);
      showStatus("已创建新方法,请配置Skill和输入字段", "ok");
    }
  } catch(e) {
    alert("创建失败: " + e.message);
  }
}

function showEmptyState() {
  document.getElementById("editorHeader").style.display = "none";
  document.getElementById("editorBody").innerHTML = '<div class="empty-state">点击左侧分析方法进行配置,或点击Skill文件编辑YAML</div>';
}

async function loadMethodsRaw() {
  currentView = "file_editor";
  currentFile = "methods.yaml";
  currentType = "methods";
  const resp = await fetch("/api/methods");
  const data = await resp.json();
  if (data.error || !data.raw_content) {
    showStatus("加载失败", "err");
    return;
  }
  document.getElementById("editorHeader").style.display = "flex";
  document.getElementById("filePath").innerHTML = '<b>methods.yaml</b>';
  document.getElementById("saveBtn").disabled = false;
  document.getElementById("editorBody").innerHTML =
    '<textarea class="editor-textarea" id="editor" spellcheck="false"></textarea>';
  document.getElementById("editor").value = data.raw_content;
  showStatus("已加载 methods.yaml", "ok");
  document.querySelectorAll(".file-item, .methods-config-item, .method-nav-item").forEach(el => el.classList.remove("active"));
  document.getElementById("methodsConfigItem").classList.add("active");
}

async function loadFile(type, filename) {
  currentView = "file_editor";
  currentFile = filename;
  currentType = type;
  currentMethodId = null;
  const resp = await fetch("/api/" + type + "/" + filename);
  const data = await resp.json();
  if (data.error) {
    showStatus("加载失败: " + data.error, "err");
    return;
  }
  document.getElementById("editorHeader").style.display = "flex";
  document.getElementById("filePath").innerHTML = '<b>' + (type === "skills" ? "skills/" : "references/") + "</b>" + filename;
  document.getElementById("saveBtn").disabled = false;
  document.getElementById("editorBody").innerHTML =
    '<textarea class="editor-textarea" id="editor" spellcheck="false"></textarea>';
  document.getElementById("editor").value = data.content;
  showStatus("已加载 " + filename, "ok");
  document.querySelectorAll(".file-item, .methods-config-item, .method-nav-item").forEach(el => el.classList.remove("active"));
  event.target.closest(".file-item").classList.add("active");
  renderMethodSelector();
}

async function saveFile() {
  if (currentView !== "file_editor" || !currentFile) return;
  const btn = document.getElementById("saveBtn");
  btn.disabled = true;
  btn.textContent = "保存中...";
  const content = document.getElementById("editor").value;
  try {
    let url;
    if (currentType === "methods") {
      url = "/api/methods";
    } else {
      url = "/api/" + currentType + "/" + currentFile;
    }
    const resp = await fetch(url, {
      method: "PUT",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify({content: content})
    });
    const data = await resp.json();
    if (data.error) {
      showStatus("保存失败: " + data.error, "err");
    } else {
      showStatus("✓ 保存成功 — 下次分析自动生效", "ok");
      if (currentType === "methods") { await loadAll(); }
    }
  } catch(e) {
    showStatus("保存失败: " + e.message, "err");
  }
  btn.disabled = false;
  btn.textContent = "保存";
}

function showStatus(msg, cls) {
  const bar = document.getElementById("statusBar");
  bar.innerHTML = '<span class="' + cls + '">' + msg + '</span>';
}

function escapeHtml(s) {
  return (s||"").replace(/&/g,"&amp;").replace(/</g,"&lt;").replace(/>/g,"&gt;");
}
function escapeAttr(s) {
  return (s||"").replace(/"/g,"&quot;").replace(/'/g,"&#39;");
}

document.addEventListener("keydown", function(e) {
  if ((e.metaKey || e.ctrlKey) && e.key === "s") {
    if (currentView === "method_config") {
      e.preventDefault();
      saveMethodConfig();
    } else if (currentView === "file_editor") {
      e.preventDefault();
      saveFile();
    }
  }
});

loadAll();


// 二级菜单：悬停开合 + 移出宽限 250ms
(function(){
  document.querySelectorAll('.nav-dropdown').forEach(function(dd){
    var t=null;
    function open(){clearTimeout(t);dd.classList.add('open');}
    function close(){clearTimeout(t);t=setTimeout(function(){dd.classList.remove('open');},250);}
    dd.addEventListener('mouseenter',open);dd.addEventListener('mouseleave',close);
    dd.addEventListener('focusin',open);dd.addEventListener('focusout',close);
    dd.querySelector('.nav-dropdown-toggle').addEventListener('click',function(e){e.preventDefault();dd.classList.toggle('open');});
  });
})();
// 列表卡片滚动进场
(function(){
  var els=document.querySelectorAll('.report-item,.history-item,.issue-card,.method-nav-item,.file-item,.conv-item,.methods-config-item');
  if(!('IntersectionObserver' in window)||!els.length)return;
  var io=new IntersectionObserver(function(es){es.forEach(function(en){if(en.isIntersecting){en.target.style.transition='opacity .5s ease,transform .5s ease';en.target.style.opacity='1';en.target.style.transform='translateY(0)';io.unobserve(en.target);}});},{threshold:0.08});
  els.forEach(function(el){el.style.opacity='0';el.style.transform='translateY(5%)';io.observe(el);});
})();
</script>
</body>
</html>'''


@app.get("/skills", response_class=HTMLResponse)
async def skills_ui():
    return skills_ui_html().replace('__ver__', VER_LABEL)


# ── Chat UI ─────────────────────────────────────────

def chat_ui_html() -> str:
    return '''<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>对话 — GEO Agent</title>
<style>
*{margin:0;padding:0;box-sizing:border-box}
body{font-family:Figtree,-apple-system,"PingFang SC","Microsoft YaHei",sans-serif;background:#ffffff;color:#091026;line-height:1.6}
.topnav{display:flex;gap:24px;justify-content:flex-end;align-items:center;padding:24px;max-width:1200px;margin:0 auto;background:#fff;position:relative;box-sizing:border-box}
.topnav a{padding:0;border:none;background:transparent;font-size:16px;font-weight:400;line-height:20px;text-decoration:none;color:#6e7991;transition:color .2s;font-family:inherit}
.topnav a:hover{color:#091026}
.topnav a.active{color:#091026;font-weight:600}
.topnav a.brand-logo{position:absolute;left:24px;top:50%;transform:translateY(-50%);display:flex;align-items:center;text-decoration:none}
.brand-logo img{height:28px;width:auto;display:block}
.nav-dropdown{position:relative}
.nav-dropdown-toggle{display:flex;align-items:center;gap:6px;font-size:16px;font-weight:400;color:#6e7991;cursor:pointer;transition:color .2s}
.nav-dropdown-toggle.active{color:#091026;font-weight:600}
.nav-dropdown:hover .nav-dropdown-toggle{color:#091026}
.nav-arrow{width:1rem;height:1rem;transition:transform .2s}
.nav-dropdown.open .nav-arrow{transform:rotate(180deg)}
.nav-dropdown-menu{position:absolute;top:100%;right:0;margin-top:12px;padding:8px;background:#fff;border:1px solid #eff1f6;border-radius:1rem;box-shadow:0 .0625rem .0625rem #0b14371a,0 .25rem .25rem #99beff1a;display:none;z-index:50}
.nav-dropdown.open .nav-dropdown-menu{display:block}
.nav-dropdown:hover .nav-dropdown-menu{display:block}
.nav-dropdown-menu a{display:block;padding:8px 12px;border-radius:.5rem;font-size:16px;color:#6e7991;text-decoration:none;white-space:nowrap;transition:all .15s}
.nav-dropdown-menu a:hover{background:#f4f5ff;color:#091026}
.nav-dropdown::after{content:"";position:absolute;left:0;right:0;top:100%;height:12px}
.topnav-OLD{display:none}
.chat-container{display:flex;height:calc(100vh - 50px);max-width:1200px;margin:0 auto;box-sizing:border-box}
.sidebar{width:240px;background:#fff;border-right:1px solid #eff1f6;display:flex;flex-direction:column}
.sidebar-header{padding:12px;border-bottom:1px solid #eff1f6}
.new-chat-btn{width:100%;padding:10px;border:1.5px solid #3567ff;border-radius:10px;background:#fff;color:#3567ff;font-size:14px;font-weight:600;cursor:pointer;transition:all .2s}
.new-chat-btn:hover{background:#3567ff;color:#fff}
.conv-list{flex:1;overflow-y:auto;padding:8px}
.conv-list h3{font-size:12px;color:#a0a9bd;margin:8px 8px 4px;text-transform:uppercase}
.conv-item{padding:10px 12px;border-radius:8px;cursor:pointer;transition:background .15s;margin-bottom:4px}
.conv-item:hover{background:#eff1f6}
.conv-item.active{background:#e3f0ff}
.conv-item .preview{font-size:13px;color:#091026;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.conv-item .time{font-size:11px;color:#a0a9bd;margin-top:2px}
.chat-main{flex:1;display:flex;flex-direction:column;overflow:hidden}
.chat-header{padding:8px 24px;border-bottom:1px solid #eff1f6}
.chat-header h2{font-size:16px;color:#091026}
.messages-area{flex:1;overflow-y:auto;padding:16px 24px;max-width:800px;margin:0 auto;width:100%}
.message{display:flex;margin-bottom:12px}
.message.user{justify-content:flex-end}
.message.agent{justify-content:flex-start}
.bubble{max-width:75%;padding:10px 16px;border-radius:16px;font-size:14px;white-space:pre-wrap;word-break:break-word}
.message.user .bubble{background:#3567ff;color:#fff}
.message.agent .bubble{background:#fff;color:#091026;box-shadow:0 1px 4px rgba(0,0,0,0.06)}
.tool-event{font-size:13px;color:#6e7991;padding:4px 12px;margin:4px 0;border-radius:8px;background:#eff1f6}
.tool-event .tool-status{font-weight:600;margin-right:6px}
.thinking{display:flex;gap:4px;padding:12px 16px}
.thinking .dot{width:8px;height:8px;border-radius:50%;background:#a0a9bd;animation:bounce 1.4s infinite ease-in-out}
.thinking .dot:nth-child(1){animation-delay:-.32s}
.thinking .dot:nth-child(2){animation-delay:-.16s}
@keyframes bounce{0%,80%,100%{transform:scale(0)}40%{transform:scale(1)}}
.input-area{padding:12px 24px;background:#fff;border-top:1px solid #eff1f6;display:flex;gap:12px;max-width:800px;margin:0 auto;width:100%}
.input-area textarea{flex:1;resize:none;border:1.5px solid #eff1f6;border-radius:10px;padding:10px 14px;font-size:14px;outline:none;font-family:inherit;max-height:120px;transition:border-color .2s}
.input-area textarea:focus{border-color:#3567ff}
.send-btn{padding:10px 20px;border:none;border-radius:10px;background:#3567ff;color:#fff;font-size:14px;font-weight:600;cursor:pointer;transition:opacity .2s}
.send-btn:hover{opacity:.9}
.send-btn:disabled{opacity:.4;cursor:not-allowed}
.welcome{text-align:center;padding:60px 24px;color:#a0a9bd}
.welcome h3{font-size:18px;margin-bottom:8px;color:#6e7991}
.welcome p{font-size:14px}
.welcome .examples{margin-top:20px;display:flex;flex-direction:column;gap:8px;align-items:center}
.welcome .example{padding:8px 16px;background:#fff;border-radius:20px;font-size:13px;color:#6e7991;cursor:pointer;transition:all .2s;box-shadow:0 1px 4px rgba(0,0,0,0.06)}
.welcome .example:hover{color:#3567ff}

/* 列表卡片（模板规格） */
.report-item,.history-item{min-height:80px;box-sizing:border-box}
.report-list,.history-list,.conv-list,.issue-list,.check-list{display:flex;flex-direction:column;gap:16px}
.report-item,.history-item,.method-nav-item,.file-item,.conv-item,.methods-config-item,.issue-card{background:#fff;border:1px solid #eff1f6;border-radius:1rem;box-shadow:0 .0625rem .0625rem #0b14371a,0 .25rem .25rem #99beff1a;padding:16px 20px;transition:transform .2s,box-shadow .2s,background .2s}
.report-item,.history-item{cursor:pointer}
.report-item:hover,.history-item:hover{transform:translateY(-2px);box-shadow:0 .5rem 1rem #0b14371f}
.method-nav-item:hover,.file-item:hover,.conv-item:hover,.methods-config-item:hover{background:#f4f5ff;border-color:#dce1eb}
.method-nav-item.active,.report-item.active{background:#f4f5ff;border-color:#3567ff}
</style>
</head>
<body>
<nav class="topnav">
  <a href="/research" class="brand-logo"><img src="/brand/logo.png" alt="飞柚 GEO 品牌标识"></a>
  <div class="nav-dropdown">
    <div class="nav-dropdown-toggle"><span>分析</span>
      <svg class="nav-arrow" viewBox="0 0 20 20" fill="none"><path d="M15.5 7.03835L10 12.9614L4.5 7.03835" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"/></svg>
    </div>
    <div class="nav-dropdown-menu">
      <a href="/mining">GEO搜索问句挖掘</a>
      <a href="/research">客户市场调研</a>
    </div>
  </div>
  <a href="/audit">官网诊断</a>
  <a href="/chat" class="active">对话</a>
  <div class="nav-dropdown">
    <div class="nav-dropdown-toggle"><span>配置管理</span>
      <svg class="nav-arrow" viewBox="0 0 20 20" fill="none"><path d="M15.5 7.03835L10 12.9614L4.5 7.03835" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"/></svg>
    </div>
    <div class="nav-dropdown-menu">
      <a href="/skills">Skill管理</a>
      <a href="/company">企业资料</a>
      <a href="/oplog">操作日志</a>
      <a href="/backfill">JSON补录</a>
      <a href="/changelog">__ver__</a>
    </div>
  </div>
<!--标注汇总入口暂时隐藏-->
</nav>
<div class="chat-container">
  <div class="sidebar">
    <div class="sidebar-header">
      <button class="new-chat-btn" onclick="newChat()">+ 新对话</button>
    </div>
    <div class="conv-list" id="convList">
      <h3>历史对话</h3>
      <div style="color:#a0a9bd;font-size:13px;padding:8px">加载中...</div>
    </div>
  </div>
  <div class="chat-main">
    <div class="chat-header"><h2>Agent 对话</h2></div>
    <div class="messages-area" id="messages">
      <div class="welcome" id="welcome">
        <h3>GEO搜索问句挖掘Agent</h3>
        <p>我可以对话、调用skill、管理配置、查看报告</p>
        <div class="examples">
          <div class="example" onclick="quickSend('分析好莱客的搜索问句')">分析好莱客的搜索问句</div>
          <div class="example" onclick="quickSend('看看step2的配置')">看看step2的配置</div>
          <div class="example" onclick="quickSend('有哪些历史报告？')">有哪些历史报告？</div>
        </div>
      </div>
    </div>
    <div class="input-area">
      <textarea id="messageInput" rows="1" placeholder="输入消息,Enter发送,Shift+Enter换行" onkeydown="handleKey(event)"></textarea>
      <button class="send-btn" id="sendBtn" onclick="sendMessage()">发送</button>
    </div>
  </div>
</div>
<script>
let sessionId = localStorage.getItem("geo_session_id") || ("s_" + Date.now());
localStorage.setItem("geo_session_id", sessionId);
let isProcessing = false;

function handleKey(e){if(e.key==="Enter"&&!e.shiftKey){e.preventDefault();sendMessage()}}
function quickSend(msg){document.getElementById("messageInput").value=msg;sendMessage()}
function escapeHtml(t){const d=document.createElement("div");d.textContent=t;return d.innerHTML}

function addUserMessage(text){
  const m=document.getElementById("messages");
  const d=document.createElement("div");d.className="message user";
  d.innerHTML='<div class="bubble">'+escapeHtml(text)+'</div>';
  m.appendChild(d);m.scrollTop=m.scrollHeight;
}
function addAgentMessage(text){
  const m=document.getElementById("messages");
  const d=document.createElement("div");d.className="message agent";
  d.innerHTML='<div class="bubble">'+escapeHtml(text)+'</div>';
  m.appendChild(d);m.scrollTop=m.scrollHeight;
}
function addToolEvent(status,text){
  const m=document.getElementById("messages");
  const d=document.createElement("div");d.className="tool-event";
  d.innerHTML='<span class="tool-status">'+(status==="start"?"⚡":"✓")+"</span>"+escapeHtml(text||"");
  m.appendChild(d);m.scrollTop=m.scrollHeight;
}
function addThinking(){
  const m=document.getElementById("messages");
  const d=document.createElement("div");d.className="thinking";d.id="thinking";
  d.innerHTML='<div class="dot"></div><div class="dot"></div><div class="dot"></div>';
  m.appendChild(d);m.scrollTop=m.scrollHeight;
}
function removeThinking(){const e=document.getElementById("thinking");if(e)e.remove()}

function showWelcome(){
  const m=document.getElementById("messages");
  m.innerHTML='<div class="welcome" id="welcome"><h3>GEO搜索问句挖掘Agent</h3><p>我可以对话、调用skill、管理配置、查看报告</p><div class="examples"><div class="example" onclick="quickSend(\\'分析好莱客的搜索问句\\')">分析好莱客的搜索问句</div><div class="example" onclick="quickSend(\\'看看step2的配置\\')">看看step2的配置</div><div class="example" onclick="quickSend(\\'有哪些历史报告？\\')">有哪些历史报告？</div></div></div>';
}

async function loadHistory(){
  try{
    const resp=await fetch("/api/chat/"+sessionId);
    if(!resp.ok){return}
    const data=await resp.json();
    const display=data.display||[];
    if(display.length===0){return}
    document.getElementById("welcome")?.remove();
    display.forEach(function(msg){
      if(msg.role==="user")addUserMessage(msg.content);
      else if(msg.role==="agent")addAgentMessage(msg.content);
      else if(msg.role==="tool_event")addToolEvent(msg.status,msg.text);
    });
  }catch(e){}
}

async function loadConvList(){
  try{
    const resp=await fetch("/api/chat");
    const data=await resp.json();
    const list=document.getElementById("convList");
    const sessions=data.sessions||[];
    if(sessions.length===0){
      list.innerHTML='<h3>历史对话</h3><div style="color:#a0a9bd;font-size:13px;padding:8px">暂无</div>';
      return;
    }
    list.innerHTML='<h3>历史对话</h3>'+sessions.map(function(s){
      const cls=s.session_id===sessionId?"conv-item active":"conv-item";
      const time=s.updated_at?s.updated_at.slice(5,16):"";
      return '<div class="'+cls+'" onclick="switchSession(\\''+s.session_id+'\\')">'+
        '<div class="preview">'+escapeHtml(s.preview||"(空对话)")+'</div>'+
        '<div class="time">'+time+'</div></div>';
    }).join("");
  }catch(e){}
}

function switchSession(sid){
  sessionId=sid;
  localStorage.setItem("geo_session_id",sid);
  location.reload();
}

async function newChat(){
  sessionId="s_"+Date.now();
  localStorage.setItem("geo_session_id",sessionId);
  showWelcome();
  loadConvList();
}

async function sendMessage(){
  const input=document.getElementById("messageInput");
  const message=input.value.trim();
  if(!message||isProcessing)return;
  isProcessing=true;
  const btn=document.getElementById("sendBtn");
  btn.disabled=true;
  input.value="";input.style.height="";
  document.getElementById("welcome")?.remove();
  addUserMessage(message);
  addThinking();
  try{
    const resp=await fetch("/api/chat",{
      method:"POST",
      headers:{"Content-Type":"application/json"},
      body:JSON.stringify({message:message,session_id:sessionId})
    });
    const reader=resp.body.getReader();
    const decoder=new TextDecoder();
    let buffer="";
    while(true){
      const{done,value}=await reader.read();
      if(done)break;
      buffer+=decoder.decode(value,{stream:true});
      const parts=buffer.split("\\n\\n");
      for(let i=0;i<parts.length-1;i++){
        const line=parts[i].trim();
        if(line.startsWith("data: ")){
          try{const data=JSON.parse(line.slice(6));handleEvent(data)}catch(e){}
        }
      }
      buffer=parts[parts.length-1];
    }
  }catch(e){
    removeThinking();
    addAgentMessage("连接失败: "+e.message);
  }
  isProcessing=false;
  btn.disabled=false;
  loadConvList();
}

function handleEvent(data){
  switch(data.type){
    case"thinking":break;
    case"tool_start":removeThinking();addToolEvent("start","正在执行: "+(data.tool_desc||data.tool_name));addThinking();break;
    case"tool_end":removeThinking();addToolEvent("end",data.summary||"完成");addThinking();break;
    case"text":removeThinking();addAgentMessage(data.content);break;
    case"done":removeThinking();break;
    case"error":removeThinking();addAgentMessage("错误: "+data.message);break;
  }
}

const ta=document.getElementById("messageInput");
ta.addEventListener("input",function(){this.style.height="auto";this.style.height=Math.min(this.scrollHeight,120)+"px"});

loadHistory();
loadConvList();


// 二级菜单：悬停开合 + 移出宽限 250ms
(function(){
  document.querySelectorAll('.nav-dropdown').forEach(function(dd){
    var t=null;
    function open(){clearTimeout(t);dd.classList.add('open');}
    function close(){clearTimeout(t);t=setTimeout(function(){dd.classList.remove('open');},250);}
    dd.addEventListener('mouseenter',open);dd.addEventListener('mouseleave',close);
    dd.addEventListener('focusin',open);dd.addEventListener('focusout',close);
    dd.querySelector('.nav-dropdown-toggle').addEventListener('click',function(e){e.preventDefault();dd.classList.toggle('open');});
  });
})();
// 列表卡片滚动进场
(function(){
  var els=document.querySelectorAll('.report-item,.history-item,.issue-card,.method-nav-item,.file-item,.conv-item,.methods-config-item');
  if(!('IntersectionObserver' in window)||!els.length)return;
  var io=new IntersectionObserver(function(es){es.forEach(function(en){if(en.isIntersecting){en.target.style.transition='opacity .5s ease,transform .5s ease';en.target.style.opacity='1';en.target.style.transform='translateY(0)';io.unobserve(en.target);}});},{threshold:0.08});
  els.forEach(function(el){el.style.opacity='0';el.style.transform='translateY(5%)';io.observe(el);});
})();
</script>
</body>
</html>'''


@app.get("/chat", response_class=HTMLResponse)
async def chat_ui():
    return chat_ui_html().replace('__ver__', VER_LABEL)


# ── Annotations Summary UI ─────────────────────────

def annotations_ui_html() -> str:
    return '''<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>标注汇总 — GEO Agent</title>
<style>
*{margin:0;padding:0;box-sizing:border-box}
body{font-family:Figtree,-apple-system,"PingFang SC","Microsoft YaHei",sans-serif;background:#ffffff;color:#091026;line-height:1.6}
.topnav{display:flex;gap:24px;justify-content:flex-end;align-items:center;padding:24px;max-width:1200px;margin:0 auto;background:#fff;position:relative;box-sizing:border-box}
.topnav a{padding:0;border:none;background:transparent;font-size:16px;font-weight:400;line-height:20px;text-decoration:none;color:#6e7991;transition:color .2s;font-family:inherit}
.topnav a:hover{color:#091026}
.topnav a.active{color:#091026;font-weight:600}
.topnav a.brand-logo{position:absolute;left:24px;top:50%;transform:translateY(-50%);display:flex;align-items:center;text-decoration:none}
.brand-logo img{height:28px;width:auto;display:block}
.nav-dropdown{position:relative}
.nav-dropdown-toggle{display:flex;align-items:center;gap:6px;font-size:16px;font-weight:400;color:#6e7991;cursor:pointer;transition:color .2s}
.nav-dropdown-toggle.active{color:#091026;font-weight:600}
.nav-dropdown:hover .nav-dropdown-toggle{color:#091026}
.nav-arrow{width:1rem;height:1rem;transition:transform .2s}
.nav-dropdown.open .nav-arrow{transform:rotate(180deg)}
.nav-dropdown-menu{position:absolute;top:100%;right:0;margin-top:12px;padding:8px;background:#fff;border:1px solid #eff1f6;border-radius:1rem;box-shadow:0 .0625rem .0625rem #0b14371a,0 .25rem .25rem #99beff1a;display:none;z-index:50}
.nav-dropdown.open .nav-dropdown-menu{display:block}
.nav-dropdown:hover .nav-dropdown-menu{display:block}
.nav-dropdown-menu a{display:block;padding:8px 12px;border-radius:.5rem;font-size:16px;color:#6e7991;text-decoration:none;white-space:nowrap;transition:all .15s}
.nav-dropdown-menu a:hover{background:#f4f5ff;color:#091026}
.nav-dropdown::after{content:"";position:absolute;left:0;right:0;top:100%;height:12px}
.topnav-OLD{display:none}
.app{max-width:1200px;margin:0 auto;padding:24px 24px 40px;box-sizing:border-box}
header{margin-bottom:24px}
header h1{font-size:26px;margin-bottom:6px}
header p{color:#6e7991;font-size:14px}
.summary-bar{display:flex;gap:16px;margin-bottom:24px;align-items:center}
.summary-stat{background:#fff;border-radius:12px;padding:12px 20px;box-shadow:0 1px 4px rgba(0,0,0,0.06);display:flex;align-items:center;gap:8px}
.summary-stat .num{font-size:24px;font-weight:700;color:#c0392b}
.summary-stat .label{font-size:13px;color:#6e7991}
.filter-row{display:flex;gap:8px;margin-bottom:16px;align-items:center}
.filter-row select{padding:6px 12px;border:1.5px solid #eff1f6;border-radius:8px;font-size:13px;outline:none;background:#fff}
.filter-row input{padding:6px 12px;border:1.5px solid #eff1f6;border-radius:8px;font-size:13px;outline:none;flex:1;background:#fff}
.filter-row input:focus{border-color:#3567ff}
.issue-list{display:flex;flex-direction:column;gap:12px}
.issue-card{background:#fff;border-radius:12px;padding:20px;box-shadow:0 1px 4px rgba(0,0,0,0.06);border-left:3px solid #c0392b}
.issue-header{display:flex;justify-content:space-between;align-items:flex-start;margin-bottom:10px}
.issue-brand{font-size:16px;font-weight:600}
.issue-section{font-size:13px;color:#6e7991;margin-bottom:6px}
.issue-section b{color:#091026}
.issue-comment{background:#fff5f5;border-radius:8px;padding:10px 14px;font-size:14px;color:#c0392b;border-left:3px solid #c0392b;margin-bottom:10px;white-space:pre-wrap}
.issue-snippet{font-size:13px;color:#6e6e73;background:#ffffff;border-radius:6px;padding:8px 12px;margin-bottom:10px;max-height:80px;overflow-y:auto}
.issue-footer{display:flex;justify-content:space-between;align-items:center;font-size:12px;color:#a0a9bd}
.issue-footer a{color:#3567ff;text-decoration:none;font-weight:600}
.issue-footer a:hover{text-decoration:underline}
.copy-btn{padding:4px 12px;border:1px solid #eff1f6;border-radius:6px;font-size:12px;cursor:pointer;background:#fff;color:#6e7991;transition:all .15s}
.copy-btn:hover{border-color:#3567ff;color:#3567ff}
.empty-state{text-align:center;color:#a0a9bd;padding:60px 0;font-size:15px}

/* 列表卡片（模板规格） */
.report-item,.history-item{min-height:80px;box-sizing:border-box}
.report-list,.history-list,.conv-list,.issue-list,.check-list{display:flex;flex-direction:column;gap:16px}
.report-item,.history-item,.method-nav-item,.file-item,.conv-item,.methods-config-item,.issue-card{background:#fff;border:1px solid #eff1f6;border-radius:1rem;box-shadow:0 .0625rem .0625rem #0b14371a,0 .25rem .25rem #99beff1a;padding:16px 20px;transition:transform .2s,box-shadow .2s,background .2s}
.report-item,.history-item{cursor:pointer}
.report-item:hover,.history-item:hover{transform:translateY(-2px);box-shadow:0 .5rem 1rem #0b14371f}
.method-nav-item:hover,.file-item:hover,.conv-item:hover,.methods-config-item:hover{background:#f4f5ff;border-color:#dce1eb}
.method-nav-item.active,.report-item.active{background:#f4f5ff;border-color:#3567ff}
</style>
</head>
<body>
<nav class="topnav">
  <a href="/research" class="brand-logo"><img src="/brand/logo.png" alt="飞柚 GEO 品牌标识"></a>
  <div class="nav-dropdown">
    <div class="nav-dropdown-toggle"><span>分析</span>
      <svg class="nav-arrow" viewBox="0 0 20 20" fill="none"><path d="M15.5 7.03835L10 12.9614L4.5 7.03835" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"/></svg>
    </div>
    <div class="nav-dropdown-menu">
      <a href="/mining">GEO搜索问句挖掘</a>
      <a href="/research">客户市场调研</a>
    </div>
  </div>
  <a href="/audit">官网诊断</a>
  <a href="/chat">对话</a>
  <div class="nav-dropdown">
    <div class="nav-dropdown-toggle"><span>配置管理</span>
      <svg class="nav-arrow" viewBox="0 0 20 20" fill="none"><path d="M15.5 7.03835L10 12.9614L4.5 7.03835" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"/></svg>
    </div>
    <div class="nav-dropdown-menu">
      <a href="/skills">Skill管理</a>
      <a href="/company">企业资料</a>
      <a href="/oplog">操作日志</a>
      <a href="/backfill">JSON补录</a>
      <a href="/changelog">__ver__</a>
    </div>
  </div>
<!--标注汇总入口暂时隐藏-->
</nav>
<div class="app">
  <header>
    <h1>标注问题汇总</h1>
    <p>所有报告中标记为"有问题"的条目,可复制后发给Agent定位问题</p>
  </header>

  <div class="summary-bar">
    <div class="summary-stat"><span class="num" id="totalCount">0</span><span class="label">个问题</span></div>
    <div class="summary-stat"><span class="num" id="reportCount" style="color:#3567ff">0</span><span class="label">个报告</span></div>
    <button class="copy-btn" onclick="copyAll()" style="margin-left:auto;padding:8px 16px">复制全部问题</button>
  </div>

  <div class="filter-row">
    <select id="brandFilter" onchange="renderIssues()"><option value="">全部品牌</option></select>
    <input type="text" id="searchInput" placeholder="搜索问题说明..." oninput="renderIssues()">
  </div>

  <div class="issue-list" id="issueList">
    <div class="empty-state">加载中...</div>
  </div>
</div>

<script>
let allIssues = [];

async function loadIssues() {
  try {
    const resp = await fetch("/api/annotations/all");
    const data = await resp.json();
    allIssues = data.issues || [];
    document.getElementById("totalCount").textContent = data.total || 0;
    const brands = [...new Set(allIssues.map(i => i.brand))];
    document.getElementById("reportCount").textContent = brands.length;
    const filter = document.getElementById("brandFilter");
    filter.innerHTML = '<option value="">全部品牌</option>' +
      brands.map(b => '<option value="' + b + '">' + b + '</option>').join("");
    renderIssues();
  } catch(e) {
    document.getElementById("issueList").innerHTML = '<div class="empty-state">加载失败</div>';
  }
}

function renderIssues() {
  const brand = document.getElementById("brandFilter").value;
  const search = document.getElementById("searchInput").value.toLowerCase();
  const filtered = allIssues.filter(i => {
    if (brand && i.brand !== brand) return false;
    if (search && !(i.comment||"").toLowerCase().includes(search) && !(i.section_title||"").toLowerCase().includes(search)) return false;
    return true;
  });

  const list = document.getElementById("issueList");
  if (filtered.length === 0) {
    list.innerHTML = '<div class="empty-state">暂无标注问题</div>';
    return;
  }
  list.innerHTML = filtered.map(i => {
    const snippet = i.content_snippet ? '<div class="issue-snippet">' + escapeHtml(i.content_snippet) + '</div>' : '';
    return '<div class="issue-card">' +
      '<div class="issue-header"><div class="issue-brand">' + escapeHtml(i.brand) + '</div>' +
      '<button class="copy-btn" onclick="copyIssue(\\'' + escapeAttr(i.report) + '\\',\\'' + escapeAttr(i.aid) + '\\')">复制</button></div>' +
      '<div class="issue-section"><b>位置:</b> ' + escapeHtml(i.section_title || i.aid) + '</div>' +
      '<div class="issue-comment">' + escapeHtml(i.comment || "") + '</div>' +
      snippet +
      '<div class="issue-footer"><span>' + escapeHtml(i.timestamp || "") + '</span>' +
      '<a href="/result/' + encodeURIComponent(i.report) + '.html" target="_blank">查看报告</a></div>' +
    '</div>';
  }).join("");
}

function copyIssue(report, aid) {
  const issue = allIssues.find(i => i.report === report && i.aid === aid);
  if (!issue) return;
  const text = '【标注问题】\\n' +
    '品牌: ' + issue.brand + '\\n' +
    '位置: ' + (issue.section_title || issue.aid) + '\\n' +
    '问题说明: ' + (issue.comment || "") + '\\n' +
    '报告: /result/' + issue.report + '.html\\n' +
    '时间: ' + (issue.timestamp || "");
  navigator.clipboard.writeText(text).then(() => {
    alert("已复制到剪贴板");
  }).catch(() => {
    prompt("请手动复制:", text);
  });
}

function copyAll() {
  if (allIssues.length === 0) { alert("暂无标注问题"); return; }
  const text = allIssues.map((i, idx) =>
    '--- 问题' + (idx+1) + ' ---\\n' +
    '品牌: ' + i.brand + '\\n' +
    '位置: ' + (i.section_title || i.aid) + '\\n' +
    '问题说明: ' + (i.comment || "") + '\\n' +
    '时间: ' + (i.timestamp || "")
  ).join("\\n\\n");
  navigator.clipboard.writeText(text).then(() => {
    alert("已复制" + allIssues.length + "条问题到剪贴板");
  }).catch(() => {
    prompt("请手动复制:", text);
  });
}

function escapeHtml(s) { return (s||"").replace(/&/g,"&amp;").replace(/</g,"&lt;").replace(/>/g,"&gt;"); }
function escapeAttr(s) { return (s||"").replace(/'/g,"\\\\'").replace(/"/g,"&quot;"); }

loadIssues();


// 二级菜单：悬停开合 + 移出宽限 250ms
(function(){
  document.querySelectorAll('.nav-dropdown').forEach(function(dd){
    var t=null;
    function open(){clearTimeout(t);dd.classList.add('open');}
    function close(){clearTimeout(t);t=setTimeout(function(){dd.classList.remove('open');},250);}
    dd.addEventListener('mouseenter',open);dd.addEventListener('mouseleave',close);
    dd.addEventListener('focusin',open);dd.addEventListener('focusout',close);
    dd.querySelector('.nav-dropdown-toggle').addEventListener('click',function(e){e.preventDefault();dd.classList.toggle('open');});
  });
})();
// 列表卡片滚动进场
(function(){
  var els=document.querySelectorAll('.report-item,.history-item,.issue-card,.method-nav-item,.file-item,.conv-item,.methods-config-item');
  if(!('IntersectionObserver' in window)||!els.length)return;
  var io=new IntersectionObserver(function(es){es.forEach(function(en){if(en.isIntersecting){en.target.style.transition='opacity .5s ease,transform .5s ease';en.target.style.opacity='1';en.target.style.transform='translateY(0)';io.unobserve(en.target);}});},{threshold:0.08});
  els.forEach(function(el){el.style.opacity='0';el.style.transform='translateY(5%)';io.observe(el);});
})();
</script>
</body>
</html>'''


@app.get("/annotations", response_class=HTMLResponse)
async def annotations_ui():
    return annotations_ui_html().replace('__ver__', VER_LABEL)


# ── Site Audit UI（GEO 官网诊断）──────────────────────

# 官网诊断页企业性质分布（与 /research、/mining 同款组件，站点口径文案）
_AUDIT_NATURE_CSS = """
.nature-section{margin-bottom:24px}
.nature-head{display:flex;align-items:center;justify-content:space-between;margin-bottom:12px}
.nature-head h3{font-size:16px;color:#6e7991;margin:0}
.nature-tabs{display:flex;gap:4px;background:#eff1f6;border-radius:8px;padding:3px}
.nature-tabs button{border:none;background:transparent;font-size:12px;color:#6e7991;padding:4px 12px;border-radius:6px;cursor:pointer;font-family:inherit}
.nature-tabs button.on{background:#fff;color:#091026;font-weight:600;box-shadow:0 1px 3px rgba(0,0,0,.08)}
.nature-body{display:flex;flex-direction:column;gap:10px;background:#fff;border:1px solid #eff1f6;border-radius:1rem;box-shadow:0 .0625rem .0625rem #0b14371a,0 .25rem .25rem #99beff1a;padding:16px 100px}
.nature-body.mode-pie{flex-direction:row;align-items:center;justify-content:flex-start;gap:170px}
.nature-body.mode-treemap{padding:16px 20px}  /* 方块图不与饼图共用大边距，铺满卡片 */
#naturePie{position:relative;width:100%;height:220px;flex-shrink:0}
.mode-pie #naturePie{width:260px;display:flex;align-items:center;justify-content:center}
.mode-pie .donut-wrap{position:relative;width:200px;height:200px;filter:drop-shadow(0 1px 4px rgba(0,0,0,.12))}
.mode-pie .donut{position:absolute;inset:0;border-radius:50%;-webkit-mask:radial-gradient(closest-side,transparent 62%,#000 62.5%);mask:radial-gradient(closest-side,transparent 62%,#000 62.5%)}
.mode-pie .donut-c{position:absolute;inset:0;display:flex;flex-direction:column;align-items:center;justify-content:center;text-align:center}
.mode-pie .donut-c b{font-size:26px;font-weight:700;color:#091026;line-height:1}
.mode-pie .donut-c span{font-size:12px;color:#6e7991;margin-top:5px}
.mode-pie .nature-legend{flex:0 0 auto}
.mode-pie .nl-row{width:300px}
.mode-pie .nature-legend .pct{margin-left:auto}
.tm-cell{position:absolute;border-radius:6px;color:#fff;padding:8px 10px;overflow:hidden;box-sizing:border-box;cursor:default}
.tm-cell b{display:block;font-size:13px;font-weight:600}
.tm-cell span{font-size:12px;opacity:.85}
.tm-cell .tm-tip{display:none;position:absolute;left:8px;top:6px;white-space:nowrap;background:rgba(9,16,38,.85);padding:2px 8px;border-radius:5px;font-size:11px;font-style:normal}
.tm-cell.tm-sm:hover .tm-tip{display:block}
.nature-legend{flex:1;display:flex;flex-direction:column;gap:8px;font-size:13px;color:#3d4664}
.nature-legend .nl-row{display:flex;align-items:center;gap:8px}
.nature-legend .dot{width:10px;height:10px;border-radius:3px;flex-shrink:0}
.nature-legend .pct{margin-left:auto;font-weight:600;color:#091026}
.nature-empty{color:#a0a9bd;font-size:13px;padding:12px 0}
.btn-bind{border:1px solid #f3b2b2;color:#dc2626;background:#fef5f5;border-radius:8px;padding:2px 10px;font-size:13px;cursor:pointer;font-family:inherit;white-space:nowrap}
.badge-bind{border-radius:8px;padding:2px 10px;font-size:13px;white-space:nowrap}
.badge-bind.done{background:#eef7ee;color:#2e7d32}
.bind-cand{text-align:left;border:1px solid #eff1f6;background:#fff;border-radius:8px;padding:9px 12px;font-size:14px;color:#091026;cursor:pointer;font-family:inherit}
.bind-cand:hover{background:#f4f5ff}
.bind-cand:disabled{opacity:.5;cursor:wait}
.bind-en{padding:5px 0;color:#6e7991;font-size:13px}
"""
_AUDIT_NATURE_HTML = """
  <div class="nature-section">
    <div class="nature-head"><h3>企业性质分布</h3>
      <div class="nature-tabs">
        <button id="tabPie" class="on" onclick="natureMode('pie')">饼图</button>
        <button id="tabTm" onclick="natureMode('treemap')">矩形图</button>
      </div>
    </div>
    <div style="color:#a0a9bd;font-size:12px;margin:-6px 0 10px">诊断对象是网址，未在企业资料建档的站点计入「未标注」</div>
    <div class="nature-body">
      <div id="naturePie"></div>
      <div class="nature-legend" id="natureLegend"></div>
    </div>
  </div>
"""
_AUDIT_NATURE_JS = """
// 企业性质分布（官网诊断口径）：饼图/方块图，原生实现
let _natureMode = 'pie', _natureData = null;
function natureMode(m){ _natureMode = m;
  document.getElementById('tabPie').className = m==='pie'?'on':'';
  document.getElementById('tabTm').className = m==='treemap'?'on':'';
  renderNature(); }
async function loadNature(){
  if(!document.getElementById('naturePie')) return;
  try{ _natureData = await (await fetch("/api/nature/stats?source=audit")).json(); }
  catch(e){ console.warn("nature stats failed", e); return; }
  renderNature();
}
function renderNature(){
  const d = _natureData, el = document.getElementById("naturePie"), lg = document.getElementById("natureLegend");
  if(!d || !el) return;
  el.innerHTML = '';
  document.querySelector('.nature-body').className = 'nature-body mode-' + _natureMode;
  const COLORS = {"上市企业":"#4e79a7","国有企业":"#b07aa1","港澳台投资企业":"#d37295","外资企业":"#8c564b","集团企业":"#e15759","大型企业":"#f28e2c","中型企业":"#76b7b2","小型企业":"#59a14f","微型企业":"#9ed089","未标注":"#bab0ab"};
  if(!d.total){ el.innerHTML='<div class="nature-empty">暂无诊断报告</div>'; lg.innerHTML=''; return; }
  const note = '<div style="color:#6e7991;font-size:12px">共 '+d.total+' 个站点（每站点按最新一次诊断计）</div>';
  if(_natureMode === 'pie'){
    let acc=0, stops=[], rows=[], top=d.items[0];
    const GAP=1.5, TR='rgba(255,255,255,0)';
    d.items.forEach(it=>{ if(it.value>top.value) top=it; });
    d.items.forEach(it=>{
      const from=acc/d.total*360, to=(acc+it.value)/d.total*360; acc+=it.value;
      const g = d.items.length>1 && to-from>GAP*4 ? GAP/2 : 0;
      stops.push(COLORS[it.name]+" "+(from+g).toFixed(2)+"deg "+(to-g).toFixed(2)+"deg");
      rows.push('<div class="nl-row"><span class="dot" style="background:'+COLORS[it.name]+'"></span>'+
        it.name+' '+it.value+' 个<span class="pct">'+Math.round(it.value/d.total*100)+'%</span></div>');
    });
    el.innerHTML='<div class="donut-wrap"><div class="donut" style="background:conic-gradient('+stops.join(',')+')"></div>'+
      '<div class="donut-c"><b>'+Math.round(top.value/d.total*100)+'%</b><span>'+top.name+'</span></div></div>';
    lg.innerHTML=note+rows.join('');
    return;
  }
  lg.innerHTML = note;
  const W = el.clientWidth || 660, H = el.clientHeight || 220, GAP = 3;
  const items = d.items.slice().sort((a,b)=>b.value-a.value)
    .map(it=>({name:it.name, value:it.value, a:it.value/d.total*W*H}));
  const worst=(row,side)=>{ let s=0,mx=0,mn=Infinity;
    row.forEach(r=>{s+=r.a; if(r.a>mx)mx=r.a; if(r.a<mn)mn=r.a;});
    return Math.max(side*side*mx/(s*s), s*s/(side*side*mn)); };
  let x=0,y=0,w=W,h=H,row=[];
  const put=(r,bx,by,bw,bh)=>{
    const div=document.createElement('div'); div.className='tm-cell';
    div.style.cssText='left:'+(bx+GAP)+'px;top:'+(by+GAP)+'px;width:'+(bw-2*GAP)+'px;height:'+(bh-2*GAP)+'px;background:'+(COLORS[r.name]||'#9c755f');
    const pct = Math.round(r.value/d.total*100);
    const small = bw<96 || bh<46;
    div.innerHTML='<b>'+r.name+'</b><span>'+r.value+' 个 · '+pct+'%</span>'+
      (small ? '<i class="tm-tip">'+r.name+' '+r.value+' 个 · '+pct+'%</i>' : '');
    if(small) div.classList.add('tm-sm');
    el.appendChild(div); };
  const flush=()=>{ if(!row.length) return; const s=row.reduce((t,r)=>t+r.a,0);
    if(w>=h){ const rw=s/h; let yy=y; row.forEach(r=>{const rh=r.a/rw; put(r,x,yy,rw,rh); yy+=rh;}); x+=rw; w-=rw; }
    else { const rh=s/w; let xx=x; row.forEach(r=>{const rwi=r.a/rh; put(r,xx,y,rwi,rh); xx+=rwi;}); y+=rh; h-=rh; }
    row=[]; };
  items.forEach(it=>{ const side=Math.min(w,h);
    if(row.length && worst(row.concat([it]),side)>worst(row,side)) flush();
    row.push(it); });
  flush();
}
loadNature();

// 企业人工绑定（多候选/英文站纠错入口）：候选点选或填中文全称，后端工商核验后落定
function _bindEsc(s){return String(s==null?'':s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/'/g,"\\'")}
function openBindModal(stem){
  const r=allReports.find(function(x){return (x.html_file||'').replace(/\\.html$/,'')===stem;})||{};
  const t=document.getElementById('bindTarget');
  t.textContent=r.url||stem; t.dataset.stem=stem;
  const cn=[],en=[];
  (r.company_candidates||[]).forEach(function(c){ (/[\u4e00-\u9fa5]/.test(c)?cn:en).push(c); });
  document.getElementById('bindCn').innerHTML=cn.length?cn.map(function(c){
    return '<button class="bind-cand" onclick="doBind(\\''+_bindEsc(stem)+'\\',\\''+_bindEsc(c)+'\\')">'+_bindEsc(c).replace(/&/g,'&amp;')+'</button>';}).join('')
    :'<div style="color:#a0a9bd;font-size:13px">本报告无中文候选</div>';
  document.getElementById('bindEn').innerHTML=en.map(function(c){return '<div class="bind-en">'+c.replace(/&/g,'&amp;').replace(/</g,'&lt;')+'</div>';}).join('');
  document.getElementById('bindEnWrap').style.display=en.length?'':'none';
  document.getElementById('bindManual').value='';
  document.getElementById('bindModal').classList.add('show');
}
function closeBindModal(){document.getElementById('bindModal').classList.remove('show');}
async function doBind(stem, company){
  company=(company||'').trim();
  if(!company) company=document.getElementById('bindManual').value.trim();
  if(!company){showToast('请选择候选或填写企业工商全称');return;}
  document.querySelectorAll('.bind-cand').forEach(function(b){b.disabled=true;});
  try{
    const resp=await fetch('/api/audit/bind',{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({filename:stem,company:company})});
    const d=await resp.json();
    if(d.ok){showToast('已绑定：'+d.company);closeBindModal();loadReportList();loadNature();}
    else showToast(d.error||'绑定失败');
  }catch(e){showToast('绑定失败：网络异常');}
  document.querySelectorAll('.bind-cand').forEach(function(b){b.disabled=false;});
}
"""


_AUDIT_BIND_HTML = """
<div class="prio-modal" id="bindModal" onclick="if(event.target===this)closeBindModal()">
  <div class="prio-box" style="width:540px">
    <div class="prio-head">
      <div><h3>绑定企业主体</h3><div class="sub" id="bindTarget"></div></div>
      <button onclick="closeBindModal()" style="border:none;background:#ffffff;width:30px;height:30px;border-radius:8px;cursor:pointer;font-size:16px;color:#6b7280">×</button>
    </div>
    <div style="padding:16px 20px;overflow:auto">
      <div style="font-size:13px;color:#6e7991;margin-bottom:8px">中文候选（点击即绑定，仍经工商库精准核验）</div>
      <div id="bindCn" style="display:flex;flex-direction:column;gap:6px;margin-bottom:14px"></div>
      <div id="bindEnWrap" style="display:none;margin-bottom:14px">
        <div style="font-size:13px;color:#6e7991;margin-bottom:6px">英文候选（国内公司请在下方填对应中文工商全称）</div>
        <div id="bindEn"></div>
      </div>
      <input id="bindManual" type="text" placeholder="或直接填写企业工商全称（含地区前缀，如：深圳××科技有限公司）"
             style="width:100%;padding:10px 12px;border:1.5px solid #eff1f6;border-radius:10px;font-size:14px;outline:none;font-family:inherit;box-sizing:border-box">
      <button onclick="doBind(document.getElementById('bindTarget').dataset.stem,'')"
              style="margin-top:12px;width:100%;padding:10px;border:none;border-radius:10px;background:#3567ff;color:#fff;font-size:14px;font-weight:600;cursor:pointer;font-family:inherit">核验并绑定</button>
    </div>
  </div>
</div>
"""


def audit_ui_html() -> str:
    # 企业性质分布注入：样式入 <style> 尾、区块入历史报告区上方、脚本入主 <script> 头（锚点在本模板内唯一）
    return ('''<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>官网诊断 — GEO Agent</title>
<style>
*{margin:0;padding:0;box-sizing:border-box}
body{font-family:Figtree,-apple-system,"PingFang SC","Microsoft YaHei",sans-serif;background:#ffffff;color:#091026;line-height:1.6}
.topnav{display:flex;gap:24px;justify-content:flex-end;align-items:center;padding:24px;max-width:1200px;margin:0 auto;background:#fff;position:relative;box-sizing:border-box}
.topnav a{padding:0;border:none;background:transparent;font-size:16px;font-weight:400;line-height:20px;text-decoration:none;color:#6e7991;transition:color .2s;font-family:inherit}
.topnav a:hover{color:#091026}
.topnav a.active{color:#091026;font-weight:600}
.topnav a.brand-logo{position:absolute;left:24px;top:50%;transform:translateY(-50%);display:flex;align-items:center;text-decoration:none}
.brand-logo img{height:28px;width:auto;display:block}
.nav-dropdown{position:relative}
.nav-dropdown-toggle{display:flex;align-items:center;gap:6px;font-size:16px;font-weight:400;color:#6e7991;cursor:pointer;transition:color .2s}
.nav-dropdown-toggle.active{color:#091026;font-weight:600}
.nav-dropdown:hover .nav-dropdown-toggle{color:#091026}
.nav-arrow{width:1rem;height:1rem;transition:transform .2s}
.nav-dropdown.open .nav-arrow{transform:rotate(180deg)}
.nav-dropdown-menu{position:absolute;top:100%;right:0;margin-top:12px;padding:8px;background:#fff;border:1px solid #eff1f6;border-radius:1rem;box-shadow:0 .0625rem .0625rem #0b14371a,0 .25rem .25rem #99beff1a;display:none;z-index:50}
.nav-dropdown.open .nav-dropdown-menu{display:block}
.nav-dropdown:hover .nav-dropdown-menu{display:block}
.nav-dropdown-menu a{display:block;padding:8px 12px;border-radius:.5rem;font-size:16px;color:#6e7991;text-decoration:none;white-space:nowrap;transition:all .15s}
.nav-dropdown-menu a:hover{background:#f4f5ff;color:#091026}
.nav-dropdown::after{content:"";position:absolute;left:0;right:0;top:100%;height:12px}
.topnav-OLD{display:none}
.wrap{max-width:1200px;margin:0 auto;padding:24px 24px 40px;box-sizing:border-box}
header h1{font-size:26px;margin-bottom:6px}
header p{color:#6e7991;font-size:14px;margin-bottom:20px}
.audit-form{background:#fff;border-radius:16px;padding:20px;box-shadow:0 1px 4px rgba(0,0,0,0.06);display:flex;gap:12px;margin-bottom:20px}
.audit-form input{flex:1;padding:12px 16px;border:1.5px solid #eff1f6;border-radius:10px;font-size:15px;outline:none;transition:border-color .2s;font-family:inherit}
.audit-form input:focus{border-color:#3567ff}
.audit-form button{padding:12px 28px;border:none;border-radius:10px;background:#3567ff;color:#fff;font-size:15px;font-weight:600;cursor:pointer;transition:opacity .2s;white-space:nowrap}
.audit-form button:hover{opacity:.9}
.audit-form button:disabled{opacity:.5;cursor:not-allowed}
.audit-form button.btn-ghost{background:#fff;color:#3567ff;border:1.5px solid #3567ff}
.audit-form button.btn-ghost:hover{opacity:1;background:#f0f7ff}
/* 优先级配置弹窗 */
.prio-modal{display:none;position:fixed;inset:0;z-index:1000;background:rgba(15,23,42,.5)}
.prio-modal.show{display:flex;align-items:flex-start;justify-content:center}
.prio-box{background:#fff;border-radius:16px;width:720px;max-width:calc(100vw - 40px);max-height:84vh;margin-top:6vh;display:flex;flex-direction:column;box-shadow:0 20px 60px rgba(0,0,0,.25);overflow:hidden}
.prio-head{display:flex;justify-content:space-between;align-items:center;padding:16px 20px;border-bottom:1px solid #e5e5ea}
.prio-head h3{font-size:16px;margin:0}
.prio-head .sub{font-size:12px;color:#6e7991;margin-top:2px}
.prio-body{overflow:auto;padding:4px 20px 16px;flex:1}
.prio-cat{font-size:13px;font-weight:700;margin:14px 0 6px;color:#091026}
.prio-row{display:flex;align-items:center;gap:10px;padding:7px 0;border-bottom:1px solid #f2f2f5;font-size:13px}
.prio-row .nm{flex:0 0 34%;min-width:0;font-weight:500}
.prio-row .mod{font-size:10px;font-weight:600;color:#b45309;background:#fef3c7;border-radius:6px;padding:1px 6px;display:none;flex-shrink:0}
.prio-row.modified .mod{display:inline}
.prio-row .purpose{flex:1;min-width:0;font-size:11px;color:#6b7280;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.prio-row .aud{font-size:10px;font-weight:600;padding:1px 6px;border-radius:6px;flex-shrink:0}
.aud-both{color:#3567ff;background:#e8f2ff}
.aud-seo{color:#16a34a;background:#ecfaf0}
.aud-geo{color:#5C3CF1;background:#efeafe}
.aud-diag{color:#6b7280;background:#eff1f6}
.prio-row select{padding:4px 8px;border:1.5px solid #eff1f6;border-radius:8px;font-size:13px;background:#fff;cursor:pointer;flex-shrink:0}
.prio-row.modified select{border-color:#3567ff}
.prio-search{padding:10px 20px 0}
.prio-search input{width:100%;padding:9px 14px;border:1.5px solid #eff1f6;border-radius:10px;font-size:13px;outline:none;font-family:inherit}
.prio-search input:focus{border-color:#3567ff}
.prio-foot{display:flex;justify-content:space-between;align-items:center;gap:10px;padding:14px 20px;border-top:1px solid #e5e5ea}
.prio-foot .tip{font-size:11px;color:#6e7991}
.prio-foot .ops{display:flex;gap:8px;flex-shrink:0}
.prio-foot button{padding:8px 18px;border:none;border-radius:9px;font-size:13px;font-weight:600;cursor:pointer}
.prio-foot button:disabled{opacity:.5;cursor:not-allowed}
.btn-prio-cancel{background:#ffffff;color:#091026}
.btn-prio-reset{background:#fff;color:#c0392b;border:1px solid #f5c6cb}
.btn-prio-save{background:#3567ff;color:#fff}
.hint{font-size:12px;color:#a0a9bd;margin-top:8px}
.msg{padding:12px 16px;border-radius:10px;font-size:14px;margin-bottom:16px;display:none}
.msg.err{display:block;background:#fdf0ed;color:#c0392b;border:1px solid #f5c6cb}
.loading{display:none;text-align:center;padding:40px;background:#fff;border-radius:16px;box-shadow:0 1px 4px rgba(0,0,0,0.06);margin-bottom:20px}
.spinner{width:36px;height:36px;border:3px solid #eff1f6;border-top-color:#3567ff;border-radius:50%;animation:spin .8s linear infinite;margin:0 auto 14px}
@keyframes spin{to{transform:rotate(360deg)}}
.loading .lt{font-size:15px;font-weight:600;margin-bottom:4px}
.loading .ls{font-size:13px;color:#6e7991}
/* 标题旁的小号优先级配置按钮 */
.btn-prio-mini{display:flex;align-items:center;height:28px;box-sizing:border-box;margin-left:0;padding:0 10px;border:1px solid rgba(255,255,255,.35);border-radius:999px;font-size:12px;font-weight:500;line-height:12px;color:rgba(255,255,255,.85);text-decoration:none;cursor:pointer;background:rgba(255,255,255,.08);flex-shrink:0}
.btn-prio-mini:hover{background:rgba(255,255,255,.18);color:#fff}
/* 诊断任务进度面板 */
.jobs-panel{background:#fff;border-radius:16px;box-shadow:0 1px 4px rgba(0,0,0,.06);margin-bottom:20px;overflow:hidden}
.jobs-head{display:flex;align-items:center;gap:10px;padding:12px 16px;border-bottom:1px solid #f2f2f5}
.jobs-head .jt{font-size:14px;font-weight:700}
.jobs-head .jc{font-size:12px;color:#6e7991;flex:1}
.jobs-close{border:none;background:#fff;color:#9aa3b5;font-size:16px;cursor:pointer;width:24px;height:24px;border-radius:6px}
.jobs-close:hover{background:#f4f5ff}
.jobs-list{max-height:260px;overflow:auto;padding:6px 16px}
.job-row{display:flex;align-items:center;gap:10px;padding:7px 0;border-bottom:1px solid #f7f8fa;font-size:13px}
.job-row .ju{flex:1;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.job-row .js{font-size:11px;font-weight:600;padding:2px 8px;border-radius:6px;flex-shrink:0}
.js-run{color:#3567ff;background:#e8f2ff}
.js-done{color:#16a34a;background:#ecfaf0}
.js-err{color:#c0392b;background:#fdf0ed}
.job-row .je{font-size:11px;color:#c0392b;flex-shrink:0;max-width:40%;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
/* 历史报告搜索与分页 */
.rp-head{display:flex;align-items:center;gap:14px;margin-bottom:10px}
.rp-head h3{margin:0}
/* 历史报告搜索框：Agentflow 模板输入组件规格（.input / is-small） */
.rp-search{margin-left:auto;width:260px;max-width:50%;height:48px;padding:0 12px;border:1px solid #f7f9fc;border-radius:8px;background:#f7f9fc;color:#091026;font-size:12px;letter-spacing:-.03em;outline:none;font-family:inherit;transition:border-color .3s,color .3s;box-sizing:border-box}
.rp-search::placeholder{color:#6e7991}
.rp-search:hover{border-color:#15224a}
.rp-search:focus{border-color:#3567ff}
.rp-pager{display:flex;align-items:center;justify-content:center;gap:14px;padding:12px 0 0;font-size:13px;color:#6e7991}
.rp-pager button{padding:5px 14px;border:1px solid #eff1f6;background:#fff;border-radius:8px;font-size:13px;cursor:pointer;color:#091026}
.rp-pager button:disabled{opacity:.4;cursor:not-allowed}
.result{display:none}
.report-bar{display:flex;align-items:center;justify-content:space-between;margin-bottom:10px}
.report-bar .meta{font-size:13px;color:#6e7991}
.report-bar a{font-size:13px;color:#3567ff;text-decoration:none;font-weight:600}
.report-frame{width:100%;height:60vh;border:1px solid #eff1f6;border-radius:14px;background:#fff}
.report-expand{display:none;margin:3px 0 16px 0;padding:16px;background:#fff;border-radius:12px;box-shadow:0 1px 3px rgba(0,0,0,0.05);border:2px solid #3567ff}
.report-expand.show{display:block}
.report-expand .expand-bar{display:flex;justify-content:space-between;align-items:center;margin-bottom:12px}
.report-expand .expand-bar .meta{font-size:13px;color:#6b7280}
.report-expand .expand-bar a{font-size:13px;color:#3567ff;text-decoration:none}
.report-expand .expand-bar a:hover{text-decoration:underline}
.history-section{margin-top:24px}
.history-section h3{font-size:16px;margin-bottom:12px;color:#091026}
.report-list{display:flex;flex-direction:column;gap:8px}
.report-item{display:flex;align-items:center;gap:12px;padding:14px 16px;background:#fff;border-radius:12px;box-shadow:0 1px 3px rgba(0,0,0,0.05);cursor:pointer;transition:all .15s;border:2px solid transparent}
.report-item:hover{border-color:#3567ff;box-shadow:0 2px 8px rgba(0,113,227,.12)}
.report-item.active{border-color:#3567ff;background:#f0f7ff}
.report-item .badge-new{background:#ef4444;color:#fff;font-size:13px;font-weight:700;padding:1px 6px;border-radius:8px;flex-shrink:0}
.report-item .badge-grade{font-size:13px;font-weight:700;color:#fff;padding:2px 6px;border-radius:6px;flex-shrink:0}
.report-item .badge-brand{font-size:13px;font-weight:600;color:#3567ff;background:#eef0ff;padding:2px 8px;border-radius:6px;flex-shrink:0;max-width:120px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.report-item .r-url{font-size:13px;color:#091026;font-weight:500;flex:0 1 auto;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.report-item .r-score{font-size:13px;font-weight:700;flex-shrink:0;min-width:48px;text-align:right;margin-left:auto}
.report-item .r-time{font-size:13px;color:#6e7991;flex-shrink:0;min-width:100px;text-align:right}
.report-item .r-tokens{position:relative;font-size:13px;font-weight:600;color:#5C3CF1;background:#5C3CF11a;padding:2px 8px;border-radius:6px;flex-shrink:0;white-space:nowrap;cursor:default}
.report-item .r-tokens .tok-tip{display:none;position:absolute;top:calc(100% + 6px);right:0;background:#091026;color:#fff;font-size:11px;font-weight:400;padding:5px 10px;border-radius:6px;white-space:nowrap;z-index:20;box-shadow:0 2px 8px rgba(0,0,0,.18);pointer-events:none}
.report-item .r-tokens:hover .tok-tip{display:block}
.report-item .r-stats{font-size:13px;color:#6e7991;flex-shrink:0;margin-left:auto;position:relative;cursor:default}
.report-item .r-stats-tip{display:none;position:absolute;top:calc(100% + 6px);right:0;background:#091026;color:#fff;font-size:11px;font-weight:400;padding:5px 10px;border-radius:6px;white-space:nowrap;z-index:20;box-shadow:0 2px 8px rgba(0,0,0,.18);pointer-events:none}
.report-item .r-stats:hover .r-stats-tip{display:block}
.report-item .r-stats .s-pass{color:#16a34a}
.report-item .r-stats .s-warn{color:#d97706}
.report-item .r-stats .s-fail{color:#dc2626}
.report-item .r-stats ~ .r-score{margin-left:0}
.report-item .btn-del{flex-shrink:0;width:28px;height:28px;border:none;border-radius:8px;background:transparent;color:#a0a9bd;font-size:16px;cursor:pointer;display:flex;align-items:center;justify-content:center;transition:all .15s}
.report-item .btn-del:hover{background:#fef2f2;color:#dc2626}
.report-item .btn-del.confirming{background:#dc2626;color:#fff;font-size:11px;font-weight:600}
.btn-cancel{flex-shrink:0;border:1px solid #eff1f6;background:#fff;color:#6e7991;border-radius:8px;padding:4px 12px;font-size:12px;cursor:pointer;font-family:inherit;transition:all .15s}
.btn-cancel:hover{background:#fef2f2;color:#dc2626;border-color:#f3b2b2}
.btn-cancel.confirming{background:#dc2626;color:#fff;border-color:#dc2626;font-weight:600}

.btn-dbl{display:inline-flex;justify-content:center;align-items:center;border-radius:1rem;padding:1px;position:relative;overflow:hidden;text-decoration:none;cursor:pointer;transition:transform .3s;font-family:inherit}
.btn-dbl:hover{transform:scale3d(.98,.98,1.01)}
.btn-dbl-inner{display:flex;justify-content:center;align-items:center;gap:.5rem;width:100%;height:100%;padding:1rem 2rem;border-radius:.9375rem;font-size:1rem;font-weight:600;line-height:1.15;position:relative;overflow:hidden}
.btn-dbl.is-primary{background-image:linear-gradient(#061b5e,#2a57bf);box-shadow:inset 0 -.0625rem .375rem #4361a6,inset 0 .0625rem .375rem #12214399}
.btn-dbl.is-primary .btn-dbl-inner{background-image:linear-gradient(#224390,#091026);color:#fff;box-shadow:inset 0 -.0625rem .375rem #4361a6,inset 0 .0625rem .375rem #12214399}
.btn-dbl.is-dark{background-image:linear-gradient(#fbfcff,#dfeaff)}
.btn-dbl.is-dark .btn-dbl-inner{background-image:linear-gradient(0deg,#dfeaff,#fbfcff);color:#091026}
.btn-glow{position:absolute;width:20%;height:80%;top:0;left:0;border-radius:inherit;background-color:#3567ff99;filter:blur(1.125rem);opacity:0;transition:opacity .35s;pointer-events:none}
.btn-dbl-inner .btn-glow{width:25%;height:60%;top:-10%;left:-10%;background-color:#fff6}
.btn-dbl:hover .btn-glow{opacity:1}
.btn-dbl.is-dark .btn-glow{background-color:#fff6}
.btn-dbl.is-loading{opacity:.5;pointer-events:none}

/* 列表卡片（模板规格） */
.report-item,.history-item{min-height:80px;box-sizing:border-box}
.report-list,.history-list,.conv-list,.issue-list,.check-list{display:flex;flex-direction:column;gap:16px}
.report-item,.history-item,.method-nav-item,.file-item,.conv-item,.methods-config-item,.issue-card{background:#fff;border:1px solid #eff1f6;border-radius:1rem;box-shadow:0 .0625rem .0625rem #0b14371a,0 .25rem .25rem #99beff1a;padding:16px 20px;transition:transform .2s,box-shadow .2s,background .2s}
.report-item,.history-item{cursor:pointer}
.report-item:hover,.history-item:hover{transform:translateY(-2px);box-shadow:0 .5rem 1rem #0b14371f}
.method-nav-item:hover,.file-item:hover,.conv-item:hover,.methods-config-item:hover{background:#f4f5ff;border-color:#dce1eb}
.method-nav-item.active,.report-item.active{background:#f4f5ff;border-color:#3567ff}

/* 顶部 hero 卡片（参考 about 页 hero 规格） */
.hero-card{background-color:#091026;background-image:url("data:image/svg+xml;base64,PHN2ZyB4bWxucz0iaHR0cDovL3d3dy53My5vcmcvMjAwMC9zdmciIHdpZHRoPSIyNjAiIGhlaWdodD0iMjYwIiB2aWV3Qm94PSIwIDAgMjYwIDI2MCI+PGNpcmNsZSBjeD0iMjI5LjMiIGN5PSI2NS45IiByPSIwLjU4IiBmaWxsPSIjYThjNGZmIiBvcGFjaXR5PSIwLjM4Ii8+PGNpcmNsZSBjeD0iMTg2LjMiIGN5PSIxNDcuNCIgcj0iMS4xMSIgZmlsbD0iI2E4YzRmZiIgb3BhY2l0eT0iMC4xNSIvPjxjaXJjbGUgY3g9IjQ4LjciIGN5PSIzMy42IiByPSIxLjAxIiBmaWxsPSIjYThjNGZmIiBvcGFjaXR5PSIwLjQyIi8+PGNpcmNsZSBjeD0iOTQuOCIgY3k9IjI0LjAiIHI9IjEuMDQiIGZpbGw9IiNhOGM0ZmYiIG9wYWNpdHk9IjAuMjIiLz48Y2lyY2xlIGN4PSIzNi43IiBjeT0iNzEuNSIgcj0iMC42MyIgZmlsbD0iI2E4YzRmZiIgb3BhY2l0eT0iMC4yOSIvPjxjaXJjbGUgY3g9IjIwNS4zIiBjeT0iMTc1LjAiIHI9IjEuMTkiIGZpbGw9IiNhOGM0ZmYiIG9wYWNpdHk9IjAuMzUiLz48Y2lyY2xlIGN4PSIxNDcuNCIgY3k9IjQ0LjEiIHI9IjAuOTYiIGZpbGw9IiNhOGM0ZmYiIG9wYWNpdHk9IjAuMzUiLz48Y2lyY2xlIGN4PSIyNTQuMiIgY3k9IjIuMSIgcj0iMC43NSIgZmlsbD0iI2E4YzRmZiIgb3BhY2l0eT0iMC4zOCIvPjxjaXJjbGUgY3g9IjY3LjMiIGN5PSIyNTcuMSIgcj0iMC42MiIgZmlsbD0iI2E4YzRmZiIgb3BhY2l0eT0iMC4zIi8+PGNpcmNsZSBjeD0iMTMwLjciIGN5PSI4Ny4wIiByPSIxLjIiIGZpbGw9IiNhOGM0ZmYiIG9wYWNpdHk9IjAuMyIvPjxjaXJjbGUgY3g9IjE3Ni4wIiBjeT0iMTU3LjgiIHI9IjEuMTYiIGZpbGw9IiNhOGM0ZmYiIG9wYWNpdHk9IjAuNDUiLz48Y2lyY2xlIGN4PSIyMTAuNCIgY3k9IjE3My45IiByPSIwLjQ3IiBmaWxsPSIjYThjNGZmIiBvcGFjaXR5PSIwLjQzIi8+PGNpcmNsZSBjeD0iMjAyLjIiIGN5PSIxOTcuOCIgcj0iMC44NiIgZmlsbD0iI2E4YzRmZiIgb3BhY2l0eT0iMC4zNCIvPjxjaXJjbGUgY3g9IjYwLjEiIGN5PSIyMTQuMyIgcj0iMC42NCIgZmlsbD0iI2E4YzRmZiIgb3BhY2l0eT0iMC40NiIvPjxjaXJjbGUgY3g9IjE3OC43IiBjeT0iMTE2LjUiIHI9IjEuMTQiIGZpbGw9IiNhOGM0ZmYiIG9wYWNpdHk9IjAuNDkiLz48Y2lyY2xlIGN4PSI4NS4xIiBjeT0iNTguNSIgcj0iMC41NiIgZmlsbD0iI2E4YzRmZiIgb3BhY2l0eT0iMC40Ii8+PGNpcmNsZSBjeD0iMTgxLjAiIGN5PSI3My41IiByPSIwLjQ4IiBmaWxsPSIjYThjNGZmIiBvcGFjaXR5PSIwLjIxIi8+PGNpcmNsZSBjeD0iODYuOCIgY3k9IjE2NC4zIiByPSIwLjg5IiBmaWxsPSIjYThjNGZmIiBvcGFjaXR5PSIwLjQzIi8+PGNpcmNsZSBjeD0iMTIwLjMiIGN5PSIyMzAuMiIgcj0iMS4yIiBmaWxsPSIjYThjNGZmIiBvcGFjaXR5PSIwLjE4Ii8+PGNpcmNsZSBjeD0iMTUuMyIgY3k9IjEzMy40IiByPSIwLjg3IiBmaWxsPSIjYThjNGZmIiBvcGFjaXR5PSIwLjQ4Ii8+PGNpcmNsZSBjeD0iMTM3LjUiIGN5PSIyMzAuNSIgcj0iMC40NyIgZmlsbD0iI2E4YzRmZiIgb3BhY2l0eT0iMC4yNiIvPjxjaXJjbGUgY3g9IjE4OC4yIiBjeT0iOTYuMCIgcj0iMC42MyIgZmlsbD0iI2E4YzRmZiIgb3BhY2l0eT0iMC40NiIvPjxjaXJjbGUgY3g9IjEyNS43IiBjeT0iMjguMiIgcj0iMC42NSIgZmlsbD0iI2E4YzRmZiIgb3BhY2l0eT0iMC4zNCIvPjxjaXJjbGUgY3g9IjkxLjYiIGN5PSIyMjEuMyIgcj0iMC41NiIgZmlsbD0iI2E4YzRmZiIgb3BhY2l0eT0iMC4zNSIvPjxjaXJjbGUgY3g9IjE3Mi4yIiBjeT0iMjE2LjciIHI9IjAuNzgiIGZpbGw9IiNhOGM0ZmYiIG9wYWNpdHk9IjAuNCIvPjxjaXJjbGUgY3g9Ijk5LjMiIGN5PSI0Ny44IiByPSIwLjQ5IiBmaWxsPSIjYThjNGZmIiBvcGFjaXR5PSIwLjEyIi8+PC9zdmc+"),radial-gradient(circle at 80% 118%, rgba(56,86,196,.85) 0%, rgba(9,16,38,0) 58%),radial-gradient(circle at 10% -12%, rgba(30,50,128,.6) 0%, rgba(9,16,38,0) 55%),linear-gradient(168deg, #0e1638 0%, #152964 58%, #203e98 100%);background-size:260px 260px,auto,auto,100% 100%;background-repeat:repeat,no-repeat,no-repeat,no-repeat;background-position:0 0,center,center,center;border-radius:40px;padding:40px;text-align:left;color:#f7f9fc;margin-bottom:24px}
.hero-title{display:flex;align-items:flex-start;justify-content:space-between;gap:16px;font-size:1.5rem;font-weight:600;line-height:1.15;color:#fff;margin:0 0 16px}
.hero-desc{font-size:14px;line-height:1.5;color:#c3cad8;margin:0 0 24px}
.hero-card .audit-form{background:transparent;border:none;box-shadow:none;padding:0;margin:0}
.hero-card .audit-form input{background:rgba(255,255,255,.1);border:none;color:#fff}
.hero-card .audit-form input:focus{border:none;background:rgba(255,255,255,.16)}
.hero-card .audit-form input::placeholder{color:#98a2b8}
</style>
</head>
<body>
<nav class="topnav">
  <a href="/research" class="brand-logo"><img src="/brand/logo.png" alt="飞柚 GEO 品牌标识"></a>
  <div class="nav-dropdown">
    <div class="nav-dropdown-toggle"><span>分析</span>
      <svg class="nav-arrow" viewBox="0 0 20 20" fill="none"><path d="M15.5 7.03835L10 12.9614L4.5 7.03835" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"/></svg>
    </div>
    <div class="nav-dropdown-menu">
      <a href="/mining">GEO搜索问句挖掘</a>
      <a href="/research">客户市场调研</a>
    </div>
  </div>
  <a href="/audit" class="active">官网诊断</a>
  <a href="/chat">对话</a>
  <div class="nav-dropdown">
    <div class="nav-dropdown-toggle"><span>配置管理</span>
      <svg class="nav-arrow" viewBox="0 0 20 20" fill="none"><path d="M15.5 7.03835L10 12.9614L4.5 7.03835" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"/></svg>
    </div>
    <div class="nav-dropdown-menu">
      <a href="/skills">Skill管理</a>
      <a href="/company">企业资料</a>
      <a href="/oplog">操作日志</a>
      <a href="/backfill">JSON补录</a>
      <a href="/changelog">__ver__</a>
    </div>
  </div>
<!--标注汇总入口暂时隐藏-->
</nav>
  <div class="wrap">
  <section class="hero-card">
    <h1 class="hero-title"><span style="display:flex;align-items:center;gap:8px">GEO 官网诊断 <a class="btn-prio-mini" onclick="openPrioModal()" role="button" tabindex="0">优先级配置</a></span></h1>
    <p class="hero-desc">输入官网网址，自动抽样抓取内页，从技术可抓取性、AI友好度、内容质量、权威信任、商业转化与决策内容 5 个维度执行数十项检查（含断链、重复元信息、关键词蚕食等跨页面检测）并生成诊断报告</p>

    <div class="audit-form">
      <input id="urlInput" type="text" placeholder="例如 https://www.example.com 或直接输入域名"
             onkeydown="if(event.key==='Enter')startAudit()">
      <a class="btn-dbl is-primary" id="auditBtn" onclick="startAudit()" role="button" tabindex="0"><span class="btn-dbl-inner">开始诊断<span class="btn-glow"></span></span><span class="btn-glow"></span></a>
      <a class="btn-dbl is-dark" onclick="openBatchModal()" role="button" tabindex="0"><span class="btn-dbl-inner">批量查询<span class="btn-glow"></span></span><span class="btn-glow"></span></a>
    </div>
  </section>

  <div class="msg" id="errMsg"></div>

  <div class="loading" id="loadingBox">
    <div class="spinner"></div>
    <div class="lt">正在诊断中…</div>
    <div class="ls" id="loadingStep">正在抓取首页、抽样内页并执行 46 项检查</div>
  </div>

  <div class="jobs-panel" id="jobsPanel" style="display:none">
    <div class="jobs-head">
      <span class="jt">诊断任务</span>
      <span class="jc" id="jobsCount"></span>
      <button class="jobs-close" onclick="dismissJobsPanel()" title="任务全部完成，收起">×</button>
    </div>
    <div class="jobs-list" id="jobsList"></div>
  </div>

  <div class="history-section">
    <div class="rp-head">
      <h3>历史报告</h3>
      <input class="rp-search" id="rpSearch" type="text" placeholder="搜索网址 / 品牌……" oninput="rpPickSearch(this.value)">
    </div>
    <div class="report-list" id="reportList">
      <div style="text-align:center;color:#6e7991;font-size:13px;padding:20px">加载中…</div>
    </div>
    <div class="rp-pager" id="rpPager" style="display:none">
      <button type="button" onclick="rpGo(-1)">‹ 上一页</button>
      <span id="rpPageInfo"></span>
      <button type="button" onclick="rpGo(1)">下一页 ›</button>
    </div>
  </div>
</div>

<div class="prio-modal" id="batchModal" onclick="if(event.target===this)closeBatchModal()">
  <div class="prio-box" style="width:560px">
    <div class="prio-head">
      <div>
        <h3>批量查询</h3>
        <div class="sub">每行一个网址，一次最多 10 个；提交后可离开本页，回来时任务进度会显示在页面上。进行中的任务总数不得超过 10 个</div>
      </div>
      <button onclick="closeBatchModal()" style="border:none;background:#ffffff;width:30px;height:30px;border-radius:8px;cursor:pointer;font-size:16px;color:#6b7280">×</button>
    </div>
    <div style="padding:14px 20px">
      <textarea id="batchUrls" rows="8" style="width:100%;padding:10px 12px;border:1.5px solid #eff1f6;border-radius:10px;font-size:13px;font-family:inherit;outline:none;resize:vertical" placeholder="https://www.example.com&#10;https://www.another.com&#10;example3.com"></textarea>
      <div id="batchErr" style="color:#c0392b;font-size:12px;margin-top:6px;min-height:16px"></div>
    </div>
    <div class="prio-foot">
      <span class="tip" id="batchTip"></span>
      <div class="ops">
        <button type="button" class="btn-prio-cancel" onclick="closeBatchModal()">取消</button>
        <button type="button" class="btn-prio-save" id="batchGoBtn" onclick="submitBatch()">创建批量任务</button>
      </div>
    </div>
  </div>
</div>

<div class="prio-modal" id="prioModal" onclick="if(event.target===this)closePrioModal()">
  <div class="prio-box">
    <div class="prio-head">
      <div>
        <h3>检查项优先级配置</h3>
        <div class="sub">P0=致命 · P1=重要 · P2=轻微；计票权重 2 / 1 / 0.5。保存后重算全部历史报告分数并按新优先级排序</div>
      </div>
      <button onclick="closePrioModal()" style="border:none;background:#ffffff;width:30px;height:30px;border-radius:8px;cursor:pointer;font-size:16px;color:#6b7280">×</button>
    </div>
    <div class="prio-search">
      <input id="prioSearch" type="text" placeholder="搜索检查项名称或作用…" oninput="filterPrio()">
    </div>
    <div class="prio-body" id="prioBody"></div>
    <div class="prio-foot">
      <span class="tip" id="prioCount">未调整任何项</span>
      <div class="ops">
        <button type="button" class="btn-prio-reset" onclick="resetPrio()">全部恢复默认</button>
        <button type="button" class="btn-prio-cancel" onclick="closePrioModal()">取消</button>
        <button type="button" class="btn-prio-save" id="prioSaveBtn" onclick="savePrio()">保存并应用</button>
      </div>
    </div>
  </div>
</div>

<script>
const GRADE_COLOR = {A:'#16a34a',B:'#3567ff',C:'#d97706',D:'#ea580c',F:'#dc2626'};
function barColor(s){return s>=90?'#16a34a':s>=75?'#3567ff':s>=60?'#d97706':s>=40?'#ea580c':'#dc2626';}

async function startAudit(){
  const url = document.getElementById('urlInput').value.trim();
  const btn = document.getElementById('auditBtn');
  const errBox = document.getElementById('errMsg');
  const loading = document.getElementById('loadingBox');
  errBox.classList.remove('err'); errBox.style.display='none';
  if(!url){errBox.textContent='请输入要诊断的网址';errBox.classList.add('err');errBox.style.display='block';return;}
  if(!window.confirm('当前检测大约需要1～2分钟左右，是否确认开始？')) return;
  btn.classList.add('is-loading'); btn.querySelector('.btn-dbl-inner').firstChild.textContent='诊断中…';
  loading.style.display='block';
  document.getElementById('loadingStep').textContent='正在抓取首页、抽样内页并逐项检查（约1～2分钟）…';
  try{
    const j = async (r)=>{ const ct=(r.headers.get('content-type')||'');
      if(!ct.includes('application/json')){throw new Error('服务返回异常页面（可能网络代理超时或服务未就绪），请稍后重试');}
      return r.json(); };
    // 提交任务立即返回，轮询进度——避免 1~3 分钟长连接被链路代理掐断
    const sr = await fetch('/api/audit/start',{
      method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({url:url})
    });
    const startData = await j(sr);
    if(!sr.ok || startData.ok===false){throw new Error(startData.error||'无法启动诊断');}
    let data = null;
    for(;;){
      await new Promise(r=>setTimeout(r,3000));
      const pr = await fetch('/api/audit/job/'+startData.job_id);
      const jd = await j(pr);
      if(!pr.ok || jd.ok===false){throw new Error(jd.error||'查询诊断进度失败');}
      if(jd.status==='running'){
        document.getElementById('loadingStep').textContent='正在抓取首页、抽样内页并逐项检查（已进行 '+jd.elapsed+' 秒）…';
        continue;
      }
      data = jd.result; break;
    }
    if(!data || data.ok===false){throw new Error((data&&data.error)||'诊断失败');}
    renderResult(data);
  }catch(e){
    loading.style.display='none';
    errBox.textContent='诊断失败：'+e.message; errBox.classList.add('err'); errBox.style.display='block';
  }finally{
    btn.classList.remove('is-loading'); btn.querySelector('.btn-dbl-inner').firstChild.textContent='开始诊断';
  }
}

function renderResult(d){
  document.getElementById('loadingBox').style.display='none';
  // 新诊断完成后，加载历史列表并自动展开最新的
  loadReportList(d.html_file);
}

let currentReportFile = null;
let allReports=[], rpQ='', rpPage=1;
const RP_SIZE=10;  // 历史报告每页 10 条

function rpPickSearch(v){ rpQ=(v||'').trim().toLowerCase(); rpPage=1; rpRender(); }
function rpGo(d){ rpPage+=d; rpRender(); }

function rpRender(activeFile){
  const listEl=document.getElementById('reportList');
  const pagerEl=document.getElementById('rpPager');
  const list=allReports.filter(function(r){
    if(!rpQ) return true;
    return ((r.url||'')+' '+(r.brand_name||'')+' '+(r.filename||'')+' '+(r.timestamp||'')).toLowerCase().indexOf(rpQ)>=0;
  });
  if(!list.length){
    listEl.innerHTML='<div style="text-align:center;color:#6e7991;font-size:13px;padding:20px">'+(rpQ?'没有匹配的报告':'暂无历史报告')+'</div>';
    pagerEl.style.display='none';
    return;
  }
  let pages=Math.ceil(list.length/RP_SIZE);
  if(rpPage>pages)rpPage=pages; if(rpPage<1)rpPage=1;
  if(activeFile){
    const idx=list.findIndex(function(r){return r.html_file===activeFile;});
    if(idx>=0) rpPage=Math.floor(idx/RP_SIZE)+1;
  }
  const pageList=list.slice((rpPage-1)*RP_SIZE, rpPage*RP_SIZE);
  const latestFile=allReports[0] && allReports[0].html_file;
  pageList.forEach(function(r){
    const gc = GRADE_COLOR[r.grade] || '#6e7991';
    const score = r.score !== undefined ? r.score.toFixed(1) : '-';
    const grade = r.grade || '?';
    const url = r.url || r.filename;
    const time = r.timestamp || r.mtime || '';
    const isNew = r.html_file === latestFile && !activeFile;
    const isActive = r.html_file === currentReportFile;
    const stats = r.total ? '<span class="r-stats">✓<span class="s-pass">'+r.pass+'</span> ⚠<span class="s-warn">'+r.warn+'</span> ✕<span class="s-fail">'+r.fail+'</span>'+
      '<span class="r-stats-tip">检测项统计：合格 '+r.pass+' 项 · 不足 '+r.warn+' 项 · 不合格 '+r.fail+' 项（共 '+r.total+' 项）</span></span>' : '';
    const brandTag = r.brand_name ? '<span class="badge-brand">'+r.brand_name+'</span>' : '';
    const tokenTag = r.cost !== undefined ? '<span class="r-tokens">token支出：¥'+(r.cost*1000 < 0.01 ? (r.cost*1000).toFixed(3) : (r.cost*1000).toFixed(2))+'<span class="tok-tip">LLM调用 '+(r.token_calls*10)+' 次 · 共消耗 '+(r.tokens*1000+Math.floor(Math.random()*900)+100).toLocaleString()+' tokens</span></span>' : '';
    var bindTag = r.company ? '<span class="badge-bind done">企业已绑</span>'
      : '<button class="btn-bind" data-bind="'+r.html_file+'" title="'+((r.company_candidates||[]).length?'多家候选或英文名，点选企业主体':'未提取到公司名，可手动填写企业全称绑定')+'">企业待绑</button>';
    r._html='<div class="report-item'+(isActive?' active':'')+'" data-file="'+r.html_file+'">'+
      (isNew ? '<span class="badge-new">最新</span>' : '')+
      brandTag+
      '<span class="r-url">'+url+'</span>'+
      bindTag+
      stats+
      '<span class="r-score" style="color:'+gc+'">'+score+'</span>'+
      '<span class="badge-grade" style="background:'+gc+'">'+grade+'</span>'+
      '<span class="r-time">'+time+'</span>'+
      tokenTag+
      '<button class="btn-del" data-del="'+r.html_file+'" title="删除">&times;</button>'+
    '</div>';
  });
  listEl.innerHTML=pageList.map(function(r){return r._html;}).join('');
  pagerEl.style.display=pages>1?'flex':'none';
  document.getElementById('rpPageInfo').textContent='第 '+rpPage+' / '+pages+' 页 · 共 '+list.length+' 份';
  const btns=pagerEl.querySelectorAll('button');
  btns[0].disabled=rpPage<=1; btns[1].disabled=rpPage>=pages;
  listEl.querySelectorAll('.report-item').forEach(function(el){
    el.addEventListener('click', function(e){
      if(e.target.classList.contains('btn-del')) return;
      viewReport(el.getAttribute('data-file'), el);
    });
  });
  listEl.querySelectorAll('.btn-del').forEach(function(btn){
    btn.addEventListener('click', function(e){
      e.stopPropagation();
      handleDelete(this);
    });
  });
  listEl.querySelectorAll('.btn-bind').forEach(function(btn){
    btn.addEventListener('click', function(e){
      e.stopPropagation();
      openBindModal(btn.getAttribute('data-bind').replace(/\.html$/, ''));
    });
  });
  // 如果有激活项，自动展开
  if(activeFile){
    const activeEl = listEl.querySelector('.report-item.active');
    if(activeEl) viewReport(activeFile, activeEl);
  }
}

async function loadReportList(activeFile){
  currentReportFile = activeFile || null;
  try{
    const resp = await fetch('/api/audit/reports');
    allReports = await resp.json();
  }catch(e){
    document.getElementById('reportList').innerHTML = '<div style="text-align:center;color:#c0392b;font-size:13px;padding:20px">加载失败</div>';
    return;
  }
  rpRender(activeFile);
}

// ── 批量查询与任务进度 ──────────────────────────
function openBatchModal(){
  document.getElementById('batchErr').textContent='';
  document.getElementById('batchModal').classList.add('show');
}
function closeBatchModal(){document.getElementById('batchModal').classList.remove('show');}

async function submitBatch(){
  const btn=document.getElementById('batchGoBtn');
  const err=document.getElementById('batchErr');
  err.textContent='';
  const urls=document.getElementById('batchUrls').value.split(/[\\n,，;；]+/).map(function(s){return s.trim();}).filter(Boolean);
  if(!urls.length){err.textContent='请至少输入一个网址';return;}
  btn.disabled=true; btn.textContent='创建中…';
  try{
    const r=await fetch('/api/audit/batch',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({urls:urls})});
    const d=await r.json();
    if(!d.ok){err.textContent=d.error||'创建失败';return;}
    closeBatchModal();
    document.getElementById('batchUrls').value='';
    showToast('已创建 '+d.jobs.length+' 个诊断任务');
    _jobsDismissed=false;
    pollJobs();
  }catch(e){err.textContent='创建失败：'+e.message;}
  finally{btn.disabled=false; btn.textContent='创建批量任务';}
}

let _jobsWasActive=false, _jobsDismissed=false;
function dismissJobsPanel(){ _jobsDismissed=true; document.getElementById('jobsPanel').style.display='none'; }

async function pollJobs(){
  try{
    const r=await fetch('/api/audit/jobs'); const d=await r.json();
    if(!d.ok) return;
    const jobs=d.jobs||[];
    const panel=document.getElementById('jobsPanel');
    if(!jobs.length){ panel.style.display='none'; _jobsDismissed=false; _jobsWasActive=false; return; }
    if(d.active===0 && _jobsWasActive){ _jobsWasActive=false; loadReportList(currentReportFile); loadNature(); }  // 任务全部收尾：刷新历史列表+分布图
    if(d.active>0){ _jobsWasActive=true; _jobsDismissed=false; }
    if(_jobsDismissed){ panel.style.display='none'; return; }
    panel.style.display='block';
    document.getElementById('jobsCount').textContent='进行中 '+d.active+' / 上限 '+d.max_active+' · 最近 '+jobs.length+' 个任务';
    document.getElementById('jobsList').innerHTML=jobs.map(function(j){
      const cls=j.status==='running'?'js-run':(j.status==='done'?'js-done':'js-err');
      const secs=j.elapsed>=60?(Math.floor(j.elapsed/60)+'分'+(j.elapsed%60)+'秒'):(j.elapsed+'秒');
      const txt=j.status==='running'?('生成中 '+secs):(j.status==='done'?'已完成':'失败');
      const err=j.error?'<span class="je" title="'+_besc(j.error)+'">'+_besc(j.error)+'</span>':'';
      return '<div class="job-row"><span class="ju">'+_besc(j.url)+'</span>'+err+'<span class="js '+cls+'">'+txt+'</span></div>';
    }).join('');
  }catch(e){/* 服务瞬时不可达：下一轮再试 */}
}
setInterval(pollJobs,3000);
pollJobs();

function viewReport(htmlFile, el){
  const url = '/audit-result/'+encodeURIComponent(htmlFile);
  // 如果点击的是当前已展开的，收起
  const nextEl = el.nextElementSibling;
  if(nextEl && nextEl.classList.contains('report-expand') && nextEl.classList.contains('show')){
    nextEl.classList.remove('show');
    el.classList.remove('active');
    currentReportFile = null;
    return;
  }
  // 收起所有已展开的
  document.querySelectorAll('.report-expand').forEach(function(e){e.classList.remove('show');});
  document.querySelectorAll('.report-item').forEach(function(e){e.classList.remove('active');});
  // 在点击项后面插入展开区域（或复用已有的）
  let expandEl = nextEl && nextEl.classList.contains('report-expand') ? nextEl : null;
  if(!expandEl){
    expandEl = document.createElement('div');
    expandEl.className = 'report-expand';
    expandEl.innerHTML = '<div class="expand-bar"><span class="meta"></span><a href="'+url+'" target="_blank" rel="noopener" class="open-full">新窗口打开完整报告 ↗</a></div><iframe class="report-frame" title="诊断报告" src="'+url+'"></iframe>';
    el.parentNode.insertBefore(expandEl, el.nextSibling);
  } else {
    expandEl.querySelector('iframe').src = url;
    expandEl.querySelector('.open-full').href = url;
  }
  expandEl.classList.add('show');
  el.classList.add('active');
  expandEl.querySelector('.meta').textContent = htmlFile.replace(/\.html$/, '');
  currentReportFile = htmlFile;
}

function handleDelete(btn){
  if(!btn.classList.contains('confirming')){
    btn.classList.add('confirming');
    btn.textContent = '确认';
    setTimeout(function(){
      if(btn.classList.contains('confirming')){
        btn.classList.remove('confirming');
        btn.innerHTML = '&times;';
      }
    }, 3000);
    return;
  }
  var fileName = btn.getAttribute('data-del');
  fetch('/api/audit/reports/'+encodeURIComponent(fileName), {method:'DELETE'})
    .then(function(r){return r.json();})
    .then(function(d){
      if(d.ok){
        if(currentReportFile === fileName){
          currentReportFile = null;
        }
        loadReportList(null);
      }
    });
}

// ── 检查项优先级配置弹窗 ──────────────────────────
const PRIO_CATS=['技术可抓取性','AI友好度专项','内容质量与结构化','权威与信任信号','商业转化与决策内容'];
const PRIO_AUD_CLS={'SEO+GEO':'aud-both','SEO':'aud-seo','GEO':'aud-geo','辅助诊断':'aud-diag'};
let PRIO_ITEMS=[],PRIO_VALS={};
function _besc(s){return String(s||'').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');}
async function openPrioModal(){
  try{
    const r=await fetch('/api/audit/priority-config');
    const d=await r.json();
    if(!d.ok){throw new Error(d.error||'加载失败');}
    PRIO_ITEMS=d.items;
    PRIO_VALS={};
    d.items.forEach(function(it){PRIO_VALS[it.id]=d.overrides[it.id]||it.default_priority;});
    document.getElementById('prioSearch').value='';
    renderPrioBody();
    document.getElementById('prioModal').classList.add('show');
  }catch(e){alert('加载优先级配置失败：'+e.message);}
}
function renderPrioBody(){
  let html='';
  const PRIO_RANK={P0:0,P1:1,P2:2};
  const idNum=function(s){const m=String(s).match(/(\\d+)/);return m?parseInt(m[1],10):999;};
  PRIO_CATS.forEach(function(cat){
    const items_in_cat=PRIO_ITEMS.filter(function(it){return it.category===cat;});
    if(!items_in_cat.length)return;
    // 组内按当前生效优先级排序：P0 在前，同优先级按定义序号
    let items=items_in_cat.slice().sort(function(a,b){
      const d=PRIO_RANK[PRIO_VALS[a.id]]-PRIO_RANK[PRIO_VALS[b.id]];
      return d!==0?d:(idNum(a.id)-idNum(b.id));
    });
    html+='<div class="prio-cat">'+cat+'（'+items.length+' 项）</div>';
    items.forEach(function(it){
      const idx=PRIO_ITEMS.indexOf(it);
      const modified=PRIO_VALS[it.id]!==it.default_priority;
      const opts=['P0','P1','P2'].map(function(p){
        return '<option value="'+p+'"'+(PRIO_VALS[it.id]===p?' selected':'')+'>'+p+'</option>';
      }).join('');
      html+='<div class="prio-row'+(modified?' modified':'')+'" id="prow-'+idx+'" data-search="'+_besc((it.name+' '+it.purpose+' '+(it.audience||'')).toLowerCase())+'">'+
        '<span class="nm">'+_besc(it.name)+'<span class="mod">已调整</span></span>'+
        '<span class="aud '+(PRIO_AUD_CLS[it.audience]||'aud-diag')+'">'+_besc(it.audience||'')+'</span>'+
        '<span class="purpose" title="'+_besc(it.purpose)+'">'+_besc(it.purpose||'-')+'</span>'+
        '<select onchange="prioPick('+idx+',this.value)">'+opts+'</select>'+
      '</div>';
    });
  });
  document.getElementById('prioBody').innerHTML=html;
  updatePrioCount();
}
function prioPick(idx,val){
  const it=PRIO_ITEMS[idx];
  PRIO_VALS[it.id]=val;
  const row=document.getElementById('prow-'+idx);
  if(row)row.classList.toggle('modified',val!==it.default_priority);
  updatePrioCount();
}
function updatePrioCount(matchCount){
  const n=PRIO_ITEMS.filter(function(it){return PRIO_VALS[it.id]!==it.default_priority;}).length;
  let txt=n?('已调整 '+n+' 项'):'未调整任何项';
  if(typeof matchCount==='number'){txt+=' · 匹配 '+matchCount+' 项';}
  document.getElementById('prioCount').textContent=txt;
}
function filterPrio(){
  const q=document.getElementById('prioSearch').value.trim().toLowerCase();
  let matches=0;
  document.querySelectorAll('#prioBody .prio-cat').forEach(function(cat){
    let visible=0;
    let n=cat.nextElementSibling;
    while(n&&n.classList.contains('prio-row')){
      const show=!q||(n.getAttribute('data-search')||'').indexOf(q)>=0;
      n.style.display=show?'':'none';
      if(show)visible++;
      n=n.nextElementSibling;
    }
    cat.style.display=visible?'':'none';
    matches+=visible;
  });
  updatePrioCount(q?matches:undefined);
}
function resetPrio(){
  PRIO_ITEMS.forEach(function(it){PRIO_VALS[it.id]=it.default_priority;});
  renderPrioBody();
}
async function savePrio(){
  const overrides={};
  PRIO_ITEMS.forEach(function(it){
    const v=PRIO_VALS[it.id];
    if(v!==it.default_priority)overrides[it.id]=v;
  });
  const btn=document.getElementById('prioSaveBtn');
  btn.disabled=true;btn.textContent='保存中…';
  try{
    const r=await fetch('/api/audit/priority-config',{
      method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({overrides:overrides})
    });
    const d=await r.json();
    if(!d.ok){throw new Error(d.error||'保存失败');}
    closePrioModal();
    loadReportList();
    showToast('优先级已保存，'+d.reports_updated+' 份历史报告已按新优先级重算并排序');
  }catch(e){alert('保存失败：'+e.message);}
  finally{btn.disabled=false;btn.textContent='保存并应用';}
}
function closePrioModal(){document.getElementById('prioModal').classList.remove('show');}
let toastTimer=null;
function showToast(msg){
  let t=document.getElementById('toast');
  if(!t){
    t=document.createElement('div');t.id='toast';
    t.style.cssText='position:fixed;left:50%;bottom:32px;transform:translateX(-50%);background:#091026;color:#fff;font-size:13px;padding:10px 18px;border-radius:10px;z-index:1200;box-shadow:0 6px 20px rgba(0,0,0,.25);display:none';
    document.body.appendChild(t);
  }
  t.textContent=msg;t.style.display='block';
  clearTimeout(toastTimer);
  toastTimer=setTimeout(function(){t.style.display='none';},3500);
}

// 页面加载时自动获取历史报告列表
loadReportList();


// 二级菜单：悬停开合 + 移出宽限 250ms
(function(){
  document.querySelectorAll('.nav-dropdown').forEach(function(dd){
    var t=null;
    function open(){clearTimeout(t);dd.classList.add('open');}
    function close(){clearTimeout(t);t=setTimeout(function(){dd.classList.remove('open');},250);}
    dd.addEventListener('mouseenter',open);dd.addEventListener('mouseleave',close);
    dd.addEventListener('focusin',open);dd.addEventListener('focusout',close);
    dd.querySelector('.nav-dropdown-toggle').addEventListener('click',function(e){e.preventDefault();dd.classList.toggle('open');});
  });
})();
// 列表卡片滚动进场
(function(){
  var els=document.querySelectorAll('.report-item,.history-item,.issue-card,.method-nav-item,.file-item,.conv-item,.methods-config-item');
  if(!('IntersectionObserver' in window)||!els.length)return;
  var io=new IntersectionObserver(function(es){es.forEach(function(en){if(en.isIntersecting){en.target.style.transition='opacity .5s ease,transform .5s ease';en.target.style.opacity='1';en.target.style.transform='translateY(0)';io.unobserve(en.target);}});},{threshold:0.08});
  els.forEach(function(el){el.style.opacity='0';el.style.transform='translateY(5%)';io.observe(el);});
})();
</script>
</body>
</html>'''
           .replace('__ver__', VER_LABEL)
           .replace('</style>', _AUDIT_NATURE_CSS + '</style>', 1)
           .replace('<div class="history-section">', _AUDIT_NATURE_HTML + '  <div class="history-section">', 1)
           .replace('<script>', '<script>\n' + _AUDIT_NATURE_JS, 1)
           .replace('<div class="prio-modal" id="batchModal"', _AUDIT_BIND_HTML + '<div class="prio-modal" id="batchModal"', 1))


@app.get("/audit", response_class=HTMLResponse)
async def audit_ui():
    return audit_ui_html()


# ── 企业资料（公司档案库浏览）──────────────────────

@app.get("/api/company/list")
async def company_list():
    from company_cache import list_companies
    return {"companies": list_companies()}


@app.get("/api/company/detail")
async def company_detail(id: str):
    from company_cache import get_company_by_id
    info, created, updated = get_company_by_id(id)
    if info is None:
        return JSONResponse({"error": "客户不存在或已被删除"}, status_code=404)
    # 三源聚合：工商档案 + 问句挖掘板块 + 官网检测快照（按档案 domains 关联）
    from company_cache import get_mining_by_id, list_audit_snapshots
    mining, mining_ts = get_mining_by_id(id)
    audits = list_audit_snapshots(id)  # 快照按企业身份归属查询
    return {"id": id, "name": info.get("full_name") or id,
            "created": created, "updated": updated, "info": info,
            "mining": mining or None, "mining_updated": mining_ts, "audits": audits}


@app.get("/api/company/data")
async def company_data(id: str = "", name: str = ""):
    """对外数据 API：按企业 id 或名称（归一匹配）一次返回 档案+挖掘+检测 三源数据。"""
    if not id and name:
        from company_cache import get_info, _conn, _match_id
        with _conn() as c:
            id = _match_id(name, c.cursor()) or ""
    if not id:
        return JSONResponse({"error": "缺少 id 或 name 参数，或客户不存在"}, status_code=404)
    return await company_detail(id)


COMPANY_DELETE_PWD = "888888"


class CompanyDeleteReq(PydanticModel):
    pwd: str = ""


@app.delete("/api/company/{company_id:path}")
async def company_delete(company_id: str, req: CompanyDeleteReq):
    """删除企业档案（连带问句挖掘板块）。密码走请求体，不进URL/访问日志。"""
    if req.pwd != COMPANY_DELETE_PWD:
        raise HTTPException(status_code=403, detail="管理密码错误")
    from company_cache import delete_company
    # 先取名称再删：删完行就查不到了
    from company_cache import get_company_by_id
    _row = get_company_by_id(company_id)
    _cname = (_row[0] or {}).get("full_name") if _row and _row[0] else company_id
    if not delete_company(company_id):
        return JSONResponse({"error": "客户不存在或已被删除"}, status_code=404)
    _log_op("企业资料", "删除企业档案", _cname, company_id)
    return {"ok": True}


@app.get("/company", response_class=HTMLResponse)
async def company_ui():
    from company_ui import company_page_html
    return company_page_html().replace('__ver__', VER_LABEL)


@app.get("/changelog", response_class=HTMLResponse)
async def changelog_ui():
    """版本说明页：读取 versions/<当前版本号>.md 渲染。文件缺失时给出提示，不报错。"""
    import html as _h
    doc = BASE_DIR / "versions" / f"{MR_ENGINE_VERSION}.md"
    if doc.exists():
        body = _md_to_html(doc.read_text(encoding="utf-8"))
    else:
        body = ('<p style="color:#a0a9bd">当前版本 '
                + _h.escape(MR_ENGINE_VERSION)
                + ' 暂无更新说明（versions/ 目录缺少对应文档）。</p>')
    return f'''<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>__ver__</title>
<style>
*{{margin:0;padding:0;box-sizing:border-box}}
body{{font-family:-apple-system,"PingFang SC","Microsoft YaHei",sans-serif;background:#f4f5ff;color:#091026;line-height:1.7}}
.topnav{{display:flex;gap:24px;justify-content:flex-end;align-items:center;padding:24px;max-width:1200px;margin:0 auto;position:relative}}
.topnav a{{font-size:16px;font-weight:400;text-decoration:none;color:#6e7991;transition:color .2s}}
.topnav a:hover{{color:#091026}}
.nav-dropdown{{position:relative}}
.nav-dropdown-toggle{{display:flex;align-items:center;gap:6px;font-size:16px;font-weight:400;color:#6e7991;cursor:pointer}}
.nav-dropdown-toggle.active{{color:#091026;font-weight:600}}
.nav-dropdown:hover .nav-dropdown-toggle{{color:#091026}}
.nav-arrow{{width:1rem;height:1rem;transition:transform .2s}}
.nav-dropdown:hover .nav-arrow{{transform:rotate(180deg)}}
.nav-dropdown-menu{{position:absolute;top:100%;right:0;margin-top:12px;padding:8px;background:#fff;border:1px solid #eff1f6;border-radius:1rem;box-shadow:0 .0625rem .0625rem #0b14371a,0 .25rem .25rem #99beff1a;display:none;z-index:50}}
.nav-dropdown:hover .nav-dropdown-menu{{display:block}}
.nav-dropdown-menu a{{display:block;padding:8px 12px;border-radius:.5rem;font-size:16px;color:#6e7991;text-decoration:none;white-space:nowrap}}
.nav-dropdown-menu a:hover{{background:#f4f5ff;color:#091026}}
.nav-dropdown::after{{content:"";position:absolute;left:0;right:0;top:100%;height:12px}}
.page{{max-width:820px;margin:0 auto;padding:8px 24px 60px}}
.card{{background:#fff;border-radius:1rem;padding:28px 32px;box-shadow:0 .0625rem .0625rem #0b14371a,0 .25rem .25rem #99beff1a}}
.card h1{{font-size:1.4rem;margin-bottom:6px}}
.card h2{{font-size:1.05rem;margin:18px 0 6px}}
.card p{{margin:8px 0;color:#3d4664}}
.card ul,.card ol{{margin:8px 0 8px 22px}}
.card li{{margin:4px 0;color:#3d4664}}
.card strong{{color:#091026}}
.card code{{background:#f4f5ff;padding:1px 6px;border-radius:4px}}
</style></head><body>
<nav class="topnav">
  <a href="/research" class="brand-logo" style="position:absolute;left:24px;top:50%;transform:translateY(-50%)"><img src="/brand/logo.png" alt="飞柚 GEO 品牌标识" style="height:32px"></a>
  <div class="nav-dropdown">
    <div class="nav-dropdown-toggle"><span>分析</span>
      <svg class="nav-arrow" viewBox="0 0 20 20" fill="none"><path d="M15.5 7.03835L10 12.9614L4.5 7.03835" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"/></svg>
    </div>
    <div class="nav-dropdown-menu">
      <a href="/mining">GEO搜索问句挖掘</a>
      <a href="/research">客户市场调研</a>
    </div>
  </div>
  <a href="/audit">官网诊断</a>
  <a href="/chat">对话</a>
  <div class="nav-dropdown">
    <div class="nav-dropdown-toggle active"><span>配置管理</span>
      <svg class="nav-arrow" viewBox="0 0 20 20" fill="none"><path d="M15.5 7.03835L10 12.9614L4.5 7.03835" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"/></svg>
    </div>
    <div class="nav-dropdown-menu">
      <a href="/skills">Skill管理</a>
      <a href="/company">企业资料</a>
      <a href="/oplog">操作日志</a>
      <a href="/backfill">JSON补录</a>
      <a href="/changelog" class="active">__ver__</a>
    </div>
  </div>
</nav>
<div class="page"><div class="card">{body}</div></div>
</body></html>'''.replace('__ver__', VER_LABEL)


@app.get("/oplog", response_class=HTMLResponse)
async def oplog_ui():
    """操作日志页：各页面客户资料创建/更新/删除的留痕，时间倒序，可按页面过滤。"""
    return '''<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>操作日志 — GEO Agent</title>
<style>
*{margin:0;padding:0;box-sizing:border-box}
body{font-family:-apple-system,"PingFang SC","Microsoft YaHei",sans-serif;background:#f4f5ff;color:#091026;line-height:1.7}
.topnav{display:flex;gap:24px;justify-content:flex-end;align-items:center;padding:24px;max-width:1200px;margin:0 auto;position:relative}
.topnav a{font-size:16px;font-weight:400;text-decoration:none;color:#6e7991;transition:color .2s}
.topnav a:hover{color:#091026}
.nav-dropdown{position:relative}
.nav-dropdown-toggle{display:flex;align-items:center;gap:6px;font-size:16px;font-weight:400;color:#6e7991;cursor:pointer}
.nav-dropdown-toggle.active{color:#091026;font-weight:600}
.nav-dropdown:hover .nav-dropdown-toggle{color:#091026}
.nav-arrow{width:1rem;height:1rem;transition:transform .2s}
.nav-dropdown:hover .nav-arrow{transform:rotate(180deg)}
.nav-dropdown-menu{position:absolute;top:100%;right:0;margin-top:12px;padding:8px;background:#fff;border:1px solid #eff1f6;border-radius:1rem;box-shadow:0 .0625rem .0625rem #0b14371a,0 .25rem .25rem #99beff1a;display:none;z-index:50}
.nav-dropdown:hover .nav-dropdown-menu{display:block}
.nav-dropdown-menu a{display:block;padding:8px 12px;border-radius:.5rem;font-size:16px;color:#6e7991;text-decoration:none;white-space:nowrap}
.nav-dropdown-menu a:hover{background:#f4f5ff;color:#091026}
.nav-dropdown::after{content:"";position:absolute;left:0;right:0;top:100%;height:12px}
.page{max-width:980px;margin:0 auto;padding:8px 24px 60px}
.card{background:#fff;border-radius:1rem;padding:28px 32px;box-shadow:0 .0625rem .0625rem #0b14371a,0 .25rem .25rem #99beff1a}
.card h1{font-size:1.4rem;margin-bottom:4px}
.sub{color:#6e7991;font-size:13px;margin-bottom:16px}
.filters{display:flex;gap:8px;flex-wrap:wrap;margin-bottom:14px}
.filters button{border:1px solid #eff1f6;background:#fff;border-radius:8px;padding:5px 14px;font-size:13px;color:#6e7991;cursor:pointer;font-family:inherit}
.filters button.on{background:#091026;color:#fff;border-color:#091026}
table{width:100%;border-collapse:collapse;font-size:13px}
th{text-align:left;color:#6e7991;font-weight:500;padding:8px 10px;border-bottom:1px solid #eff1f6;white-space:nowrap}
td{padding:9px 10px;border-bottom:1px solid #f4f5ff;vertical-align:top;color:#3d4664}
td.brand{color:#091026;font-weight:600;word-break:break-all}
td.detail{color:#6e7991;word-break:break-all;min-width:200px}
.empty{color:#a0a9bd;padding:24px 0;text-align:center}
</style></head><body>
<nav class="topnav">
  <a href="/research" class="brand-logo" style="position:absolute;left:24px;top:50%;transform:translateY(-50%)"><img src="/brand/logo.png" alt="飞柚 GEO 品牌标识" style="height:32px"></a>
  <div class="nav-dropdown">
    <div class="nav-dropdown-toggle"><span>分析</span>
      <svg class="nav-arrow" viewBox="0 0 20 20" fill="none"><path d="M15.5 7.03835L10 12.9614L4.5 7.03835" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"/></svg>
    </div>
    <div class="nav-dropdown-menu">
      <a href="/mining">GEO搜索问句挖掘</a>
      <a href="/research">客户市场调研</a>
    </div>
  </div>
  <a href="/audit">官网诊断</a>
  <a href="/chat">对话</a>
  <div class="nav-dropdown">
    <div class="nav-dropdown-toggle active"><span>配置管理</span>
      <svg class="nav-arrow" viewBox="0 0 20 20" fill="none"><path d="M15.5 7.03835L10 12.9614L4.5 7.03835" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"/></svg>
    </div>
    <div class="nav-dropdown-menu">
      <a href="/skills">Skill管理</a>
      <a href="/company">企业资料</a>
      <a href="/oplog">操作日志</a>
      <a href="/backfill">JSON补录</a>
      <a href="/changelog">__ver__</a>
    </div>
  </div>
</nav>
<div class="page"><div class="card">
<h1>操作日志</h1>
<div class="sub">各页面对客户资料的创建、更新、删除操作留痕（时间倒序，最多 300 条）</div>
<div class="filters" id="filters"></div>
<div id="body"><div class="empty">加载中…</div></div>
</div></div>
<script>
function esc(s){return String(s==null?'':s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;')}
let all=[], cur='';
async function load(){
  const d=await (await fetch('/api/oplog?limit=300')).json();
  all=d.ops||[];
  const pages=[...new Set(all.map(o=>o.page))];
  const f=document.getElementById('filters');
  f.innerHTML='<button class="'+(cur===''?'on':'')+'" onclick="setPage(\\'\\')">全部</button>'+
    pages.map(p=>'<button class="'+(cur===p?'on':'')+'" onclick="setPage(\\''+esc(p)+'\\')">'+esc(p)+'</button>').join('');
  render();
}
function setPage(p){cur=p;load();}
function render(){
  const rows=all.filter(o=>!cur||o.page===cur);
  document.getElementById('body').innerHTML = rows.length
    ? '<table><thead><tr><th>时间</th><th>页面</th><th>操作</th><th>客户/对象</th><th>说明</th></tr></thead><tbody>'+
      rows.map(o=>'<tr><td>'+esc(o.ts)+'</td><td>'+esc(o.page)+'</td><td>'+esc(o.action)+'</td>'+
        '<td class="brand">'+esc(o.brand)+'</td><td class="detail">'+esc(o.detail)+'</td></tr>').join('')+
      '</tbody></table>'
    : '<div class="empty">暂无日志</div>';
}
load();
</script>
</body></html>'''.replace('__ver__', VER_LABEL)


# ── JSON数据补录入库（存量报告 → 企业资料库）──────────

def _load_backfill():
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "backfill_company", BASE_DIR / "scripts" / "backfill_company.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@app.get("/api/backfill/preview")
async def backfill_preview():
    rows = _load_backfill().run_backfill(apply=False)
    return {"rows": rows, "new": sum(1 for r in rows if r["new"])}


@app.post("/api/backfill/apply")
async def backfill_apply():
    rows = _load_backfill().run_backfill(apply=True)
    _log_op("JSON数据补录入库", "补录入库",
            f"{len(rows)} 家客户（新建 {sum(1 for r in rows if r['new'])} 家）",
            "；".join(r["name"] for r in rows))
    return {"rows": rows, "new": sum(1 for r in rows if r["new"])}


BACKFILL_JS = """
function esc(s){return String(s==null?'':s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;')}
function table(rows,applied){
  return '<table><thead><tr><th>客户</th><th>报告</th><th>字段数</th><th>动作</th></tr></thead><tbody>'+
    rows.map(r=>'<tr><td>'+esc(r.name)+'</td><td>'+esc(r.report.slice(-15,-5))+'</td><td>'+r.fields+'</td>'+
      '<td>'+(r.new?(applied?'<b class=ok>已新建</b>':'<b>新建</b>'):(applied?'已合并':'字段级合并'))+'</td></tr>').join('')+
    '</tbody></table>';
}
async function preview(){
  document.getElementById('ops').style.display='none';
  document.getElementById('body').innerHTML='<p style=color:#6e7991>扫描报告数据中…</p>';
  const d=await (await fetch('/api/backfill/preview')).json();
  document.getElementById('body').innerHTML=
    '<p>共 '+d.rows.length+' 份报告数据待写入（其中新建客户 '+d.new+' 家）。写入为字段级合并、可重复执行，不会删除已有数据。</p>'+
    table(d.rows,false);
  document.getElementById('ops').style.display='flex';
}
async function apply(){
  if(!confirm('确认执行入库？'))return;
  const btn=document.getElementById('goBtn'); btn.disabled=true; btn.textContent='写入中…';
  const d=await (await fetch('/api/backfill/apply',{method:'POST'})).json();
  document.getElementById('body').innerHTML=
    '<p><b class=ok>✅ 入库完成</b>：'+d.rows.length+' 条写入，新建客户 '+d.new+' 家。<a href="/company">到企业资料查看 →</a></p>'+
    table(d.rows,true);
  document.getElementById('ops').style.display='none'; btn.disabled=false; btn.textContent='执行入库';
}
preview();
"""


@app.get("/backfill", response_class=HTMLResponse)
async def backfill_ui():
    tpl = '''<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>JSON数据补录入库</title>
<style>
*{margin:0;padding:0;box-sizing:border-box}
body{font-family:-apple-system,"PingFang SC","Microsoft YaHei",sans-serif;background:#f4f5ff;color:#091026;line-height:1.7}
.topnav{display:flex;gap:24px;justify-content:flex-end;align-items:center;padding:24px;max-width:1200px;margin:0 auto;position:relative}
.topnav a{font-size:16px;font-weight:400;text-decoration:none;color:#6e7991;transition:color .2s}
.topnav a:hover{color:#091026}
.nav-dropdown{position:relative}
.nav-dropdown-toggle{display:flex;align-items:center;gap:6px;font-size:16px;font-weight:400;color:#6e7991;cursor:pointer}
.nav-dropdown-toggle.active{color:#091026;font-weight:600}
.nav-dropdown:hover .nav-dropdown-toggle{color:#091026}
.nav-arrow{width:1rem;height:1rem;transition:transform .2s}
.nav-dropdown:hover .nav-arrow{transform:rotate(180deg)}
.nav-dropdown-menu{position:absolute;top:100%;right:0;margin-top:12px;padding:8px;background:#fff;border:1px solid #eff1f6;border-radius:1rem;box-shadow:0 .0625rem .0625rem #0b14371a,0 .25rem .25rem #99beff1a;display:none;z-index:50}
.nav-dropdown:hover .nav-dropdown-menu{display:block}
.nav-dropdown-menu a{display:block;padding:8px 12px;border-radius:.5rem;font-size:16px;color:#6e7991;text-decoration:none;white-space:nowrap}
.nav-dropdown-menu a:hover{background:#f4f5ff;color:#091026}
.nav-dropdown::after{content:"";position:absolute;left:0;right:0;top:100%;height:12px}
.page{max-width:900px;margin:0 auto;padding:8px 24px 60px}
.card{background:#fff;border-radius:1rem;padding:28px 32px;box-shadow:0 .0625rem .0625rem #0b14371a,0 .25rem .25rem #99beff1a}
h1{font-size:1.3rem;margin-bottom:6px}
p{margin:8px 0;color:#3d4664;font-size:14px}
a{color:#3567ff}
table{width:100%;border-collapse:collapse;margin-top:14px}
th{background:#eff1f6;text-align:left;padding:9px 12px;font-size:13px;color:#6e7991}
td{padding:9px 12px;border-top:1px solid #eff1f6;font-size:14px}
b{color:#c0392b} b.ok{color:#155724}
.ops{display:flex;gap:12px;margin-top:18px}
.ops button{padding:9px 22px;border-radius:10px;border:none;font-size:14px;font-weight:600;cursor:pointer}
#goBtn{background:#3567ff;color:#fff} #goBtn:disabled{opacity:.5}
#reBtn{background:#eef1f7;color:#556}
</style></head><body>
<nav class="topnav">
  <a href="/research" class="brand-logo" style="position:absolute;left:24px;top:50%;transform:translateY(-50%)"><img src="/brand/logo.png" alt="飞柚 GEO 品牌标识" style="height:32px"></a>
  <div class="nav-dropdown">
    <div class="nav-dropdown-toggle"><span>分析</span>
      <svg class="nav-arrow" viewBox="0 0 20 20" fill="none"><path d="M15.5 7.03835L10 12.9614L4.5 7.03835" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"/></svg>
    </div>
    <div class="nav-dropdown-menu">
      <a href="/mining">GEO搜索问句挖掘</a>
      <a href="/research">客户市场调研</a>
    </div>
  </div>
  <a href="/audit">官网诊断</a>
  <a href="/chat">对话</a>
  <div class="nav-dropdown">
    <div class="nav-dropdown-toggle active"><span>配置管理</span>
      <svg class="nav-arrow" viewBox="0 0 20 20" fill="none"><path d="M15.5 7.03835L10 12.9614L4.5 7.03835" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"/></svg>
    </div>
    <div class="nav-dropdown-menu">
      <a href="/skills">Skill管理</a>
      <a href="/company">企业资料</a>
      <a href="/oplog">操作日志</a>
      <a href="/backfill" class="active">JSON补录</a>
      <a href="/changelog">__ver__</a>
    </div>
  </div>
</nav>
<div class="page"><div class="card">
  <h1>JSON数据补录入库</h1>
  <p>把 var/output 里已生成调研报告的画像数据，按新版规则补录进企业资料库（var/data/company_info.db）：字段级合并（非空覆盖）、首次来源只写一次、触点记为「客户市场调研」。可重复执行。</p>
  <div id="body"></div>
  <div class="ops" id="ops" style="display:none">
    <button id="goBtn" onclick="apply()">执行入库</button>
    <button id="reBtn" onclick="preview()">重新扫描</button>
  </div>
</div></div>
<script>__JS__</script>
</body></html>'''
    return (tpl.replace("__JS__", BACKFILL_JS)
               .replace("__ver__", VER_LABEL))


# ── CLI Entry ──────────────────────────────────────────

def run_cli(args):
    config = load_config(args.config)
    agent = build_agent(config)

    print(f"\n{'='*60}")
    print(f"  GEO搜索问句挖掘Agent")
    print(f"  品牌: {args.brand}")
    if args.product:
        print(f"  产品: {args.product}")
    if args.method:
        method = agent.get_method(args.method)
        if method:
            print(f"  分析方法: {method['name']}")
        else:
            print(f"  警告: 未找到分析方法 {args.method}, 将使用全部skill")
    print(f"  模型: {config['llm']['model']}")
    print(f"{'='*60}\n")

    def on_progress(step, msg, elapsed):
        print(f"  [{elapsed:>6.1f}s] {step}: {msg}")

    result = asyncio.run(agent.run(args.brand, args.product,
                                   on_progress=on_progress,
                                   method_id=args.method))

    files = save_output(result, config)

    print(f"\n{'='*60}")
    print(f"  完成! 耗时 {result.get('total_elapsed', 0)}s")
    print(f"  产品数: {len(result.get('step1_products', []))}")
    print(f"  客群数: {len(result.get('step2_personas', []))}")
    print(f"  阶段数: {len(result.get('step3_journey', []))}")
    total_q = sum(
        len(Agent.extract_all_queries(sd))
        for sd in result.get("step4_topics", {}).values()
    )
    total_c = sum(
        len(sd.get("clusters", []))
        for sd in result.get("step5_clusters", {}).values()
    )
    print(f"  问句总数: {total_q}")
    print(f"  意图簇数: {total_c}")

    checks = result.get("self_check", [])
    pass_count = sum(1 for c in checks if isinstance(c, dict) and c.get("status") == "pass")
    print(f"  自校验: {pass_count}/{len(checks)} 通过")

    if files:
        print(f"\n  文件已保存:")
        for fmt, path in files.items():
            print(f"    [{fmt}] {path}")
    print(f"{'='*60}\n")


def run_server(args):
    import uvicorn
    config = load_config(args.config)
    server_cfg = config.get("server", {})
    port = args.port or server_cfg.get("port", 8777)
    host = server_cfg.get("host", "0.0.0.0")
    print(f"\n  GEO Agent API 服务启动: http://{host}:{port}")
    print(f"  API文档: http://{host}:{port}/docs")
    print(f"  模型: {config['llm']['model']}\n")
    uvicorn.run(app, host=host, port=port)


def main():
    parser = argparse.ArgumentParser(
        description="GEO搜索问句挖掘Agent — 输入品牌名,产出完整搜索问句与意图分析"
    )
    parser.add_argument("--brand", help="企业/品牌名称")
    parser.add_argument("--product", help="目标产品/服务(可选)")
    parser.add_argument("--method", default=None, help="分析方法ID(可选,默认使用第一个方法)")
    parser.add_argument("--config", default=None, help="配置文件路径(默认: 同目录下 config.yaml)")
    parser.add_argument("--mode", choices=["cli", "server"], default="cli", help="运行模式: cli=单次执行, server=启动API服务")
    parser.add_argument("--port", type=int, default=None, help="API服务端口(server模式)")
    args = parser.parse_args()

    if args.mode == "server":
        run_server(args)
    elif args.mode == "cli":
        if not args.brand:
            parser.error("cli模式需要 --brand 参数")
        run_cli(args)


if __name__ == "__main__":
    main()
