"""JSON 报告生成"""
import json
from site_audit.report.html_report import ENGINE_VERSION
from site_audit.core.models import AuditReport, Status


def report_to_dict(report: AuditReport) -> dict:
    """将报告转为字典"""
    return {
        "url": report.url,
        "final_url": report.final_url,
        "site_brand": report.site_brand or "",
        "timestamp": report.timestamp,
        "engine_version": ENGINE_VERSION,
        "prompt_version": report.prompt_version,
        "token_usage": report.token_usage,
        "total_score": report.total_score,
        "grade": report.grade,
        "summary": report.summary,
        "sampled_pages": report.sampled_pages,
        "categories": [
            {
                "name": c.category.value,
                "score": c.score,
                "weight": c.weight,
                "total": c.total,
                "pass": c.passed,
                "warn": c.warned,
                "fail": c.failed,
                "skip": c.skipped,
            }
            for c in report.categories
        ],
        "results": [
            {
                "id": r.item_id,
                "name": r.item_name,
                "category": r.category.value,
                "priority": r.priority.value,
                "status": r.status.value,
                "score": r.score,
                "message": r.message,
                "detail": r.detail,
                "manual_check": r.manual_check,
                "purpose": r.purpose,
                "impact": r.impact,
                "scope": r.scope,
                "applicable_pages": r.applicable_pages,
                "suggestion": r.suggestion,
                "excellent": r.excellent,
                "normal": r.normal,
                "failed": r.failed,
                "tier": r.tier,
                "skip_kind": r.skip_kind,
                "pages": r.pages or [],
            }
            for r in report.results
        ],
    }


def save_json_report(report: AuditReport, output_path: str):
    """保存 JSON 报告"""
    data = report_to_dict(report)
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    return output_path
