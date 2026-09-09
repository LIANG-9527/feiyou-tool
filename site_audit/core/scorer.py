"""评分引擎：根据检查结果计算维度分和总分"""
from typing import List
from .models import CheckResult, Status, Priority, Category, CategoryScore, AuditReport


# 三档计票分量：致命问题(P0)2票、重要(P1)1票、轻微(P2)半票。
# 维度得分 = Σ(状态分×分量) / Σ分量，状态分 pass=1 / warn=0.5 / fail=0。
# 用既有优先级一物两用，不引入一堆人工小数权重；致命项不合格会明显更压分。
PRIORITY_WEIGHT = {Priority.P0: 2.0, Priority.P1: 1.0, Priority.P2: 0.5}


def _weight_of(result: CheckResult) -> float:
    return PRIORITY_WEIGHT.get(getattr(result, "priority", Priority.P1), 1.0)


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
        # 锦上添花项不参与计分（总分与维度统计均为核心项口径）
        if getattr(r, 'tier', 'core') == 'bonus':
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

        # 计分：pass=1, warn=0.5, fail=0。
        # SKIP 分两类：天然不适用(skip_kind=na)剔出分母、不罚；
        # 该查却没抽到/证据不足(missing)按中性 0.5 计入分母，防止"检查得越少分越高"。
        scored = [i for i in items if i.status != Status.SKIP]
        missing = [i for i in items
                   if i.status == Status.SKIP and getattr(i, "skip_kind", "na") == "missing"]
        # 三档加权：每项按优先级分量计票（P0=2/P1=1/P2=0.5）
        denom_weight = sum(_weight_of(i) for i in scored) + sum(_weight_of(i) for i in missing)
        if denom_weight:
            earned_weight = sum(i.score * _weight_of(i) for i in scored) \
                + 0.5 * sum(_weight_of(i) for i in missing)
            score = (earned_weight / denom_weight) * 100
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
                 results: List[CheckResult], sampled_pages: list = None,
                 prompt_version: str = "",
                 token_usage: dict = None,
                 site_brand: str = "",
                 crawl_stats: dict = None,
                 light_scan_meta: list = None) -> AuditReport:
    """构建完整报告"""
    category_scores = calculate_category_scores(results)
    total_score = calculate_total_score(category_scores)
    grade = grade_from_score(total_score)

    # H1/L3/L1：优先级计算 + 根因标签 + 根因聚合（规则全部来自 config yaml，透明可展开）
    from site_audit.core import aggregator
    aggregator.annotate(results)
    stories = aggregator.aggregate_stories(results)

    return AuditReport(
        url=url,
        final_url=final_url,
        timestamp=timestamp,
        total_score=total_score,
        grade=grade,
        categories=category_scores,
        results=results,
        sampled_pages=sampled_pages or [],
        prompt_version=prompt_version,
        token_usage=token_usage or {},
        site_brand=site_brand,
        crawl_stats=crawl_stats or {},
        stories=stories,
        light_scan_meta=light_scan_meta or [],
    )
