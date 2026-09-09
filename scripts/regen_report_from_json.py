# -*- coding: utf-8 -*-
"""从已保存的报告 JSON 重建 AuditReport 并按当前代码口径重新生成 HTML+JSON。

用途：计分口径调整后，用同一份原始检测结果重算总分与报告，
不重新抓站、不调用 LLM，快照凭证不受影响。

用法: python scripts/regen_report_from_json.py <report.json> [输出目录]
"""
import sys
import json
from datetime import datetime
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from site_audit.core.models import (AuditReport, CheckResult, Category,
                                    Priority, Status)
from site_audit.core.scorer import build_report
from site_audit.report.json_report import save_json_report
from site_audit.report.html_report import save_html_report


def rebuild(json_path: str, output_dir: str = None):
    data = json.loads(Path(json_path).read_text(encoding="utf-8"))
    results = []
    for r in data["results"]:
        results.append(CheckResult(
            item_id=r["id"],
            item_name=r["name"],
            category=Category(r["category"]),
            priority=Priority(r["priority"]),
            status=Status(r["status"]),
            message=r.get("message", ""),
            detail=r.get("detail"),
            score=r.get("score", 0.0),
            manual_check=r.get("manual_check", ""),
            purpose=r.get("purpose", ""),
            impact=r.get("impact", ""),
            scope=r.get("scope", "首页"),
            applicable_pages=r.get("applicable_pages", "首页"),
            suggestion=r.get("suggestion", ""),
            excellent=r.get("excellent", ""),
            normal=r.get("normal", ""),
            failed=r.get("failed", ""),
            tier=r.get("tier", "core"),
            # 必须恢复 pages（结果实际涉及的具体页面路径），否则报告来源退回"抽样内容页"兜底
            pages=r.get("pages", []),
        ))
    report = build_report(
        data["url"], data["final_url"], data["timestamp"], results,
        sampled_pages=data.get("sampled_pages", []),
        prompt_version=data.get("prompt_version", ""),
        token_usage=data.get("token_usage") or None,
        crawl_stats=data.get("crawl_stats", {}) or {},
        light_scan_meta=data.get("light_scan_meta", []) or [],
    )
    out = Path(output_dir) if output_dir else Path(json_path).parent
    out.mkdir(parents=True, exist_ok=True)
    stem = Path(json_path).stem
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    new_stem = f"{stem.rsplit('_', 2)[0]}_regen_{ts}"
    html_path = save_html_report(report, str(out / f"{new_stem}.html"))
    json_path = save_json_report(report, str(out / f"{new_stem}.json"))
    print(f"总分: {report.total_score} ({data.get('total_score')} → 重算)")
    print(f"等级: {report.grade}")
    print(f"HTML: {html_path}")
    print(f"JSON: {json_path}")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)
    rebuild(sys.argv[1], sys.argv[2] if len(sys.argv) > 2 else None)
