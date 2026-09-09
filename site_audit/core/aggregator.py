"""优先级计算（H1/L3）与根因聚合（L1）——规则全部来自 config yaml，新增规则/根因只加配置。

- compute_priority(result)：按 priority_rules.yaml 计算每条结论的 P1/P2/P3 与规则说明，
  写回 result.priority_mark / result.priority_reason。
- assign_root_cause(result)：按 root_causes.yaml 的 members 把检查项映射到根因标签，
  写回 result.root_cause。
- aggregate_stories(results)：同根因的"不足/不合格"结论攒够 threshold 条，合并为整改故事
  （业务标题/严重度/证据检查项），供报告"根因聚合"区块展示。
"""
import os
import re
from pathlib import Path

import yaml

_CFG_DIR = Path(__file__).resolve().parent.parent / "config"
_STATUS_CN = {"pass": "合格", "warn": "不足", "fail": "不合格", "skip": "未检查"}


def _load_yaml(name):
    with open(_CFG_DIR / name, encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


_cache = {}


def _rules():
    if "rules" not in _cache:
        cfg = _load_yaml("priority_rules.yaml") or {}
        _cache["rules"] = cfg.get("rules", [])
    return _cache["rules"]


def _groups():
    if "groups" not in _cache:
        cfg = _load_yaml("root_causes.yaml") or {}
        _cache["groups"] = cfg.get("groups", [])
    return _cache["groups"]


def _match(rule, r) -> bool:
    m = rule.get("match", {})
    if m.get("item_id") and r.item_id != m["item_id"]:
        return False
    want_status = m.get("any_status")
    if want_status and r.status.value not in want_status:
        return False
    keywords = m.get("any_text_contains") or []
    if keywords:
        hay = f"{r.message or ''} {r.detail or ''}"
        if not any(kw.lower() in hay.lower() for kw in keywords):
            return False
    return True


def annotate(results):
    """遍历结论：计算优先级 + 打根因标签。规则透明，报告可展开 why。"""
    groups = _groups()
    member_map = {}
    for g in groups:
        for mid in g.get("members", []):
            member_map.setdefault(mid, g["id"])

    for r in results:
        # 优先级：规则表命中取最高（P1 > P2 > P3）；未命中按状态兜底
        hit = None
        for rule in _rules():
            if _match(rule, r):
                hit = rule
                break
        if hit:
            r.priority_mark = hit["priority"]
            r.priority_reason = hit.get("why", "")
        elif r.status.value == "fail":
            r.priority_mark, r.priority_reason = "P2", "不合格项：直接影响搜索与AI呈现，需优先修复"
        elif r.status.value == "warn":
            r.priority_mark, r.priority_reason = "P3", "不足项：有优化空间，建议按批次处理"
        else:
            r.priority_mark, r.priority_reason = "", ""

        # 根因标签：按检查项归组映射（检查器显式指定的优先）
        if not r.root_cause:
            r.root_cause = member_map.get(r.item_id, "")
    return results


def aggregate_stories(results):
    """L1 聚合：同根因的 不足/不合格 结论攒够 threshold 条 → 一条整改故事。"""
    stories = []
    for g in _groups():
        members = [r for r in results
                   if r.root_cause == g["id"] and r.status.value in ("warn", "fail")]
        if len(members) >= max(1, int(g.get("threshold", 3))):
            stories.append({
                "root": g["id"],
                "title": g["title"],
                "severity": g.get("severity", "P2"),
                "evidence": [f"{r.item_name}（{_STATUS_CN.get(r.status.value, '')}）" for r in members],
            })
    # 严重度排序：P1 在前
    stories.sort(key=lambda s: s["severity"])
    return stories
