"""评分引擎：根据检查结果计算维度分和总分"""
from typing import List
from .models import CheckResult, Status, Category, CategoryScore, AuditReport


# 各维度权重（合计 1.0）
CATEGORY_WEIGHTS = {
    Category.TECHNICAL: 0.30,
    Category.AI_FRIENDLY: 0.30,
    Category.CONTENT: 0.20,
    Category.AUTHORITY: 0.15,
    Category.B2B: 0.05,
}


def grade_from_score(score: float) -> str:
    """根据分数返回等级"""
    if score >= 90:
        return "A"
    elif score >= 75:
        return "B"
    elif score >= 60:
        return "C"
    elif score >= 40:
        return "D"
    else:
        return "F"


# CMS诊断项不参与打分，单独展示
CMS_ITEM_IDS = {"tech-14", "tech-15"}


def calculate_category_scores(results: List[CheckResult]) -> List[CategoryScore]:
    """计算各维度得分（排除CMS诊断项）"""
    category_map = {}
    for r in results:
        if r.item_id in CMS_ITEM_IDS:
            continue
        if r.category not in category_map:
            category_map[r.category] = []
        category_map[r.category].append(r)

    scores = []
    for cat, items in category_map.items():
        total = len(items)
        passed = sum(1 for i in items if i.status == Status.PASS)
        warned = sum(1 for i in items if i.status == Status.WARN)
        failed = sum(1 for i in items if i.status == Status.FAIL)
        skipped = sum(1 for i in items if i.status == Status.SKIP)

        # 计分：pass=1, warn=0.5, fail=0, skip 不计入分母
        scorable = [i for i in items if i.status != Status.SKIP]
        if scorable:
            earned = sum(i.score for i in scorable)
            score = (earned / len(scorable)) * 100
        else:
            score = 0.0

        scores.append(CategoryScore(
            category=cat,
            total=total,
            passed=passed,
            warned=warned,
            failed=failed,
            skipped=skipped,
            score=round(score, 1),
            weight=CATEGORY_WEIGHTS.get(cat, 0.05),
        ))

    return scores


def calculate_total_score(category_scores: List[CategoryScore]) -> float:
    """计算加权总分"""
    total = 0.0
    total_weight = 0.0
    for cs in category_scores:
        total += cs.score * cs.weight
        total_weight += cs.weight
    if total_weight == 0:
        return 0.0
    return round(total / total_weight, 1)


def build_report(url: str, final_url: str, timestamp: str,
                 results: List[CheckResult], sampled_pages: list = None) -> AuditReport:
    """构建完整报告"""
    category_scores = calculate_category_scores(results)
    total_score = calculate_total_score(category_scores)
    grade = grade_from_score(total_score)

    return AuditReport(
        url=url,
        final_url=final_url,
        timestamp=timestamp,
        total_score=total_score,
        grade=grade,
        categories=category_scores,
        results=results,
        sampled_pages=sampled_pages or [],
    )
