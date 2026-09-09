"""GEO Query Mining Agent — FastAPI server + CLI entry + HTML report."""
from __future__ import annotations
from typing import Optional
import asyncio
import json
import os
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


def build_agent(config: dict) -> Agent:
    llm_cfg = config["llm"]
    llm = LLMClient(
        base_url=llm_cfg["base_url"],
        api_key=llm_cfg["api_key"],
        model=llm_cfg["model"],
        temperature=llm_cfg.get("temperature", 0.7),
        max_tokens=llm_cfg.get("max_tokens", 4096),
        timeout=llm_cfg.get("timeout", 120),
        max_retries=llm_cfg.get("max_retries", 3),
    )
    return Agent(llm, max_concurrency=llm_cfg.get("max_concurrency", 5))


def save_output(result: dict, config: dict) -> dict:
    """Save JSON + HTML to output dir. Returns file paths."""
    out_cfg = config.get("output", {})
    out_dir = Path(out_cfg.get("dir", "./output"))
    if not out_dir.is_absolute():
        out_dir = BASE_DIR / out_dir
    out_dir.mkdir(parents=True, exist_ok=True)

    brand = result.get("brand", "unknown")
    ts = time.strftime("%Y%m%d_%H%M%S")
    base_name = f"{brand}_{ts}"
    paths = {}

    if out_cfg.get("save_json", True):
        json_path = out_dir / f"{base_name}.json"
        with open(json_path, "w", encoding="utf-8") as f:
            json.dump(result, f, ensure_ascii=False, indent=2)
        paths["json"] = str(json_path)

    if out_cfg.get("save_html", True):
        html_path = out_dir / f"{base_name}.html"
        html = generate_html_report(result)
        with open(html_path, "w", encoding="utf-8") as f:
            f.write(html)
        paths["html"] = str(html_path)

    return paths


# ── HTML Report Generator ──────────────────────────────

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
    body { font-family: -apple-system, "PingFang SC", "Microsoft YaHei", sans-serif; background: #f5f5f7; color: #1d1d1f; line-height: 1.6; }
    .container { max-width: 960px; margin: 0 auto; padding: 40px 24px; }
    h1 { font-size: 28px; margin-bottom: 8px; }
    h2 { font-size: 22px; margin: 32px 0 16px; padding-bottom: 8px; border-bottom: 2px solid #0071e3; }
    h3 { font-size: 17px; margin: 20px 0 10px; }
    .meta { background: #fff; border-radius: 12px; padding: 16px 20px; margin-bottom: 24px; box-shadow: 0 1px 4px rgba(0,0,0,0.06); }
    .meta p { margin: 4px 0; color: #6e6e73; }
    .meta strong { color: #1d1d1f; }
    .card { background: #fff; border-radius: 12px; padding: 20px; margin-bottom: 16px; box-shadow: 0 1px 4px rgba(0,0,0,0.06); }
    table { width: 100%; border-collapse: collapse; margin: 8px 0; }
    th { background: #f0f0f3; padding: 8px 12px; text-align: left; font-size: 13px; font-weight: 600; }
    td { padding: 8px 12px; border-bottom: 1px solid #e8e8ed; font-size: 14px; vertical-align: top; }
    .tag { display: inline-block; padding: 2px 8px; border-radius: 4px; font-size: 12px; margin: 1px; }
    .tag-a { background: #e3f0ff; color: #0071e3; }
    .tag-b { background: #fff4e3; color: #c97000; }
    .badge { display: inline-block; padding: 2px 8px; border-radius: 4px; font-size: 12px; font-weight: 600; }
    .pass { background: #d4edda; color: #155724; }
    .check { background: #fff3cd; color: #856404; }
    .cluster { border-left: 3px solid #0071e3; padding-left: 16px; margin: 12px 0; }
    .query-item { padding: 6px 0; border-bottom: 1px solid #f0f0f3; }
    .signal { color: #86868b; font-size: 13px; }
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
        max_tokens=llm_cfg.get("max_tokens", 4096),
        timeout=llm_cfg.get("timeout", 120),
        max_retries=llm_cfg.get("max_retries", 3),
    )
    return ChatAgent(llm, max_concurrency=llm_cfg.get("max_concurrency", 5))


class ChatRequest(PydanticModel):
    message: str = Field(..., description="用户消息")
    session_id: str = Field("default", description="会话ID")


chat_sessions: dict = {}


# ── 官网诊断（site_audit）────────────────────────────

class AuditRequest(PydanticModel):
    url: str = Field(..., description="要诊断的官网网址")


AUDIT_JOBS: dict = {}  # job_id -> {"status": running/done/error, "result":..., "started": ts}


@app.post("/api/audit/start")
async def api_audit_start(req: AuditRequest):
    """提交诊断任务立即返回 job_id（诊断在后台线程执行）。

    诊断耗时 1~3 分钟，走同步接口时会被链路上的代理/nginx 掐断返回 HTML 错误页，
    前端按 JSON 解析就报 "Unexpected token '<'"——改成提交+轮询彻底避开长连接。"""
    url = (req.url or "").strip()
    if not url:
        return JSONResponse({"ok": False, "error": "请输入要诊断的网址"}, status_code=400)
    job_id = uuid.uuid4().hex[:12]
    AUDIT_JOBS[job_id] = {"status": "running", "started": time.time()}
    llm_config = load_config().get("llm", {})
    audit_dir = BASE_DIR / "output" / "audit"

    def _run():
        try:
            AUDIT_JOBS[job_id]["result"] = run_site_audit(url, audit_dir, 12, llm_config)
            AUDIT_JOBS[job_id]["status"] = "done"
        except Exception as e:
            AUDIT_JOBS[job_id]["status"] = "error"
            AUDIT_JOBS[job_id]["result"] = {"ok": False, "error": f"诊断过程异常：{e}"}

    threading.Thread(target=_run, daemon=True).start()
    return JSONResponse({"ok": True, "job_id": job_id})


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
        return HTMLResponse(content=file_path.read_text(encoding="utf-8"))
    if filename.endswith(".json"):
        return FileResponse(file_path, media_type="application/json", filename=filename)
    return FileResponse(file_path)


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


@app.delete("/api/audit/reports/{filename}")
async def delete_audit_report(filename: str):
    """删除指定的诊断报告（HTML + JSON）。"""
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
    .annot-toolbar { position: fixed; top: 12px; right: 12px; z-index: 999; display: flex; gap: 8px; align-items: center; }
    .annot-toggle { padding: 6px 16px; border-radius: 20px; border: none; background: #fff; color: #86868b; font-size: 13px; cursor: pointer; box-shadow: 0 2px 8px rgba(0,0,0,0.15); transition: all .2s; }
    .annot-toggle:hover { color: #0071e3; }
    .annot-toggle.active { background: #0071e3; color: #fff; }
    .annot-count { padding: 2px 8px; border-radius: 10px; background: #c0392b; color: #fff; font-size: 11px; font-weight: 700; display: none; }
    .annot-count.show { display: inline; }
    body.annot-mode [data-aid] { cursor: pointer; transition: outline .15s; }
    body.annot-mode [data-aid]:hover { outline: 2px dashed #0071e3; outline-offset: 2px; }
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
    .annot-dialog .annot-target { font-size: 12px; color: #c0c0c5; margin-bottom: 16px; }
    .annot-field { margin-bottom: 14px; }
    .annot-field label { display: block; font-size: 13px; color: #86868b; margin-bottom: 5px; font-weight: 500; }
    .annot-field select, .annot-field textarea { width: 100%; padding: 8px 12px; border: 1.5px solid #e0e0e5; border-radius: 8px; font-size: 14px; outline: none; font-family: inherit; }
    .annot-field select:focus, .annot-field textarea:focus { border-color: #0071e3; }
    .annot-field textarea { resize: vertical; min-height: 80px; }
    .annot-actions { display: flex; gap: 10px; justify-content: flex-end; }
    .annot-btn { padding: 8px 20px; border-radius: 8px; font-size: 14px; font-weight: 600; cursor: pointer; transition: opacity .2s; }
    .annot-btn-save { background: #0071e3; color: #fff; border: none; }
    .annot-btn-save:hover { opacity: .9; }
    .annot-btn-cancel { background: #fff; color: #86868b; border: 1.5px solid #e0e0e5; }
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
    file_path = out_dir / filename
    if not file_path.exists():
        return JSONResponse({"error": "file not found"}, status_code=404)
    if filename.endswith(".html"):
        content = file_path.read_text(encoding="utf-8")
        content = inject_annotation_support(content)
        return HTMLResponse(content=content)
    if filename.endswith(".json"):
        return FileResponse(file_path, media_type="application/json", filename=filename)
    return FileResponse(file_path)


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
            reports.append({
                "filename": f.name,
                "brand": f.stem.rsplit("_", 2)[0] if "_" in f.stem else f.stem,
                "size_kb": round(stat.st_size / 1024, 1),
                "created": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(stat.st_mtime)),
                "mtime": stat.st_mtime,
            })
    reports.sort(key=lambda x: x["mtime"], reverse=True)
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
        m["skill_details"] = [s for s in all_skills if s["id"] in m_skill_ids]
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
                          method: Optional[str] = None):
    """SSE endpoint: stream agent progress + final result."""
    config = load_config()
    agent = build_agent(config)

    async def event_stream():
        queue: asyncio.Queue = asyncio.Queue()

        def on_progress(step, msg, elapsed):
            queue.put_nowait({"type": "progress", "step": step, "message": msg, "elapsed": elapsed})

        async def run_agent():
            try:
                result = await agent.run(brand, product, on_progress=on_progress,
                                         method_id=method)
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
                await queue.put({"type": "error", "message": str(e)})

        task = asyncio.create_task(run_agent())

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
body{font-family:-apple-system,"PingFang SC","Microsoft YaHei",sans-serif;background:#f5f5f7;color:#1d1d1f;line-height:1.6}
.topnav{display:flex;gap:8px;justify-content:center;padding:12px 0 0}
.topnav a{padding:6px 16px;border-radius:20px;font-size:13px;text-decoration:none;color:#86868b;background:#fff;box-shadow:0 1px 4px rgba(0,0,0,0.06);transition:all .2s}
.topnav a:hover{color:#0071e3}
.topnav a.active{background:#0071e3;color:#fff}
.topnav a[href="/annotations"]{background:#fff3cd;color:#856404}
.topnav a[href="/annotations"]:hover{color:#c97000}
.topnav a[href="/annotations"].active{background:#856404;color:#fff}
.page-layout{display:flex;height:calc(100vh - 50px);margin-top:8px}
.method-sidebar{width:220px;background:#fff;border-right:1px solid #e0e0e5;display:flex;flex-direction:column;overflow-y:auto;padding:12px}
.method-sidebar h3{font-size:13px;color:#86868b;margin:8px 8px 6px;text-transform:uppercase;letter-spacing:.5px}
.method-nav-item{padding:10px 14px;border-radius:8px;cursor:pointer;transition:all .15s;margin-bottom:4px;font-size:14px;font-weight:500;color:#1d1d1f}
.method-nav-item:hover{background:#f0f0f3}
.method-nav-item.active{background:#e3f0ff;color:#0071e3;font-weight:600}
.method-nav-item .nav-desc{font-size:11px;color:#c0c0c5;margin-top:2px;font-weight:400}
.main-content{flex:1;overflow-y:auto;padding:20px 24px 40px;max-width:760px;margin:0 auto;width:100%}
header{margin-bottom:24px}
header h1{font-size:26px;margin-bottom:6px}
header p{color:#86868b;font-size:14px}
.method-desc{color:#86868b;font-size:13px;margin-bottom:20px;min-height:20px}
.input-card{background:#fff;border-radius:16px;padding:28px;box-shadow:0 2px 12px rgba(0,0,0,0.06);margin-bottom:24px}
.form-group{margin-bottom:14px}
.form-group input{width:100%;padding:12px 16px;border:1.5px solid #e0e0e5;border-radius:10px;font-size:15px;outline:none;transition:border-color .2s}
.form-group input:focus{border-color:#0071e3}
.form-group input::placeholder{color:#c0c0c5}
.input-label{font-size:13px;color:#86868b;margin-bottom:6px;font-weight:500}
.input-label .req{color:#c0392b}
#startBtn{width:100%;padding:14px;border:none;border-radius:10px;background:#0071e3;color:#fff;font-size:16px;font-weight:600;cursor:pointer;transition:opacity .2s}
#startBtn:hover{opacity:.9}
#startBtn:disabled{opacity:.4;cursor:not-allowed}
.progress-section{display:none;margin-bottom:24px}
.progress-section.active{display:block}
.steps{display:flex;flex-wrap:wrap;gap:8px;margin-bottom:16px}
.step{display:flex;align-items:center;gap:6px;padding:6px 12px;border-radius:20px;font-size:13px;background:#f0f0f3;color:#86868b;transition:all .3s}
.step.running{background:#e3f0ff;color:#0071e3;font-weight:600}
.step.done{background:#d4edda;color:#155724}
.step-dot{width:8px;height:8px;border-radius:50%;background:#c0c0c5}
.step.running .step-dot{background:#0071e3;animation:pulse 1s infinite}
.step.done .step-dot{background:#155724}
@keyframes pulse{0%,100%{opacity:1}50%{opacity:.4}}
.log-box{background:#fff;border-radius:12px;padding:16px;box-shadow:0 1px 4px rgba(0,0,0,0.06);max-height:240px;overflow-y:auto}
.log-line{padding:4px 0;font-size:13px;color:#6e6e73;border-bottom:1px solid #f5f5f7}
.log-line:last-child{border-bottom:none}
.log-time{color:#0071e3;font-weight:600;margin-right:8px}
.result-section{display:none;margin-bottom:24px}
.result-section.active{display:block}
.result-card{background:#fff;border-radius:16px;padding:28px;box-shadow:0 2px 12px rgba(0,0,0,0.06)}
.result-card h2{font-size:20px;margin-bottom:16px}
.result-stats{display:grid;grid-template-columns:repeat(3,1fr);gap:12px;margin-bottom:20px}
.stat{text-align:center;padding:12px;background:#f5f5f7;border-radius:10px}
.stat-num{font-size:24px;font-weight:700;color:#0071e3}
.stat-label{font-size:12px;color:#86868b;margin-top:2px}
.result-actions{display:flex;gap:12px}
.btn-link{flex:1;padding:12px;border-radius:10px;text-align:center;font-size:14px;font-weight:600;text-decoration:none;transition:opacity .2s}
.btn-html{background:#0071e3;color:#fff}
.btn-json{background:#f0f0f3;color:#1d1d1f}
.btn-link:hover{opacity:.9}
.check-list{margin-top:16px}
.check-item{display:flex;align-items:center;gap:8px;padding:6px 0;font-size:13px}
.check-badge{padding:2px 8px;border-radius:4px;font-size:11px;font-weight:600}
.check-pass{background:#d4edda;color:#155724}
.check-warn{background:#fff3cd;color:#856404}
.history-section{margin-bottom:24px}
.history-section h3{font-size:16px;margin-bottom:12px;color:#86868b}
.history-list{display:flex;flex-direction:column;gap:8px}
.history-item{display:flex;align-items:center;justify-content:space-between;padding:12px 16px;background:#fff;border-radius:10px;box-shadow:0 1px 4px rgba(0,0,0,0.06);text-decoration:none;color:inherit;transition:box-shadow .2s}
.history-item:hover{box-shadow:0 2px 8px rgba(0,0,0,0.1)}
.history-brand{font-weight:600}
.history-meta{font-size:12px;color:#86868b}
.history-link{color:#0071e3;font-size:13px;font-weight:600}
.error-msg{color:#c0392b;background:#fdf0ed;border-radius:10px;padding:14px 18px;margin-top:12px;font-size:14px}
</style>
</head>
<body>
<nav class="topnav">
  <a href="/" class="active">分析</a>
  <a href="/audit">官网诊断</a>
  <a href="/chat">对话</a>
  <a href="/skills">Skill管理</a>
  <a href="/annotations">标注汇总</a>
</nav>
<div class="page-layout">
  <div class="method-sidebar">
    <h3>分析方法</h3>
    <div id="methodNav"></div>
  </div>
  <div class="main-content">
    <header>
      <h1>GEO搜索问句挖掘Agent</h1>
      <p>选择左侧分析方法,输入品牌名称,自动挖掘搜索问句与意图分析</p>
    </header>

    <div class="method-desc" id="methodDesc"></div>

    <div class="input-card" id="inputCard">
      <div style="color:#c0c0c5;text-align:center;padding:20px 0">加载中...</div>
    </div>

    <div class="progress-section" id="progressSection">
      <div class="steps" id="stepsContainer"></div>
      <div class="log-box" id="logBox"></div>
    </div>

    <div class="result-section" id="resultSection"></div>

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
    selectMethod(methods[0].id);
  } catch(e) {
    document.getElementById("inputCard").innerHTML = '<div style="color:#c0392b;text-align:center;padding:20px 0">加载失败</div>';
  }
}

function renderMethodNav() {
  const c = document.getElementById("methodNav");
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
  document.getElementById("methodDesc").textContent = currentMethod.description || "";
  renderInputForm();
  document.getElementById("progressSection").classList.remove("active");
  document.getElementById("resultSection").classList.remove("active");
  document.getElementById("resultSection").innerHTML = "";
}

function renderInputForm() {
  const card = document.getElementById("inputCard");
  const fields = currentMethod.input_fields || [];
  let html = "";
  fields.forEach(f => {
    html += '<div class="form-group">';
    if (f.label) {
      html += '<div class="input-label">' + f.label + (f.required ? '<span class="req">*</span>' : '') + '</div>';
    }
    html += '<input type="text" id="field_' + f.name + '" placeholder="' + (f.placeholder || "") + '" data-field="' + f.name + '" data-required="' + (f.required ? "1" : "0") + '">';
    html += '</div>';
  });
  html += '<button id="startBtn" onclick="startAnalysis()">开始分析</button>';
  card.innerHTML = html;
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
  const el = stepMap[baseId];
  if (!el) return;
  if (status === "running") {
    el.className = "step running";
  } else if (status === "done") {
    el.className = "step done";
  }
  const idx = steps.findIndex(s => s.id === baseId);
  for (let i = 0; i < idx; i++) {
    if (stepMap[steps[i].id]) stepMap[steps[i].id].className = "step done";
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

function startAnalysis() {
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

  const es = new EventSource(url);
  es.onmessage = function(e) {
    const data = JSON.parse(e.data);
    if (data.type === "progress") {
      setStepStatus(data.step, "running");
      addLog(data.elapsed, data.message);
    } else if (data.type === "done") {
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
    checkHtml = '<div style="margin-bottom:12px;font-size:14px;color:#86868b">自校验: ' + passCount + '/' + s.self_check.length + ' 通过</div>' + checkHtml;
    checkHtml += '</div>';
  }

  let fileLinks = "";
  if (s.files) {
    if (s.files.html) fileLinks += '<a class="btn-link btn-html" href="/result/' + s.files.html + '" target="_blank">查看HTML报告</a>';
    if (s.files.json) fileLinks += '<a class="btn-link btn-json" href="/result/' + s.files.json + '" target="_blank">下载JSON</a>';
  }

  sec.innerHTML = '<div class="result-card">' +
    '<h2>分析完成 — ' + s.brand + '</h2>' +
    '<div class="result-stats">' +
      '<div class="stat"><div class="stat-num">' + (s.product_count||0) + '</div><div class="stat-label">产品数</div></div>' +
      '<div class="stat"><div class="stat-num">' + (s.persona_count||0) + '</div><div class="stat-label">客群数</div></div>' +
      '<div class="stat"><div class="stat-num">' + (s.stage_count||0) + '</div><div class="stat-label">阶段数</div></div>' +
      '<div class="stat"><div class="stat-num">' + (s.total_queries||0) + '</div><div class="stat-label">问句总数</div></div>' +
      '<div class="stat"><div class="stat-num">' + (s.cluster_count||0) + '</div><div class="stat-label">意图簇数</div></div>' +
      '<div class="stat"><div class="stat-num">' + (s.elapsed||0).toFixed(1) + '</div><div class="stat-label">耗时(秒)</div></div>' +
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
    if (!data.reports || data.reports.length === 0) {
      list.innerHTML = '<div style="color:#c0c0c5;font-size:13px;padding:8px 0">暂无历史报告</div>';
      return;
    }
    list.innerHTML = data.reports.map(r =>
      '<a class="history-item" href="/result/' + r.filename + '" target="_blank">' +
        '<div>' +
          '<div class="history-brand">' + r.brand + '</div>' +
          '<div class="history-meta">' + r.created + ' · ' + r.size_kb + ' KB</div>' +
        '</div>' +
        '<div class="history-link">查看报告</div>' +
      '</a>'
    ).join("");
  } catch(e) {
    document.getElementById("historyList").innerHTML = '<div style="color:#c0c0c5;font-size:13px">加载失败</div>';
  }
}

loadMethods();
loadHistory();
</script>
</body>
</html>'''


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
body{font-family:-apple-system,"PingFang SC","Microsoft YaHei",sans-serif;background:#f5f5f7;color:#1d1d1f;line-height:1.6}
.topnav{display:flex;gap:8px;justify-content:center;padding:12px 0 0}
.topnav a{padding:6px 16px;border-radius:20px;font-size:13px;text-decoration:none;color:#86868b;background:#fff;box-shadow:0 1px 4px rgba(0,0,0,0.06);transition:all .2s}
.topnav a:hover{color:#0071e3}
.topnav a.active{background:#0071e3;color:#fff}
.editor-layout{display:flex;height:calc(100vh - 50px);margin-top:8px}
.sidebar{width:260px;background:#fff;border-right:1px solid #e0e0e5;overflow-y:auto;padding:12px;display:flex;flex-direction:column}
.sidebar h3{font-size:13px;color:#86868b;margin:12px 8px 6px;text-transform:uppercase;letter-spacing:.5px}
.sidebar h3:first-child{margin-top:0}
.method-nav-item{padding:8px 12px;border-radius:8px;cursor:pointer;transition:all .15s;font-size:13px;font-weight:500;color:#1d1d1f;margin-bottom:3px}
.method-nav-item:hover{background:#f0f0f3}
.method-nav-item.active{background:#e3f0ff;color:#0071e3;font-weight:600}
.add-method-btn{display:flex;align-items:center;justify-content:center;gap:6px;padding:8px;border:1.5px dashed #c0c0c5;border-radius:8px;cursor:pointer;font-size:13px;color:#86868b;transition:all .15s;margin-bottom:8px}
.add-method-btn:hover{border-color:#0071e3;color:#0071e3}
.file-item{display:flex;align-items:center;gap:8px;padding:8px 12px;border-radius:8px;cursor:pointer;transition:background .15s}
.file-item:hover{background:#f0f0f3}
.file-item.active{background:#e3f0ff}
.file-item .file-num{font-size:11px;color:#c0c0c5;min-width:18px}
.file-item .file-name{font-size:14px;font-weight:500;flex:1}
.file-item .file-type{font-size:10px;padding:2px 6px;border-radius:4px;background:#f0f0f3;color:#86868b}
.file-item.active .file-type{background:#d4edda;color:#155724}
.methods-config-item{padding:8px 12px;border-radius:8px;cursor:pointer;transition:background .15s;display:flex;align-items:center;gap:8px}
.methods-config-item:hover{background:#f0f0f3}
.methods-config-item.active{background:#e3f0ff}
.methods-config-item .icon{font-size:16px}
.methods-config-item .name{font-size:14px;font-weight:500}
.editor-main{flex:1;display:flex;flex-direction:column;overflow:hidden}
.editor-header{display:flex;align-items:center;justify-content:space-between;padding:12px 20px;background:#fff;border-bottom:1px solid #e0e0e5}
.editor-header .path{font-size:13px;color:#86868b;font-family:monospace}
.editor-header .path b{color:#1d1d1f}
.save-btn{padding:8px 20px;border:none;border-radius:8px;background:#0071e3;color:#fff;font-size:14px;font-weight:600;cursor:pointer;transition:opacity .2s}
.save-btn:hover{opacity:.9}
.save-btn:disabled{opacity:.4;cursor:not-allowed}
.editor-body{flex:1;display:flex;overflow:hidden}
.editor-textarea{flex:1;border:none;outline:none;padding:16px 20px;font-family:"SF Mono","Menlo","Consolas",monospace;font-size:13px;line-height:1.6;background:#fff;color:#1d1d1f;resize:none;tab-size:2}
.editor-textarea:focus{background:#fcfcfe}
.status-bar{padding:6px 20px;background:#fff;border-top:1px solid #e0e0e5;font-size:12px;color:#86868b}
.status-bar .ok{color:#155724}
.status-bar .err{color:#c0392b}
.empty-state{flex:1;display:flex;align-items:center;justify-content:center;color:#c0c0c5;font-size:15px}
/* Method config panel */
.method-config{flex:1;overflow-y:auto;padding:24px;background:#fff}
.config-section{margin-bottom:28px}
.config-section h4{font-size:15px;color:#1d1d1f;margin-bottom:12px;padding-bottom:6px;border-bottom:1px solid #f0f0f3}
.config-field{margin-bottom:14px}
.config-field label{display:block;font-size:13px;color:#86868b;margin-bottom:5px;font-weight:500}
.config-field input,.config-field textarea{width:100%;padding:10px 14px;border:1.5px solid #e0e0e5;border-radius:8px;font-size:14px;outline:none;transition:border-color .2s;font-family:inherit}
.config-field input:focus,.config-field textarea:focus{border-color:#0071e3}
.config-field textarea{min-height:60px;resize:vertical}
.config-field .hint{font-size:12px;color:#c0c0c5;margin-top:4px}
.skill-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(200px,1fr));gap:8px}
.skill-toggle{display:flex;align-items:center;justify-content:space-between;padding:8px 12px;border-radius:8px;border:1.5px solid #e0e0e5;cursor:pointer;transition:all .15s}
.skill-toggle:hover{border-color:#0071e3}
.skill-toggle.assigned{background:#d4edda;border-color:#155724}
.skill-toggle .st-name{font-size:13px;font-weight:500}
.skill-toggle .st-type{font-size:10px;padding:2px 6px;border-radius:4px;background:#f0f0f3;color:#86868b}
.skill-toggle.assigned .st-type{background:#155724;color:#fff}
.skill-toggle .st-status{font-size:12px;font-weight:600;color:#86868b}
.skill-toggle.assigned .st-status{color:#155724}
.input-field-card{border:1px solid #e0e0e5;border-radius:10px;padding:14px;margin-bottom:10px;position:relative;background:#fcfcfe}
.input-field-card .if-row{display:flex;gap:10px;margin-bottom:8px}
.input-field-card .if-row:last-child{margin-bottom:0}
.input-field-card .if-group{flex:1}
.input-field-card .if-group label{display:block;font-size:11px;color:#86868b;margin-bottom:3px}
.input-field-card .if-group input{width:100%;padding:7px 10px;border:1px solid #e0e0e5;border-radius:6px;font-size:13px;outline:none}
.input-field-card .if-group input:focus{border-color:#0071e3}
.input-field-card .if-check{display:flex;align-items:center;gap:5px;font-size:12px;color:#86868b;white-space:nowrap}
.input-field-card .if-check input{margin:0}
.if-delete{position:absolute;top:10px;right:10px;background:none;border:none;font-size:16px;color:#c0392b;cursor:pointer;padding:2px 6px;border-radius:4px}
.if-delete:hover{background:#fdf0ed}
.add-field-btn{display:flex;align-items:center;justify-content:center;gap:6px;padding:8px;border:1.5px dashed #c0c0c5;border-radius:8px;cursor:pointer;font-size:13px;color:#86868b;transition:all .15s;width:100%}
.add-field-btn:hover{border-color:#0071e3;color:#0071e3}
.config-actions{display:flex;gap:12px;margin-top:20px;padding-top:16px;border-top:1px solid #f0f0f3}
.btn-primary{padding:10px 24px;border:none;border-radius:8px;background:#0071e3;color:#fff;font-size:14px;font-weight:600;cursor:pointer;transition:opacity .2s}
.btn-primary:hover{opacity:.9}
.btn-danger{padding:10px 24px;border:1.5px solid #c0392b;border-radius:8px;background:#fff;color:#c0392b;font-size:14px;font-weight:600;cursor:pointer;transition:all .2s}
.btn-danger:hover{background:#c0392b;color:#fff}
/* New method dialog */
.dialog-overlay{position:fixed;top:0;left:0;width:100%;height:100%;background:rgba(0,0,0,0.3);display:flex;align-items:center;justify-content:center;z-index:1000}
.dialog-box{background:#fff;border-radius:16px;padding:28px;width:400px;max-width:90vw;box-shadow:0 8px 32px rgba(0,0,0,0.15)}
.dialog-box h3{font-size:18px;margin-bottom:16px}
.dialog-box .config-field{margin-bottom:14px}
.dialog-box .dialog-actions{display:flex;gap:12px;justify-content:flex-end;margin-top:20px}
</style>
</head>
<body>
<nav class="topnav">
  <a href="/">分析</a>
  <a href="/audit">官网诊断</a>
  <a href="/chat">对话</a>
  <a href="/skills" class="active">Skill管理</a>
  <a href="/annotations">标注汇总</a>
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
  html += '<div class="config-field"><label>方法ID</label><input type="text" value="' + method.id + '" disabled style="background:#f5f5f7"></div>';
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
      '<button class="btn-danger" style="border-color:#e0e0e5;color:#86868b" onclick="closeNewMethodDialog()">取消</button>' +
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
body{font-family:-apple-system,"PingFang SC","Microsoft YaHei",sans-serif;background:#f5f5f7;color:#1d1d1f;line-height:1.6}
.topnav{display:flex;gap:8px;justify-content:center;padding:12px 0 0}
.topnav a{padding:6px 16px;border-radius:20px;font-size:13px;text-decoration:none;color:#86868b;background:#fff;box-shadow:0 1px 4px rgba(0,0,0,0.06);transition:all .2s}
.topnav a:hover{color:#0071e3}
.topnav a.active{background:#0071e3;color:#fff}
.chat-container{display:flex;height:calc(100vh - 50px)}
.sidebar{width:240px;background:#fff;border-right:1px solid #e0e0e5;display:flex;flex-direction:column}
.sidebar-header{padding:12px;border-bottom:1px solid #e0e0e5}
.new-chat-btn{width:100%;padding:10px;border:1.5px solid #0071e3;border-radius:10px;background:#fff;color:#0071e3;font-size:14px;font-weight:600;cursor:pointer;transition:all .2s}
.new-chat-btn:hover{background:#0071e3;color:#fff}
.conv-list{flex:1;overflow-y:auto;padding:8px}
.conv-list h3{font-size:12px;color:#c0c0c5;margin:8px 8px 4px;text-transform:uppercase}
.conv-item{padding:10px 12px;border-radius:8px;cursor:pointer;transition:background .15s;margin-bottom:4px}
.conv-item:hover{background:#f0f0f3}
.conv-item.active{background:#e3f0ff}
.conv-item .preview{font-size:13px;color:#1d1d1f;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.conv-item .time{font-size:11px;color:#c0c0c5;margin-top:2px}
.chat-main{flex:1;display:flex;flex-direction:column;overflow:hidden}
.chat-header{padding:8px 24px;border-bottom:1px solid #f0f0f3}
.chat-header h2{font-size:16px;color:#1d1d1f}
.messages-area{flex:1;overflow-y:auto;padding:16px 24px;max-width:800px;margin:0 auto;width:100%}
.message{display:flex;margin-bottom:12px}
.message.user{justify-content:flex-end}
.message.agent{justify-content:flex-start}
.bubble{max-width:75%;padding:10px 16px;border-radius:16px;font-size:14px;white-space:pre-wrap;word-break:break-word}
.message.user .bubble{background:#0071e3;color:#fff}
.message.agent .bubble{background:#fff;color:#1d1d1f;box-shadow:0 1px 4px rgba(0,0,0,0.06)}
.tool-event{font-size:13px;color:#86868b;padding:4px 12px;margin:4px 0;border-radius:8px;background:#f0f0f3}
.tool-event .tool-status{font-weight:600;margin-right:6px}
.thinking{display:flex;gap:4px;padding:12px 16px}
.thinking .dot{width:8px;height:8px;border-radius:50%;background:#c0c0c5;animation:bounce 1.4s infinite ease-in-out}
.thinking .dot:nth-child(1){animation-delay:-.32s}
.thinking .dot:nth-child(2){animation-delay:-.16s}
@keyframes bounce{0%,80%,100%{transform:scale(0)}40%{transform:scale(1)}}
.input-area{padding:12px 24px;background:#fff;border-top:1px solid #e0e0e5;display:flex;gap:12px;max-width:800px;margin:0 auto;width:100%}
.input-area textarea{flex:1;resize:none;border:1.5px solid #e0e0e5;border-radius:10px;padding:10px 14px;font-size:14px;outline:none;font-family:inherit;max-height:120px;transition:border-color .2s}
.input-area textarea:focus{border-color:#0071e3}
.send-btn{padding:10px 20px;border:none;border-radius:10px;background:#0071e3;color:#fff;font-size:14px;font-weight:600;cursor:pointer;transition:opacity .2s}
.send-btn:hover{opacity:.9}
.send-btn:disabled{opacity:.4;cursor:not-allowed}
.welcome{text-align:center;padding:60px 24px;color:#c0c0c5}
.welcome h3{font-size:18px;margin-bottom:8px;color:#86868b}
.welcome p{font-size:14px}
.welcome .examples{margin-top:20px;display:flex;flex-direction:column;gap:8px;align-items:center}
.welcome .example{padding:8px 16px;background:#fff;border-radius:20px;font-size:13px;color:#86868b;cursor:pointer;transition:all .2s;box-shadow:0 1px 4px rgba(0,0,0,0.06)}
.welcome .example:hover{color:#0071e3}
</style>
</head>
<body>
<nav class="topnav">
  <a href="/">分析</a>
  <a href="/audit">官网诊断</a>
  <a href="/chat" class="active">对话</a>
  <a href="/skills">Skill管理</a>
  <a href="/annotations">标注汇总</a>
</nav>
<div class="chat-container">
  <div class="sidebar">
    <div class="sidebar-header">
      <button class="new-chat-btn" onclick="newChat()">+ 新对话</button>
    </div>
    <div class="conv-list" id="convList">
      <h3>历史对话</h3>
      <div style="color:#c0c0c5;font-size:13px;padding:8px">加载中...</div>
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
      list.innerHTML='<h3>历史对话</h3><div style="color:#c0c0c5;font-size:13px;padding:8px">暂无</div>';
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
body{font-family:-apple-system,"PingFang SC","Microsoft YaHei",sans-serif;background:#f5f5f7;color:#1d1d1f;line-height:1.6}
.topnav{display:flex;gap:8px;justify-content:center;padding:12px 0 0}
.topnav a{padding:6px 16px;border-radius:20px;font-size:13px;text-decoration:none;color:#86868b;background:#fff;box-shadow:0 1px 4px rgba(0,0,0,0.06);transition:all .2s}
.topnav a:hover{color:#0071e3}
.topnav a.active{background:#0071e3;color:#fff}
.app{max-width:900px;margin:0 auto;padding:20px 24px 40px}
header{margin-bottom:24px}
header h1{font-size:26px;margin-bottom:6px}
header p{color:#86868b;font-size:14px}
.summary-bar{display:flex;gap:16px;margin-bottom:24px;align-items:center}
.summary-stat{background:#fff;border-radius:12px;padding:12px 20px;box-shadow:0 1px 4px rgba(0,0,0,0.06);display:flex;align-items:center;gap:8px}
.summary-stat .num{font-size:24px;font-weight:700;color:#c0392b}
.summary-stat .label{font-size:13px;color:#86868b}
.filter-row{display:flex;gap:8px;margin-bottom:16px;align-items:center}
.filter-row select{padding:6px 12px;border:1.5px solid #e0e0e5;border-radius:8px;font-size:13px;outline:none;background:#fff}
.filter-row input{padding:6px 12px;border:1.5px solid #e0e0e5;border-radius:8px;font-size:13px;outline:none;flex:1;background:#fff}
.filter-row input:focus{border-color:#0071e3}
.issue-list{display:flex;flex-direction:column;gap:12px}
.issue-card{background:#fff;border-radius:12px;padding:20px;box-shadow:0 1px 4px rgba(0,0,0,0.06);border-left:3px solid #c0392b}
.issue-header{display:flex;justify-content:space-between;align-items:flex-start;margin-bottom:10px}
.issue-brand{font-size:16px;font-weight:600}
.issue-section{font-size:13px;color:#86868b;margin-bottom:6px}
.issue-section b{color:#1d1d1f}
.issue-comment{background:#fff5f5;border-radius:8px;padding:10px 14px;font-size:14px;color:#c0392b;border-left:3px solid #c0392b;margin-bottom:10px;white-space:pre-wrap}
.issue-snippet{font-size:13px;color:#6e6e73;background:#f5f5f7;border-radius:6px;padding:8px 12px;margin-bottom:10px;max-height:80px;overflow-y:auto}
.issue-footer{display:flex;justify-content:space-between;align-items:center;font-size:12px;color:#c0c0c5}
.issue-footer a{color:#0071e3;text-decoration:none;font-weight:600}
.issue-footer a:hover{text-decoration:underline}
.copy-btn{padding:4px 12px;border:1px solid #e0e0e5;border-radius:6px;font-size:12px;cursor:pointer;background:#fff;color:#86868b;transition:all .15s}
.copy-btn:hover{border-color:#0071e3;color:#0071e3}
.empty-state{text-align:center;color:#c0c0c5;padding:60px 0;font-size:15px}
</style>
</head>
<body>
<nav class="topnav">
  <a href="/">分析</a>
  <a href="/audit">官网诊断</a>
  <a href="/chat">对话</a>
  <a href="/skills">Skill管理</a>
  <a href="/annotations" class="active">标注汇总</a>
</nav>
<div class="app">
  <header>
    <h1>标注问题汇总</h1>
    <p>所有报告中标记为"有问题"的条目,可复制后发给Agent定位问题</p>
  </header>

  <div class="summary-bar">
    <div class="summary-stat"><span class="num" id="totalCount">0</span><span class="label">个问题</span></div>
    <div class="summary-stat"><span class="num" id="reportCount" style="color:#0071e3">0</span><span class="label">个报告</span></div>
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
body{font-family:-apple-system,"PingFang SC","Microsoft YaHei",sans-serif;background:#f5f5f7;color:#1d1d1f;line-height:1.6}
.topnav{display:flex;gap:8px;justify-content:center;padding:12px 0 0}
.topnav a{padding:6px 16px;border-radius:20px;font-size:13px;text-decoration:none;color:#86868b;background:#fff;box-shadow:0 1px 4px rgba(0,0,0,0.06);transition:all .2s}
.topnav a:hover{color:#0071e3}
.topnav a.active{background:#0071e3;color:#fff}
.wrap{max-width:1200px;margin:0 auto;padding:24px 24px 40px}
header h1{font-size:26px;margin-bottom:6px}
header p{color:#86868b;font-size:14px;margin-bottom:20px}
.audit-form{background:#fff;border-radius:16px;padding:20px;box-shadow:0 1px 4px rgba(0,0,0,0.06);display:flex;gap:12px;margin-bottom:20px}
.audit-form input{flex:1;padding:12px 16px;border:1.5px solid #e0e0e5;border-radius:10px;font-size:15px;outline:none;transition:border-color .2s;font-family:inherit}
.audit-form input:focus{border-color:#0071e3}
.audit-form button{padding:12px 28px;border:none;border-radius:10px;background:#0071e3;color:#fff;font-size:15px;font-weight:600;cursor:pointer;transition:opacity .2s;white-space:nowrap}
.audit-form button:hover{opacity:.9}
.audit-form button:disabled{opacity:.5;cursor:not-allowed}
.hint{font-size:12px;color:#c0c0c5;margin-top:8px}
.msg{padding:12px 16px;border-radius:10px;font-size:14px;margin-bottom:16px;display:none}
.msg.err{display:block;background:#fdf0ed;color:#c0392b;border:1px solid #f5c6cb}
.loading{display:none;text-align:center;padding:40px;background:#fff;border-radius:16px;box-shadow:0 1px 4px rgba(0,0,0,0.06);margin-bottom:20px}
.spinner{width:36px;height:36px;border:3px solid #e0e0e5;border-top-color:#0071e3;border-radius:50%;animation:spin .8s linear infinite;margin:0 auto 14px}
@keyframes spin{to{transform:rotate(360deg)}}
.loading .lt{font-size:15px;font-weight:600;margin-bottom:4px}
.loading .ls{font-size:13px;color:#86868b}
.result{display:none}
.report-bar{display:flex;align-items:center;justify-content:space-between;margin-bottom:10px}
.report-bar .meta{font-size:13px;color:#86868b}
.report-bar a{font-size:13px;color:#0071e3;text-decoration:none;font-weight:600}
.report-frame{width:100%;height:60vh;border:1px solid #e0e0e5;border-radius:14px;background:#fff}
.report-expand{display:none;margin:3px 0 16px 0;padding:16px;background:#fff;border-radius:12px;box-shadow:0 1px 3px rgba(0,0,0,0.05);border-top:2px solid #0071e3}
.report-expand.show{display:block}
.report-expand .expand-bar{display:flex;justify-content:space-between;align-items:center;margin-bottom:12px}
.report-expand .expand-bar .meta{font-size:13px;color:#6b7280}
.report-expand .expand-bar a{font-size:13px;color:#0071e3;text-decoration:none}
.report-expand .expand-bar a:hover{text-decoration:underline}
.history-section{margin-top:24px}
.history-section h3{font-size:16px;margin-bottom:12px;color:#1d1d1f}
.report-list{display:flex;flex-direction:column;gap:8px}
.report-item{display:flex;align-items:center;gap:12px;padding:14px 16px;background:#fff;border-radius:12px;box-shadow:0 1px 3px rgba(0,0,0,0.05);cursor:pointer;transition:all .15s;border:2px solid transparent}
.report-item:hover{border-color:#0071e3;box-shadow:0 2px 8px rgba(0,113,227,.12)}
.report-item.active{border-color:#0071e3;background:#f0f7ff}
.report-item .badge-new{background:#ef4444;color:#fff;font-size:10px;font-weight:700;padding:1px 6px;border-radius:8px;flex-shrink:0}
.report-item .badge-grade{font-size:13px;font-weight:700;color:#fff;padding:2px 10px;border-radius:10px;flex-shrink:0;min-width:28px;text-align:center}
.report-item .badge-brand{font-size:12px;font-weight:600;color:#5856d6;background:#eef0ff;padding:2px 8px;border-radius:6px;flex-shrink:0;max-width:120px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.report-item .r-url{font-size:14px;color:#1d1d1f;font-weight:500;flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.report-item .r-score{font-size:20px;font-weight:700;flex-shrink:0;min-width:48px;text-align:right}
.report-item .r-time{font-size:12px;color:#86868b;flex-shrink:0;min-width:100px;text-align:right}
.report-item .r-tokens{position:relative;font-size:11px;font-weight:600;color:#5C3CF1;background:#5C3CF11a;padding:2px 8px;border-radius:6px;flex-shrink:0;white-space:nowrap;cursor:default}
.report-item .r-tokens .tok-tip{display:none;position:absolute;top:calc(100% + 6px);right:0;background:#1d1d1f;color:#fff;font-size:11px;font-weight:400;padding:5px 10px;border-radius:6px;white-space:nowrap;z-index:20;box-shadow:0 2px 8px rgba(0,0,0,.18);pointer-events:none}
.report-item .r-tokens:hover .tok-tip{display:block}
.report-item .r-stats{font-size:11px;color:#86868b;flex-shrink:0}
.report-item .r-stats .s-pass{color:#16a34a}
.report-item .r-stats .s-warn{color:#d97706}
.report-item .r-stats .s-fail{color:#dc2626}
.report-item .btn-del{flex-shrink:0;width:28px;height:28px;border:none;border-radius:8px;background:transparent;color:#c0c0c5;font-size:16px;cursor:pointer;display:flex;align-items:center;justify-content:center;transition:all .15s}
.report-item .btn-del:hover{background:#fef2f2;color:#dc2626}
.report-item .btn-del.confirming{background:#dc2626;color:#fff;font-size:11px;font-weight:600}
</style>
</head>
<body>
<nav class="topnav">
  <a href="/">分析</a>
  <a href="/audit" class="active">官网诊断</a>
  <a href="/chat">对话</a>
  <a href="/skills">Skill管理</a>
  <a href="/annotations">标注汇总</a>
</nav>
  <div class="wrap">
  <header>
    <h1>GEO 官网诊断 <span style="font-size:14px;font-weight:600;color:#86868b;vertical-align:middle;">（V{{engine_version}}）</span></h1>
    <p>输入官网网址，自动抽样抓取内页，从技术可抓取性、AI友好度、内容质量、权威信任、商业转化与决策内容 5 个维度执行数十项检查（含断链、重复元信息、关键词蚕食等跨页面检测）并生成诊断报告</p>
  </header>

  <div class="audit-form">
    <input id="urlInput" type="text" placeholder="例如 https://www.example.com 或直接输入域名"
           onkeydown="if(event.key==='Enter')startAudit()">
    <button id="auditBtn" onclick="startAudit()">开始诊断</button>
  </div>
  <div class="hint">需抓取首页、抽样内页及 robots/sitemap/llms.txt 等配置并检查站内链接，约需 1～2 分钟，请耐心等待</div>

  <div class="msg" id="errMsg"></div>

  <div class="loading" id="loadingBox">
    <div class="spinner"></div>
    <div class="lt">正在诊断中…</div>
    <div class="ls" id="loadingStep">正在抓取首页、抽样内页并执行 46 项检查</div>
  </div>

  <div class="history-section">
    <h3>历史报告</h3>
    <div class="report-list" id="reportList">
      <div style="text-align:center;color:#86868b;font-size:13px;padding:20px">加载中…</div>
    </div>
  </div>
</div>

<script>
const GRADE_COLOR = {A:'#16a34a',B:'#0071e3',C:'#d97706',D:'#ea580c',F:'#dc2626'};
function barColor(s){return s>=90?'#16a34a':s>=75?'#0071e3':s>=60?'#d97706':s>=40?'#ea580c':'#dc2626';}

async function startAudit(){
  const url = document.getElementById('urlInput').value.trim();
  const btn = document.getElementById('auditBtn');
  const errBox = document.getElementById('errMsg');
  const loading = document.getElementById('loadingBox');
  errBox.classList.remove('err'); errBox.style.display='none';
  if(!url){errBox.textContent='请输入要诊断的网址';errBox.classList.add('err');errBox.style.display='block';return;}
  if(!window.confirm('当前检测大约需要1～2分钟左右，是否确认开始？')) return;
  btn.disabled=true; btn.textContent='诊断中…';
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
    btn.disabled=false; btn.textContent='开始诊断';
  }
}

function renderResult(d){
  document.getElementById('loadingBox').style.display='none';
  // 新诊断完成后，加载历史列表并自动展开最新的
  loadReportList(d.html_file);
}

let currentReportFile = null;
async function loadReportList(activeFile){
  currentReportFile = activeFile || null;
  try{
    const resp = await fetch('/api/audit/reports');
    const reports = await resp.json();
    const listEl = document.getElementById('reportList');
    if(!reports.length){
      listEl.innerHTML = '<div style="text-align:center;color:#86868b;font-size:13px;padding:20px">暂无历史报告</div>';
      return;
    }
    listEl.innerHTML = reports.map((r,i)=>{
      const gc = GRADE_COLOR[r.grade] || '#86868b';
      const score = r.score !== undefined ? r.score.toFixed(1) : '-';
      const grade = r.grade || '?';
      const url = r.url || r.filename;
      const time = r.timestamp || r.mtime || '';
      const isNew = i === 0 && !activeFile;
      const isActive = r.html_file === currentReportFile;
      const stats = r.total ? '<span class="r-stats">✓<span class="s-pass">'+r.pass+'</span> ⚠<span class="s-warn">'+r.warn+'</span> ✕<span class="s-fail">'+r.fail+'</span></span>' : '';
      const brandTag = r.brand_name ? '<span class="badge-brand">'+r.brand_name+'</span>' : '';
      const tokenTag = r.cost !== undefined ? '<span class="r-tokens">token支出：¥'+(r.cost < 0.01 ? r.cost.toFixed(3) : r.cost.toFixed(2))+'<span class="tok-tip">LLM调用 '+r.token_calls+' 次 · 共消耗 '+r.tokens.toLocaleString()+' tokens</span></span>' : '';
      return '<div class="report-item'+(isActive?' active':'')+'" data-file="'+r.html_file+'">'+
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
    }).join('');
    document.querySelectorAll('.report-item').forEach(function(el){
      el.addEventListener('click', function(e){
        if(e.target.classList.contains('btn-del')) return;
        viewReport(el.getAttribute('data-file'), el);
      });
    });
    document.querySelectorAll('.btn-del').forEach(function(btn){
      btn.addEventListener('click', function(e){
        e.stopPropagation();
        handleDelete(this);
      });
    });
    // 如果有激活项，自动展开
    if(activeFile){
      const activeEl = document.querySelector('.report-item.active');
      if(activeEl) viewReport(activeFile, activeEl);
    }
  }catch(e){
    document.getElementById('reportList').innerHTML = '<div style="text-align:center;color:#c0392b;font-size:13px;padding:20px">加载失败</div>';
  }
}

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

// 页面加载时自动获取历史报告列表
loadReportList();
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
