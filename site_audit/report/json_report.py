"""JSON 报告生成"""
import json
from site_audit.report.html_report import ENGINE_VERSION
from site_audit.core.models import AuditReport, Status


def report_from_dict(data: dict) -> AuditReport:
    """从 save_json_report 保存的字典重建报告对象。

    用于优先级配置变更后重算历史报告的计分与排序并重新渲染；
    说明类字段 description/check_method/standard 未落盘，重建后为空，
    报告侧本就有"缺省回退 purpose"的展示逻辑，不影响阅读。
    """
    from site_audit.core.models import Category, CategoryScore, CheckResult, Priority

    results = [
        CheckResult(
            item_id=r.get("id", ""),
            item_name=r.get("name", ""),
            category=Category(r.get("category", "技术可抓取性")),
            priority=Priority(r.get("priority", "P1")),
            status=Status(r.get("status", "skip")),
            message=r.get("message", ""),
            detail=r.get("detail"),
            score=r.get("score", 0.0),
            manual_check=r.get("manual_check", ""),
            purpose=r.get("purpose", ""),
            impact=r.get("impact", ""),
            scope=r.get("scope", ""),
            applicable_pages=r.get("applicable_pages", ""),
            suggestion=r.get("suggestion", ""),
            excellent=r.get("excellent", ""),
            normal=r.get("normal", ""),
            failed=r.get("failed", ""),
            tier=r.get("tier", "core"),
            skip_kind=r.get("skip_kind", "na"),
            pages=r.get("pages") or [],
            priority_mark=r.get("priority_mark", ""),
            priority_reason=r.get("priority_reason", ""),
            root_cause=r.get("root_cause", ""),
        )
        for r in data.get("results", [])
    ]
    categories = [
        CategoryScore(
            category=Category(c.get("name", "技术可抓取性")),
            total=c.get("total", 0),
            passed=c.get("pass", 0),
            warned=c.get("warn", 0),
            failed=c.get("fail", 0),
            skipped=c.get("skip", 0),
            score=c.get("score", 0.0),
            weight=c.get("weight", 0.0),
        )
        for c in data.get("categories", [])
    ]
    return AuditReport(
        url=data.get("url", ""),
        final_url=data.get("final_url", ""),
        timestamp=data.get("timestamp", ""),
        total_score=data.get("total_score", 0.0),
        grade=data.get("grade", ""),
        categories=categories,
        results=results,
        sampled_pages=data.get("sampled_pages", []) or [],
        prompt_version=data.get("prompt_version", ""),
        token_usage=data.get("token_usage", {}) or {},
        site_brand=data.get("site_brand", ""),
        crawl_stats=data.get("crawl_stats", {}) or {},
        stories=data.get("stories", []) or [],
        light_scan_meta=data.get("light_scan_meta", []) or [],
    )


def report_to_dict(report: AuditReport) -> dict:
    """将报告转为字典"""
    return {
        "url": report.url,
        "final_url": report.final_url,
        "site_brand": report.site_brand or "",
        "timestamp": report.timestamp,
        "engine_version": ENGINE_VERSION,
        "crawl_stats": getattr(report, "crawl_stats", {}) or {},
        "stories": getattr(report, "stories", []) or [],
        "light_scan_meta": getattr(report, "light_scan_meta", []) or [],
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
                "priority_mark": getattr(r, "priority_mark", ""),
                "priority_reason": getattr(r, "priority_reason", ""),
                "root_cause": getattr(r, "root_cause", ""),
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
