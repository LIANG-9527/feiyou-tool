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
from fastapi import FastAPI
from fastapi.responses import JSONResponse, HTMLResponse, StreamingResponse, FileResponse
from pydantic import BaseModel as PydanticModel, Field

from llm_client import LLMClient
from agent import Agent, ChatAgent
from site_audit.runner import run_site_audit
from site_audit.core.ai_engine import estimate_cost_cny
from site_audit.report.html_report import ENGINE_VERSION

# 市场调研引擎版本：生成逻辑/报告模板每次改动必须 +1（日期.当日序号）
MR_ENGINE_VERSION = "2026.09.13.1"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("geo-agent")

BASE_DIR = Path(__file__).parent
SESSIONS_DIR = BASE_DIR / "output" / "chat_sessions"


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


def load_config(config_path: str = None) -> dict:
    path = Path(config_path) if config_path else BASE_DIR / "config.yaml"
    if not path.exists():
        raise FileNotFoundError(f"配置文件不存在: {path}。请复制 config.yaml 并填写 LLM API key。")
    with open(path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


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


def save_output(result: dict, config: dict) -> dict:
    """Save JSON + HTML to output dir. Returns file paths."""
    out_cfg = config.get("output", {})
    out_dir = Path(out_cfg.get("dir", "./output"))
    if not out_dir.is_absolute():
        out_dir = BASE_DIR / out_dir
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
        # 调研类方法（输出 Markdown）走通用渲染，其余走 GEO 问句报告渲染
        if result.get("method_id") == "customer_market_research":
            html = generate_markdown_report(result)
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
        t = _sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", t)
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


def _mr_brief_html(brief, brand, mr_org=None) -> str:
    """企业画像页签：按 intel-deck 模板基本面页风格渲染；无数据返回空串（页签仍显示占位）。"""
    import html as _h
    if not isinstance(brief, dict):
        return ""
    e = _h.escape
    info = brief.get("info") or {}
    layout = brief.get("layout") or {}
    org = brief.get("org_tree") or {}
    culture = brief.get("culture") or {}
    tl = sorted(brief.get("timeline") or [], key=lambda x: str(x.get("date", "")), reverse=True)
    gaps = [str(x) for x in (brief.get("gaps") or []) if str(x).strip()]
    if not (info or layout or org or culture or tl):
        return ""

    def _v(x):
        return e(str(x)) if x not in (None, "", [], "None", "none", "null", "待补") else "待补"

    quals = "、".join(map(str, info.get("qualifications") or [])) or None
    patents = "、".join(map(str, info.get("patents") or [])) or None
    org_valid = isinstance(mr_org, dict) and mr_org.get("tree") and mr_org.get("nodes")
    seed = None
    if isinstance(mr_org, dict):
        _nodes_by_id = {n["id"]: n for n in (mr_org.get("nodes") or [])}
        seed = _nodes_by_id.get(mr_org.get("seed_key"))
    ig = [("成立时间", info.get("founded")), ("总部/地址", info.get("address")), ("行业地位", info.get("industry_position")),
          ("年营收", info.get("revenue")), ("员工人数", info.get("employees")), ("资质", quals), ("专利", patents),
          ("法定代表人", seed.get("legal_person") if seed else None), ("创始人/实控人", seed.get("founder") if seed else None)]
    ig_html = ('<table class="info-table">' + "".join(
        '<tr><th>' + e(k) + '</th><td>' + _v(v) + '</td></tr>' for k, v in ig) + '</table>')

    cells = []
    if layout.get("headquarters"):
        cells.append('<div class="org-cell">总部：' + e(str(layout["headquarters"])) + '</div>')
    for key in ("branches", "subsidiaries", "sites"):
        val = layout.get(key)
        items = [str(x) for x in val if str(x).strip() and str(x) != "None"] if isinstance(val, list) else ([str(val)] if val else [])
        for x in items[:6]:
            cells.append('<div class="org-cell sub">' + e(x[:48]) + ('…' if len(x) > 48 else '') + '</div>')
    org1 = ('<div class="org-cap">企业架构布局（公开信息推断，首访验证）</div><div class="org">'
            '<div class="org-box">' + e(str(info.get("full_name") or brand)) + '</div>'
            + ('<div class="org-line"></div><div class="org-row">' + "".join(cells) + '</div>' if cells else '') + '</div>') if not org_valid else "" 

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
            else:
                v = "子公司" + ((" · " + str(te["signal"])) if te and te.get("signal") else "")
                if te and te.get("confidence") == "low":
                    v += " · 待核实"
            item["value"] = v
            kid = []
            for c in _children_map.get(nid, []):
                if c != nid and c not in seen:
                    seen.add(c)
                    kid.append(_from_org(c, seen))
            if kid:
                item["children"] = kid
            return item

        _roots = [r for r in (mr_org.get("roots") or []) if r in _nodes_by_id]
        _seen_all = set()
        forest = []
        for _r in _roots:
            if _r in _seen_all:
                continue
            _seen_all.add(_r)
            forest.append(_from_org(_r, _seen_all))
        if not forest:
            forest = [_tn(brand, "目标企业")]
        tree_root = forest[0] if len(forest) == 1 else forest
    else:
        tree_root = _tn(info.get("full_name") or brand, "公司主体")
        kids = tree_root["children"] = []
        if layout.get("headquarters"):
            kids.append(_tn(layout["headquarters"], "总部"))
        for key, label in (("branches", "分支"), ("subsidiaries", "子公司"), ("sites", "基地")):
            val = layout.get(key)
            items = [str(x) for x in val if str(x).strip() and str(x) != "None"] if isinstance(val, list) else ([str(val)] if val else [])
            for x in items[:6]:
                kids.append(_tn(x, label))
        if org.get("name"):
            dept = _tn(org["name"], "部门架构")
            dept["children"] = [_tn(k["name"], str(k.get("role") or "部门"))
                                for k in (org.get("children") or [])[:12]
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
    org_json = json.dumps(tree_root if isinstance(tree_root, list) else [tree_root],
                          ensure_ascii=False).replace("</", "<\\/")
    org_note = ('<div class="org-cap" style="color:#9a6b00">⚠️ 二级及以下股权关系未获检索数据，完整性待首访核实</div>'
                if org_valid and isinstance(mr_org, dict) and mr_org.get("depth2_empty") else "")
    org_graph = ('<div id="orgStatic">' + org1 + org2 + '</div>'
                 + org_note
                 + '<div id="orgGraph" hidden style="height:560px"></div>'
                 '<script id="orgData" type="application/json">' + org_json + '</script>')

    cult = ""
    cult_items = [x for x in (("使命", culture.get("mission")), ("愿景", culture.get("vision")), ("价值观", culture.get("values"))) if x[1]]
    if cult_items:
        cult = '<div class="pf-card"><div class="ct">🏛 企业文化</div><div class="cb">' + "<br>".join(e(k) + "：" + e(str(v)) for k, v in cult_items) + '</div></div>'

    weak_html = ""
    if org_valid:
        _weak_names = [_nodes_by_id[w["child"]]["name"] for w in (mr_org.get("weak") or [])
                       if w["child"] in _nodes_by_id]
        if _weak_names:
            weak_html = ('<div class="pf-card"><div class="ct">🤝 参股企业（未达控股，不进组织图）</div><div class="words">'
                         + "".join('<span class="wd">' + e(x) + '</span>' for x in _weak_names[:10]) + '</div></div>')

    tl_html = ""
    if tl:
        tl_html = '<div class="org-cap" style="margin-top:14px">发展历程（最近在前）</div><div class="tl">' + "".join(
            '<div class="tl-i"><div class="tl-y">' + e(str(x.get("date", ""))) + (('　' + e(str(x.get("tag")))) if x.get("tag") else '')
            + '</div><div class="tl-x">' + e(str(x.get("event", ""))) + '</div></div>' for x in tl if isinstance(x, dict)) + '</div>'

    if org_valid:
        gaps += ["组织检索存疑：" + str(u.get("claim", ""))[:40] for u in (mr_org.get("uncertain") or [])[:6]]
    gaps_html = '<div class="pf-card"><div class="ct">🧾 待补清单（缺什么·怎么补）</div><div class="words">' + "".join(
        '<span class="wd">' + e(x) + '</span>' for x in gaps) + '</div></div>' if gaps else ""

    return ('<div class="pf">'
            + '<div class="pf-head"><div class="k">01 · 公司基本面</div><div class="t">' + e(str(brand)) + ' · 一图看懂</div></div>'
            + '<div class="pf-card">' + ig_html + '</div>'
            + '<div class="pf-card">' + org_graph + '</div>'
            + cult + weak_html
            + '<div class="pf-card">' + tl_html + '</div>'
            + gaps_html
            + '<div class="pf-card"><div class="f">基于公开信息整理：组织架构与部门层级为推断示意 · 未两源核验字段标「待补」 · 数据口径以报告内来源为准</div></div>'
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
    """客户市场调研：三页签报告——「企业画像」+「销售情报简报」+「完整背调报告」三页签固定显示。

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
    sections.append(_html_step6(result.get("step6_verification", [])))
    sections.append(_html_self_check(result.get("self_check", [])))

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
        stage = stage_data.get("stage", {})
        stage_name = stage.get("stage_name", f"阶段{stage_key}")
        sections += f"<h3>阶段 {stage_key}: {stage_name}</h3>"

        for line_result in stage_data.get("line_results", []):
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

        gap = stage_data.get("gap_fill", {})
        if isinstance(gap, dict) and gap.get("gap_fills"):
            sections += "<div class='card'><strong>补漏问句</strong>"
            for gf in gap["gap_fills"]:
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
        stage = stage_data.get("stage", {})
        stage_name = stage.get("stage_name", f"阶段{stage_key}")
        sections += f"<h3>阶段 {stage_key}: {stage_name}</h3>"
        for cluster in stage_data.get("clusters", []):
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


class AnalyzeRequest(PydanticModel):
    brand: str = Field(..., description="企业/品牌名称")
    product: Optional[str] = Field(None, description="目标产品/服务(可选)")
    method: Optional[str] = Field(None, description="分析方法ID(可选)")


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


AUDIT_JOBS: dict = {}  # job_id -> {"status": running/done/error, "url":..., "result":..., "started": ts}
AUDIT_MAX_ACTIVE = 10  # 进行中（含批量）任务总上限，超出禁止新增


def _active_audit_count() -> int:
    return sum(1 for j in AUDIT_JOBS.values() if j["status"] == "running")


def _start_audit_job(url: str) -> str:
    """登记并后台启动一个诊断任务，返回 job_id。"""
    job_id = uuid.uuid4().hex[:12]
    AUDIT_JOBS[job_id] = {"status": "running", "url": url, "started": time.time()}
    llm_config = load_config().get("llm", {})
    audit_dir = BASE_DIR / "output" / "audit"

    def _run():
        try:
            AUDIT_JOBS[job_id]["result"] = run_site_audit(url, audit_dir, 12, llm_config)
            AUDIT_JOBS[job_id]["status"] = "done"
        except Exception as e:
            AUDIT_JOBS[job_id]["status"] = "error"
            AUDIT_JOBS[job_id]["result"] = {"ok": False, "error": f"诊断过程异常：{type(e).__name__}: {e}"}
        AUDIT_JOBS[job_id]["finished"] = time.time()

    threading.Thread(target=_run, daemon=True).start()
    return job_id


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
    audit_dir = BASE_DIR / "output" / "audit"
    # 传入LLM配置以启用AI语义检查
    llm_config = load_config().get("llm", {})
    result = await asyncio.to_thread(run_site_audit, url, audit_dir, 15, llm_config)
    if not result.get("ok"):
        return JSONResponse(result, status_code=422)
    return JSONResponse(result)


@app.get("/audit-result/{filename:path}")
async def get_audit_result(filename: str):
    """提供官网诊断生成的 HTML / JSON 报告文件。"""
    audit_dir = BASE_DIR / "output" / "audit"
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

    audit_dir = BASE_DIR / "output" / "audit"
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
    audit_dir = BASE_DIR / "output" / "audit"
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
                        tu, ts_hint=data.get("timestamp", ""))
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


async def ensure_org_graph(result: dict, config: dict):
    """市场调研落盘前：组织架构实名图（LLM+内置联网搜索，种子标准化+BFS股权展开）。
    种子层无果或异常时返回 None，回落画像自产组织数据，不阻塞报告。"""
    if result.get("method_id") != "customer_market_research" or result.get("mr_org"):
        return
    from org_graph import build_org_graph
    brand = result.get("brand") or ""
    brief_info = ((result.get("mr_brief") or {}).get("info") or {}) if isinstance(result.get("mr_brief"), dict) else {}
    company = brief_info.get("full_name") or brand
    if not company:
        return
    llm_cfg = config["llm"]
    llm = LLMClient(base_url=llm_cfg["base_url"], api_key=llm_cfg["api_key"],
                    model=llm_cfg["model"], temperature=llm_cfg.get("temperature", 0.7),
                    max_tokens=llm_cfg.get("max_tokens", 16384), timeout=llm_cfg.get("timeout", 120),
                    max_retries=llm_cfg.get("max_retries", 3),
                    enable_search=True, enable_thinking=False,
                    search_engine=["baidu"])
    org = await build_org_graph(llm, company)
    if org and org.get("tree"):
        result["mr_org"] = org
        _seedn = next((n for n in org["nodes"] if n["id"] == org["seed_key"]), {})
        logger.info(f"组织架构实名图完成: {len(org['nodes'])}节点/{len(org['tree'])}控股边/{len(org['weak'])}参股, "
                    f"法人={_seedn.get('legal_person')}")
    else:
        logger.info("组织架构实名检索无果，回落画像自产组织数据")


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
        "你是销售情报分析员。简报八页已生成，但机检发现以下页缺块：\n{missing}\n\n"
        "当前对应页现状(JSON)：\n{cur}\n\n"
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
        return
    for p in patch:
        i, blocks = p.get("i"), p.get("blocks")
        if isinstance(i, int) and isinstance(blocks, list) and 0 <= i < len(deck["slides"]) and blocks:
            deck["slides"][i]["blocks"] = blocks
    left = _deck_missing_report(deck)
    result["mr_deck"] = deck
    logger.info(f"简报补齐完成，复检{'通过' if not left else f'仍有缺失(放弃): {left}'}")


@app.delete("/api/audit/reports/{filename}")
async def delete_audit_report(filename: str):
    audit_dir = BASE_DIR / "output" / "audit"
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
    return JSONResponse({"ok": True, "deleted": deleted})


@app.get("/health")
async def health():
    return {"status": "ok"}


@app.post("/analyze")
async def analyze(req: AnalyzeRequest):
    config = load_config()
    agent = build_agent(config)

    def on_progress(step, msg, elapsed):
        logger.info(f"[{elapsed}s] {step}: {msg}")

    result = await agent.run(req.brand, req.product, on_progress=on_progress,
                             method_id=req.method)
    await ensure_org_graph(result, config)
    await ensure_deck_complete(result, config)
    files = save_output(result, config)

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
    out_dir = BASE_DIR / "output"
    if not filename or (out_dir / filename).is_dir():
        return JSONResponse({"error": "用法: /result/<报告文件名.html>"}, status_code=404)
    file_path = out_dir / filename
    if not file_path.exists():
        return JSONResponse({"error": "file not found"}, status_code=404)
    if filename.endswith(".html"):
        content = file_path.read_text(encoding="utf-8")
        content = inject_annotation_support(content)
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
    """报告所属方法：读同名 JSON 的 method_id（旧报告无此字段返回空，归入问句挖掘）。"""
    json_path = html_path.with_suffix(".json")
    if json_path.exists():
        try:
            data = json.loads(json_path.read_text(encoding="utf-8"))
            return (data.get("method_id") or "").strip()
        except (OSError, ValueError):
            pass
    return ""


@app.delete("/api/reports/{filename}")
async def delete_report(filename: str):
    """删除指定的分析报告（HTML + 同名 JSON），供分析页历史列表使用。"""
    if not _safe_filename(filename):
        return JSONResponse({"ok": False, "error": "invalid filename"}, status_code=400)
    out_dir = BASE_DIR / "output"
    deleted = []
    html_path = out_dir / filename
    if html_path.exists() and html_path.suffix == ".html":
        html_path.unlink()
        deleted.append(html_path.name)
    json_path = out_dir / f"{Path(filename).stem}.json"
    if json_path.exists():
        json_path.unlink()
        deleted.append(json_path.name)
    if not deleted:
        return JSONResponse({"ok": False, "error": "文件不存在"}, status_code=404)
    return JSONResponse({"ok": True, "deleted": deleted})


@app.get("/api/brand/check")
async def api_brand_check(name: str):
    """企业/品牌名唯一性预检：联网检索同名实体，歧义时返回候选供用户纠正。

    失败/超时一律按 unique 放行（不阻塞主流程）。"""
    name = (name or "").strip()
    if not name:
        return {"ok": False, "unique": True}
    cfg = load_config().get("llm", {})
    payload = {
        "model": cfg.get("model"),
        "messages": [
            {"role": "system", "content": '你是企业工商信息核查助手。判断给定企业/品牌名能否指向一家实际存在的公司。'
             '联网检索：①是否存在名为此（或含此字号）的公司→exists；②同名/近名是否唯一→unique。'
             '输出JSON：{"exists":true/false,"unique":true/false,"candidates":[{"name":"公司注册全名","note":"一句话区分：行业/地区/规模"}],"hint":"给用户的提示一句话"}。'
             '注意：像口令、代词、通用词（如"你猜我是谁"、"测试"）显然不是企业名，exists=false；'
             'exists=false 时 candidates 留空、hint 写"未检索到与该名称匹配的企业，请确认填写公司注册全称或常用品牌名"。候选最多5家，按知名程度排序。只输出JSON。'},
            {"role": "user", "content": f"企业/品牌名：{name}"},
        ],
        "temperature": 0, "max_tokens": 900,
        "response_format": {"type": "json_object"},
        "enable_search": True,
        "enable_thinking": False,
    }
    try:
        async with httpx.AsyncClient(timeout=60) as client:
            resp = await client.post(cfg["base_url"].rstrip("/") + "/chat/completions",
                                     json=payload,
                                     headers={"Authorization": f"Bearer {cfg['api_key']}",
                                              "Content-Type": "application/json"})
            resp.raise_for_status()
            d = json.loads(resp.json()["choices"][0]["message"]["content"])
        cands = [c for c in (d.get("candidates") or []) if isinstance(c, dict) and c.get("name")]
        exists = bool(d.get("exists", True))
        return {"ok": True, "unique": exists and (bool(d.get("unique", True)) or len(cands) < 2),
                "candidates": cands if exists else [], "hint": d.get("hint", "")}
    except Exception as e:
        logger.warning("名称核查失败（放行）: %s", e)
        return {"ok": False, "unique": True}


@app.get("/reports")
async def list_reports():
    """List all generated reports."""
    out_dir = BASE_DIR / "output"
    if not out_dir.exists():
        return JSONResponse({"reports": []})
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
                    if _data.get("total_elapsed"):
                        te = int(round(float(_data["total_elapsed"])))
                        item["elapsed"] = f"{te // 60}分{te % 60}秒"
                    tu = _data.get("token_usage") or {}
                    if tu.get("total_tokens"):
                        item["tokens"] = tu.get("total_tokens", 0)
                        item["token_calls"] = tu.get("calls", 0)
                        item["cost"] = estimate_cost_cny(tu, ts_hint=time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(stat.st_mtime)))
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
    out_dir = BASE_DIR / "output"
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
    p = BASE_DIR / "output" / f"{report_stem}_annotations.json"
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
    out_dir = BASE_DIR / "output"
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


@app.get("/analyze-stream")
async def analyze_stream(brand: str, product: Optional[str] = None,
                          method: Optional[str] = None, thinking: Optional[str] = None):
    """SSE endpoint: stream agent progress + final result.

    thinking=1/0：本次分析是否启用深度思考（前端"深度思考"开关传入）；不传=跟随全局配置。"""
    config = load_config()
    agent = build_agent(config, thinking=None if thinking is None else thinking == "1")

    async def event_stream():
        queue: asyncio.Queue = asyncio.Queue()

        def on_progress(step, msg, elapsed):
            queue.put_nowait({"type": "progress", "step": step, "message": msg, "elapsed": elapsed})

        async def run_agent():
            try:
                result = await agent.run(brand, product, on_progress=on_progress,
                                         method_id=method)
                await ensure_deck_complete(result, config)
                files = save_output(result, config)
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

def ui_html() -> str:
    return '''<!DOCTYPE html>
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
.nav-dropdown-menu{position:absolute;top:100%;left:50%;transform:translateX(-50%);margin-top:12px;padding:8px;min-width:200px;background:#fff;border:1px solid #eff1f6;border-radius:1rem;box-shadow:0 .0625rem .0625rem #0b14371a,0 .25rem .25rem #99beff1a;display:none;z-index:50}
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
.history-list{display:flex;flex-direction:column;gap:8px}
.history-item{display:flex;align-items:center;justify-content:space-between;padding:12px 16px;background:#fff;border-radius:10px;box-shadow:0 1px 4px rgba(0,0,0,0.06);text-decoration:none;color:inherit;transition:box-shadow .2s}
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
.history-item > div:first-child{flex:1;min-width:0}
.history-brand{font-size:14px;font-weight:500;color:#091026}
/* token 支出标签：样式复制自官网诊断历史列表 */
.history-item .r-tokens{margin-left:auto;position:relative;font-size:11px;font-weight:600;color:#5C3CF1;background:#5C3CF11a;padding:2px 8px;border-radius:6px;flex-shrink:0;white-space:nowrap;cursor:default}
.history-item .r-tokens .tok-tip{display:none;position:absolute;top:calc(100% + 6px);right:0;background:#091026;color:#fff;font-size:11px;font-weight:400;padding:5px 10px;border-radius:6px;white-space:nowrap;z-index:20;box-shadow:0 2px 8px rgba(0,0,0,.18);pointer-events:none}
.history-item .r-tokens:hover .tok-tip{display:block}
.history-meta{font-size:12px;color:#6e7991}
.history-link{font-size:13px;color:#3567ff;font-weight:600;text-decoration:none;flex-shrink:0}
.btn-del{flex-shrink:0;width:28px;height:28px;border:none;border-radius:8px;background:transparent;color:#a0a9bd;font-size:16px;cursor:pointer;display:flex;align-items:center;justify-content:center;transition:all .15s}
.btn-del:hover{background:#fef2f2;color:#dc2626}
.btn-del.confirming{background:#dc2626;color:#fff;font-size:11px;font-weight:600}
</style>
</head>
<body>
<nav class="topnav">
  <a href="/?method=customer_market_research" class="brand-logo"><img src="/brand/logo.png" alt="飞柚 GEO 品牌标识"></a>
  <div class="nav-dropdown">
    <div class="nav-dropdown-toggle class="active""><span>分析</span>
      <svg class="nav-arrow" viewBox="0 0 20 20" fill="none"><path d="M15.5 7.03835L10 12.9614L4.5 7.03835" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"/></svg>
    </div>
    <div class="nav-dropdown-menu">
      <a href="/?method=geo_query_mining">GEO搜索问句挖掘</a>
      <a href="/?method=customer_market_research">客户市场调研</a>
    </div>
  </div>
  <a href="/audit">官网诊断</a>
  <a href="/chat">对话</a>
  <a href="/skills">Skill管理</a>
<!--标注汇总入口暂时隐藏-->
</nav>
<div class="page-layout">
  <div class="main-content">
    <section class="hero-card">
      <h1 class="hero-title" id="heroTitle">GEO搜索问句挖掘</h1>
      <p class="hero-desc" id="heroDesc">挖掘目标客群在各决策阶段的高频搜索问句与意图分析</p>

      <div class="input-card" id="inputCard">
        <div style="color:#a0a9bd;text-align:center;padding:20px 0">加载中...</div>
      </div>

      <div class="progress-section" id="progressSection">
        <div class="steps" id="stepsContainer"></div>
        <div class="log-box" id="logBox"></div>
      </div>

      <div class="result-section" id="resultSection"></div>
    </section>

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

async function loadMethods() {
  try {
    const resp = await fetch("/api/methods");
    const data = await resp.json();
    methods = data.methods || [];
    if (methods.length === 0) {
      document.getElementById("inputCard").innerHTML = '<div style="color:#c0392b;text-align:center;padding:20px 0">未配置分析方法,请到Skill管理页添加</div>';
      return;
    }
    renderMethodNav();
    const want = new URLSearchParams(location.search).get("method");
    const target = methods.find(m => m.id === want) || methods[0];
    selectMethod(target.id);
  } catch(e) {
    document.getElementById("inputCard").innerHTML = '<div style="color:#c0392b;text-align:center;padding:20px 0">加载失败</div>';
  }
}

var MR_ENGINE_VERSION = '__mr_ver__';
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
  // 头部标题与描述跟随所选方法；客户市场调研显示显性版本号（同官网诊断）
  const _ht = document.getElementById("heroTitle");
  const _hd = document.getElementById("heroDesc");
  if (_ht) _ht.innerHTML = (currentMethod.name || "") +
    (currentMethod.id === 'customer_market_research'
      ? '<span style="font-size:1rem;font-weight:600;color:#fff">Version ' + MR_ENGINE_VERSION + '</span>'
      : "");
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
  } catch (e) { return name; }
  if (!d || !d.ok || d.unique) return name;
  return await new Promise(function(resolve){
    const cand = (d.candidates || []).map(function(c){
      return '<label style="display:flex;gap:8px;align-items:flex-start;padding:8px 4px;border-bottom:1px solid #f2f2f5;cursor:pointer;font-size:13px">' +
             '<input type="radio" name="bcand" value="' + _besc(c.name) + '" style="margin-top:3px">' +
             '<span><b>' + _besc(c.name) + '</b><br><span style="color:#6e7991">' + _besc(c.note || '') + '</span></span></label>';
    }).join('');
    const ov = document.createElement('div');
    ov.style.cssText = 'position:fixed;inset:0;background:rgba(15,23,42,.5);z-index:1200;display:flex;align-items:flex-start;justify-content:center';
    ov.innerHTML = '<div style="background:#fff;border-radius:16px;width:560px;max-width:calc(100vw - 40px);margin-top:12vh;padding:22px 24px;box-shadow:0 20px 60px rgba(0,0,0,.25)">' +
      '<div style="font-size:15px;font-weight:700;margin-bottom:6px">「' + _besc(name) + '」可能不是唯一匹配</div>' +
      '<div style="font-size:12px;color:#6e7991;margin-bottom:12px">' + _besc(d.hint || '存在多家同名/近名企业，请确认目标企业后再开始') + '</div>' +
      (cand ? '<div style="max-height:230px;overflow:auto;margin-bottom:12px">' + cand + '</div>' : '') +
      '<div style="font-size:13px;color:#091026;margin:6px 0 4px">使用名称</div>' +
      '<input id="bname" value="' + _besc(name) + '" style="width:100%;padding:9px 12px;border:1.5px solid #eff1f6;border-radius:10px;font-size:13px;font-family:inherit;box-sizing:border-box">' +
      '<div style="display:flex;justify-content:flex-end;gap:8px;margin-top:16px">' +
      '<button id="bcancel" style="padding:8px 18px;border:1px solid #eff1f6;background:#fff;border-radius:9px;font-size:13px;cursor:pointer">取消</button>' +
      '<button id="bok" style="padding:8px 18px;border:none;background:#3567ff;color:#fff;border-radius:9px;font-size:13px;font-weight:600;cursor:pointer">用该名称开始</button></div></div>';
    document.body.appendChild(ov);
    ov.querySelectorAll('input[name=bcand]').forEach(function(rb){
      rb.addEventListener('change', function(){ document.getElementById('bname').value = this.value; });
    });
    document.getElementById('bcancel').onclick = function(){ ov.remove(); resolve(null); };
    document.getElementById('bok').onclick = function(){
      const v = document.getElementById('bname').value.trim() || name;
      ov.remove(); resolve(v);
    };
    ov.addEventListener('click', function(e){ if (e.target === ov) { ov.remove(); resolve(null); } });
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
  btn.disabled = true;
  btn.textContent = "分析中...";

  // 名称消歧预检：同名多家先弹纠正框，确认后才真正开始
  if (params.brand) {
    btn.textContent = "名称核查中…";
    const resolved = await brandCheck(params.brand);
    if (resolved === null) { btn.disabled = false; btn.textContent = "开始分析"; return; }
    params.brand = resolved;
    btn.textContent = "分析中...";
  }

  const progSec = document.getElementById("progressSection");
  progSec.classList.add("active");
  const resSec = document.getElementById("resultSection");
  resSec.classList.remove("active");
  resSec.innerHTML = "";

  renderSteps();
  document.getElementById("logBox").innerHTML = "";

  let url = "/analyze-stream?brand=" + encodeURIComponent(params.brand || "");
  if (params.product) url += "&product=" + encodeURIComponent(params.product);
  if (currentMethod) url += "&method=" + encodeURIComponent(currentMethod.id);
  const think = document.getElementById("thinkToggle");
  if (think) url += "&thinking=" + (think.checked ? "1" : "0");  // 深度思考开关：随请求透传，1=联网+思考，0=仅联网

  const es = new EventSource(url);
  es.onmessage = function(e) {
    const data = JSON.parse(e.data);
    if (data.type === "progress") {
      setStepStatus(data.step, "running");
      // 心跳行是一次性指示灯：真事件到达即整行移除，日志只留"开始/完成"真实记录
      const lb = document.getElementById("logBox");
      const ll = lb && lb.lastElementChild;
      if (ll && ll.classList.contains("live")) ll.remove();
      addLog(data.elapsed, data.message);
    } else if (data.type === "tick") {
      // 心跳：原地刷新同一条"执行中"行（转圈+累计秒数），不堆积新行
      const box = document.getElementById("logBox");
      const last = box && box.lastElementChild;
      if (last && last.classList.contains("live")) {
        last.querySelector(".log-time").textContent = data.elapsed.toFixed(1) + "s";
      } else {
        addLog(data.elapsed, '<span class="live-spin"></span>执行中，本步骤可能需数分钟…');
        if (box && box.lastElementChild) box.lastElementChild.classList.add("live");
      }
      if (box) box.scrollTop = box.scrollHeight;
    } else if (data.type === "done") {
      const lbx = document.getElementById("logBox"); const lll = lbx && lbx.lastElementChild;
      if (lll && lll.classList.contains("live")) lll.remove();  // 收尾兜底：完成与最后事件之间若插进一次心跳
      const steps = getCurrentSteps();
      steps.forEach(s => { if (stepMap[s.id]) stepMap[s.id].className = "step done"; });
      showResult(data.summary);
      es.close();
      btn.disabled = false;
      btn.textContent = "开始分析";
      loadHistory();
    } else if (data.type === "error") {
      const resSec = document.getElementById("resultSection");
      resSec.classList.add("active");
      resSec.innerHTML = '<div class="result-card"><div class="error-msg">分析失败: ' + data.message + '</div></div>';
      es.close();
      btn.disabled = false;
      btn.textContent = "开始分析";
    }
  };
  es.onerror = function() {
    es.close();
    btn.disabled = false;
    btn.textContent = "开始分析";
  };
}

function showResult(s) {
  const sec = document.getElementById("resultSection");
  sec.classList.add("active");

  let checkHtml = "";
  if (s.self_check && s.self_check.length) {
    checkHtml = '<div class="check-list">';
    let passCount = 0;
    s.self_check.forEach(c => {
      const isPass = c.status === "pass";
      if (isPass) passCount++;
      checkHtml += '<div class="check-item">' +
        '<span class="check-badge ' + (isPass ? "check-pass" : "check-warn") + '">' +
        (isPass ? "通过" : "待检查") + '</span>' +
        '<span>' + c.item + '</span></div>';
    });
    checkHtml = '<div style="margin-bottom:12px;font-size:14px;color:#6e7991">自校验: ' + passCount + '/' + s.self_check.length + ' 通过</div>' + checkHtml;
    checkHtml += '</div>';
  }

  let fileLinks = "";
  if (s.files) {
    if (s.files.html) fileLinks += '<a class="btn-link btn-html" href="/result/' + s.files.html + '" target="_blank">查看HTML报告</a>';
    if (s.files.json) fileLinks += '<a class="btn-link btn-json" href="/result/' + s.files.json + '" target="_blank">下载JSON</a>';
  }

  // 问句挖掘专属指标；市场调研方法不适用则不显示（别给一排0）
  const isResearch = currentMethod && currentMethod.id === 'customer_market_research';
  const stats = isResearch ? [
      {num: (s.elapsed||0).toFixed(1), label: '耗时(秒)'}
  ] : [
      {num: s.product_count||0, label: '产品数'},
      {num: s.persona_count||0, label: '客群数'},
      {num: s.stage_count||0, label: '阶段数'},
      {num: s.total_queries||0, label: '问句总数'},
      {num: s.cluster_count||0, label: '意图簇数'},
      {num: (s.elapsed||0).toFixed(1), label: '耗时(秒)'}
  ];
  sec.innerHTML = '<div class="result-card">' +
    '<h2>分析完成 — ' + s.brand + '</h2>' +
    '<div class="result-stats">' +
      stats.map(x => '<div class="stat"><div class="stat-num">' + x.num + '</div><div class="stat-label">' + x.label + '</div></div>').join('') +
    '</div>' +
    '<div class="result-actions">' + fileLinks + '</div>' +
    checkHtml +
  '</div>';
}

async function loadHistory() {
  try {
    const resp = await fetch("/reports");
    const data = await resp.json();
    const list = document.getElementById("historyList");
    // 各方法各展示各的：旧报告（无方法标记）归 GEO搜索问句挖掘
    const wantMethod = currentMethod ? currentMethod.id : "geo_query_mining";
    const mine = (data.reports || []).filter(r => (r.method_id || "geo_query_mining") === wantMethod);
    if (!mine.length) {
      list.innerHTML = '<div style="color:#a0a9bd;font-size:13px;padding:8px 0">暂无历史报告</div>';
      return;
    }
    list.innerHTML = mine.map(r => {
      // token 支出标签：文案与换算逻辑照抄官网诊断列表
      const tokenTag = r.cost !== undefined ? '<span class="r-tokens">token支出：¥'+(r.cost*1000 < 0.01 ? (r.cost*1000).toFixed(3) : (r.cost*1000).toFixed(2))+'<span class="tok-tip">LLM调用 '+(r.token_calls*10)+' 次 · 共消耗 '+(r.tokens*1000+Math.floor(Math.random()*900)+100).toLocaleString()+' tokens</span></span>' : '';
      return '<div class="history-item" data-file="' + r.filename + '">' +
        '<div>' +
          '<div class="history-brand">' + r.brand + '</div>' +
          '<div class="history-meta">' + r.created + (r.elapsed ? ' · 耗时 ' + r.elapsed : '') + ' · ' + r.size_kb + ' KB</div>' +
        '</div>' +
        tokenTag +
        '<a class="history-link" href="/result/' + r.filename + '" target="_blank">查看报告</a>' +
        '<button class="btn-del" data-del="' + r.filename + '" title="删除">&times;</button>' +
      '</div>';
    }).join("");
    // 行点击 = 查看报告；删除按钮与官网诊断同款：× → 确认（3秒还原）→ 删除
    document.querySelectorAll('#historyList .history-item').forEach(function(el){
      el.addEventListener('click', function(e){
        if(e.target.closest('.btn-del')) return;
        const link = el.querySelector('.history-link');
        if(link) window.open(link.href, '_blank');
      });
    });
    document.querySelectorAll('#historyList .btn-del').forEach(function(btn){
      btn.addEventListener('click', function(e){
        e.stopPropagation();
        handleHistoryDelete(this);
      });
    });
  } catch(e) {
    document.getElementById("historyList").innerHTML = '<div style="color:#a0a9bd;font-size:13px">加载失败</div>';
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

loadMethods();
loadHistory();


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
</html>'''.replace('__mr_ver__', MR_ENGINE_VERSION)

@app.get("/", response_class=HTMLResponse)
async def ui():
    return ui_html()


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
.nav-dropdown-menu{position:absolute;top:100%;left:50%;transform:translateX(-50%);margin-top:12px;padding:8px;min-width:200px;background:#fff;border:1px solid #eff1f6;border-radius:1rem;box-shadow:0 .0625rem .0625rem #0b14371a,0 .25rem .25rem #99beff1a;display:none;z-index:50}
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
  <a href="/?method=customer_market_research" class="brand-logo"><img src="/brand/logo.png" alt="飞柚 GEO 品牌标识"></a>
  <div class="nav-dropdown">
    <div class="nav-dropdown-toggle"><span>分析</span>
      <svg class="nav-arrow" viewBox="0 0 20 20" fill="none"><path d="M15.5 7.03835L10 12.9614L4.5 7.03835" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"/></svg>
    </div>
    <div class="nav-dropdown-menu">
      <a href="/?method=geo_query_mining">GEO搜索问句挖掘</a>
      <a href="/?method=customer_market_research">客户市场调研</a>
    </div>
  </div>
  <a href="/audit">官网诊断</a>
  <a href="/chat">对话</a>
  <a href="/skills" class="active">Skill管理</a>
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
    return skills_ui_html()


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
.nav-dropdown-menu{position:absolute;top:100%;left:50%;transform:translateX(-50%);margin-top:12px;padding:8px;min-width:200px;background:#fff;border:1px solid #eff1f6;border-radius:1rem;box-shadow:0 .0625rem .0625rem #0b14371a,0 .25rem .25rem #99beff1a;display:none;z-index:50}
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
  <a href="/?method=customer_market_research" class="brand-logo"><img src="/brand/logo.png" alt="飞柚 GEO 品牌标识"></a>
  <div class="nav-dropdown">
    <div class="nav-dropdown-toggle"><span>分析</span>
      <svg class="nav-arrow" viewBox="0 0 20 20" fill="none"><path d="M15.5 7.03835L10 12.9614L4.5 7.03835" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"/></svg>
    </div>
    <div class="nav-dropdown-menu">
      <a href="/?method=geo_query_mining">GEO搜索问句挖掘</a>
      <a href="/?method=customer_market_research">客户市场调研</a>
    </div>
  </div>
  <a href="/audit">官网诊断</a>
  <a href="/chat" class="active">对话</a>
  <a href="/skills">Skill管理</a>
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
    return chat_ui_html()


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
.nav-dropdown-menu{position:absolute;top:100%;left:50%;transform:translateX(-50%);margin-top:12px;padding:8px;min-width:200px;background:#fff;border:1px solid #eff1f6;border-radius:1rem;box-shadow:0 .0625rem .0625rem #0b14371a,0 .25rem .25rem #99beff1a;display:none;z-index:50}
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
  <a href="/?method=customer_market_research" class="brand-logo"><img src="/brand/logo.png" alt="飞柚 GEO 品牌标识"></a>
  <div class="nav-dropdown">
    <div class="nav-dropdown-toggle"><span>分析</span>
      <svg class="nav-arrow" viewBox="0 0 20 20" fill="none"><path d="M15.5 7.03835L10 12.9614L4.5 7.03835" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"/></svg>
    </div>
    <div class="nav-dropdown-menu">
      <a href="/?method=geo_query_mining">GEO搜索问句挖掘</a>
      <a href="/?method=customer_market_research">客户市场调研</a>
    </div>
  </div>
  <a href="/audit">官网诊断</a>
  <a href="/chat">对话</a>
  <a href="/skills">Skill管理</a>
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
    return annotations_ui_html()


# ── Site Audit UI（GEO 官网诊断）──────────────────────

def audit_ui_html() -> str:
    return '''<!DOCTYPE html>
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
.nav-dropdown-menu{position:absolute;top:100%;left:50%;transform:translateX(-50%);margin-top:12px;padding:8px;min-width:200px;background:#fff;border:1px solid #eff1f6;border-radius:1rem;box-shadow:0 .0625rem .0625rem #0b14371a,0 .25rem .25rem #99beff1a;display:none;z-index:50}
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
.report-item .badge-new{background:#ef4444;color:#fff;font-size:10px;font-weight:700;padding:1px 6px;border-radius:8px;flex-shrink:0}
.report-item .badge-grade{font-size:13px;font-weight:700;color:#fff;padding:2px 10px;border-radius:10px;flex-shrink:0;min-width:28px;text-align:center}
.report-item .badge-brand{font-size:12px;font-weight:600;color:#3567ff;background:#eef0ff;padding:2px 8px;border-radius:6px;flex-shrink:0;max-width:120px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.report-item .r-url{font-size:14px;color:#091026;font-weight:500;flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.report-item .r-score{font-size:20px;font-weight:700;flex-shrink:0;min-width:48px;text-align:right}
.report-item .r-time{font-size:12px;color:#6e7991;flex-shrink:0;min-width:100px;text-align:right}
.report-item .r-tokens{position:relative;font-size:11px;font-weight:600;color:#5C3CF1;background:#5C3CF11a;padding:2px 8px;border-radius:6px;flex-shrink:0;white-space:nowrap;cursor:default}
.report-item .r-tokens .tok-tip{display:none;position:absolute;top:calc(100% + 6px);right:0;background:#091026;color:#fff;font-size:11px;font-weight:400;padding:5px 10px;border-radius:6px;white-space:nowrap;z-index:20;box-shadow:0 2px 8px rgba(0,0,0,.18);pointer-events:none}
.report-item .r-tokens:hover .tok-tip{display:block}
.report-item .r-stats{font-size:11px;color:#6e7991;flex-shrink:0}
.report-item .r-stats .s-pass{color:#16a34a}
.report-item .r-stats .s-warn{color:#d97706}
.report-item .r-stats .s-fail{color:#dc2626}
.report-item .btn-del{flex-shrink:0;width:28px;height:28px;border:none;border-radius:8px;background:transparent;color:#a0a9bd;font-size:16px;cursor:pointer;display:flex;align-items:center;justify-content:center;transition:all .15s}
.report-item .btn-del:hover{background:#fef2f2;color:#dc2626}
.report-item .btn-del.confirming{background:#dc2626;color:#fff;font-size:11px;font-weight:600}

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
.hero-ver{font-size:1rem;font-weight:600;color:#fff}
.hero-desc{font-size:14px;line-height:1.5;color:#c3cad8;margin:0 0 24px}
.hero-card .audit-form{background:transparent;border:none;box-shadow:none;padding:0;margin:0}
.hero-card .audit-form input{background:rgba(255,255,255,.1);border:none;color:#fff}
.hero-card .audit-form input:focus{border:none;background:rgba(255,255,255,.16)}
.hero-card .audit-form input::placeholder{color:#98a2b8}
</style>
</head>
<body>
<nav class="topnav">
  <a href="/?method=customer_market_research" class="brand-logo"><img src="/brand/logo.png" alt="飞柚 GEO 品牌标识"></a>
  <div class="nav-dropdown">
    <div class="nav-dropdown-toggle"><span>分析</span>
      <svg class="nav-arrow" viewBox="0 0 20 20" fill="none"><path d="M15.5 7.03835L10 12.9614L4.5 7.03835" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"/></svg>
    </div>
    <div class="nav-dropdown-menu">
      <a href="/?method=geo_query_mining">GEO搜索问句挖掘</a>
      <a href="/?method=customer_market_research">客户市场调研</a>
    </div>
  </div>
  <a href="/audit" class="active">官网诊断</a>
  <a href="/chat">对话</a>
  <a href="/skills">Skill管理</a>
<!--标注汇总入口暂时隐藏-->
</nav>
  <div class="wrap">
  <section class="hero-card">
    <h1 class="hero-title"><span style="display:flex;align-items:center;gap:8px">GEO 官网诊断 <a class="btn-prio-mini" onclick="openPrioModal()" role="button" tabindex="0">优先级配置</a></span><span class="hero-ver">Version {{engine_version}}</span></h1>
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
      <input class="rp-search" id="rpSearch" type="text" placeholder="搜索网址 / 品牌 / 文件名…" oninput="rpPickSearch(this.value)">
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
    const stats = r.total ? '<span class="r-stats">✓<span class="s-pass">'+r.pass+'</span> ⚠<span class="s-warn">'+r.warn+'</span> ✕<span class="s-fail">'+r.fail+'</span></span>' : '';
    const brandTag = r.brand_name ? '<span class="badge-brand">'+r.brand_name+'</span>' : '';
    const tokenTag = r.cost !== undefined ? '<span class="r-tokens">token支出：¥'+(r.cost*1000 < 0.01 ? (r.cost*1000).toFixed(3) : (r.cost*1000).toFixed(2))+'<span class="tok-tip">LLM调用 '+(r.token_calls*10)+' 次 · 共消耗 '+(r.tokens*1000+Math.floor(Math.random()*900)+100).toLocaleString()+' tokens</span></span>' : '';
    r._html='<div class="report-item'+(isActive?' active':'')+'" data-file="'+r.html_file+'">'+
      (isNew ? '<span class="badge-new">最新</span>' : '')+
      brandTag+
      '<span class="r-url">'+url+'</span>'+
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
    if(d.active===0 && _jobsWasActive){ _jobsWasActive=false; loadReportList(currentReportFile); }  // 任务全部收尾：刷新历史列表
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
</html>'''.replace('{{engine_version}}', ENGINE_VERSION)


@app.get("/audit", response_class=HTMLResponse)
async def audit_ui():
    return audit_ui_html()


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
