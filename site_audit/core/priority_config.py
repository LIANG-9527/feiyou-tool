"""检查项优先级自定义配置。

各项的默认优先级（P0/P1/P2）写死在 site_audit/audit/ 各检查模块的 _item(...) 调用里。
本模块负责：
1. 从检查模块源码提取检查项目录（编号、业务名、默认优先级），无需跑一次诊断即可枚举全部检查项；
2. 读写项目根目录 priority_overrides.json 里的用户覆盖（编号 -> P0/P1/P2）；
3. 供 BaseChecker._item 在构造检查项时应用覆盖——计分权重（scorer）与报告排序随之改变。
"""
import json
import re
from pathlib import Path

# site_audit/core/priority_config.py -> 项目根目录
_PROJECT_ROOT = Path(__file__).resolve().parents[2]
_AUDIT_DIR = Path(__file__).resolve().parents[1] / "audit"
OVERRIDES_PATH = _PROJECT_ROOT / "priority_overrides.json"

_VALID_PRIORITY = ("P0", "P1", "P2")

# 与各检查模块源码里 _item(...) 的书写方式保持一致：_item("编号", "业务名", Priority.Px, ...
_ITEM_PATTERN = re.compile(
    r'_item\(\s*"([a-z0-9-]+)",\s*"([^"]+)",\s*Priority\.(P[012])', re.S)
# 每项的"作用"文案：_item(...) 调用内的 purpose="..." 关键字参数
_PURPOSE_PATTERN = re.compile(r'purpose="([^"]*)"')

_CATEGORY_ORDER = ["技术可抓取性", "AI友好度专项", "内容质量与结构化",
                   "权威与信任信号", "商业转化与决策内容"]


def _category_of(item_id: str) -> str:
    """维度归属：与各检查模块运行时给 item.category 的赋值保持一致。"""
    if item_id.startswith("tech-"):
        return "技术可抓取性"
    if item_id.startswith("ai-"):
        return "AI友好度专项"
    if item_id.startswith("content-"):
        return "内容质量与结构化"
    if item_id.startswith("auth-"):
        return "权威与信任信号"
    if item_id.startswith("b2b-"):
        return "商业转化与决策内容"
    if item_id.startswith("mp-"):
        # multi_page.py 里 mp-01~03 保持类默认 AI友好度专项，mp-04~07 改写为内容质量
        return "AI友好度专项" if item_id in ("mp-01", "mp-02", "mp-03") else "内容质量与结构化"
    return "其他"


def _sort_key(item_id: str):
    cat = _category_of(item_id)
    cat_idx = _CATEGORY_ORDER.index(cat) if cat in _CATEGORY_ORDER else 99
    m = re.match(r"[a-z]+-(\d+)", item_id)
    return (cat_idx, int(m.group(1)) if m else 999)


def _audience_of(item_id: str) -> str:
    """领域针对性：按报告 SEO/GEO 检查集归属划分（辅助诊断为 neither）。"""
    from site_audit.report.html_report import GEO_ITEM_IDS, SEO_ITEM_IDS
    in_seo, in_geo = item_id in SEO_ITEM_IDS, item_id in GEO_ITEM_IDS
    if in_seo and in_geo:
        return "SEO+GEO"
    return "SEO" if in_seo else ("GEO" if in_geo else "辅助诊断")


def item_catalog() -> list:
    """全部检查项目录：[{id, name, category, default_priority, purpose, audience}]，按维度+序号排序。"""
    seen = {}
    for f in sorted(_AUDIT_DIR.glob("*.py")):
        try:
            text = f.read_text(encoding="utf-8")
        except OSError:
            continue
        item_matches = list(_ITEM_PATTERN.finditer(text))
        purpose_matches = list(_PURPOSE_PATTERN.finditer(text))
        for i, m in enumerate(item_matches):
            iid = m.group(1)
            if iid in seen:
                continue
            # "作用"取 _item 调用块内、下一项开始前的第一个 purpose="..."
            scope_end = item_matches[i + 1].start() if i + 1 < len(item_matches) else len(text)
            purpose = ""
            for pm in purpose_matches:
                if m.end() < pm.start() < scope_end:
                    purpose = pm.group(1)
                    break
            seen[iid] = {
                "id": iid,
                "name": m.group(2),
                "category": _category_of(iid),
                "default_priority": m.group(3),
                "purpose": purpose,
                "audience": _audience_of(iid),
            }
    return sorted(seen.values(), key=lambda it: _sort_key(it["id"]))


def load_overrides() -> dict:
    """读取用户覆盖：{检查项编号: P0/P1/P2}；文件缺失/损坏/格式非法时返回空。"""
    if not OVERRIDES_PATH.exists():
        return {}
    try:
        data = json.loads(OVERRIDES_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    return {k: v for k, v in data.items()
            if isinstance(k, str) and re.fullmatch(r"[a-z0-9-]+", k)
            and v in _VALID_PRIORITY}


def save_overrides(overrides: dict) -> int:
    """写入用户覆盖（非法键值直接丢弃），返回保存条数。"""
    clean = {k: v for k, v in (overrides or {}).items()
            if isinstance(k, str) and re.fullmatch(r"[a-z0-9-]+", k)
            and v in _VALID_PRIORITY}
    OVERRIDES_PATH.write_text(
        json.dumps(clean, ensure_ascii=False, indent=2, sort_keys=True),
        encoding="utf-8")
    return len(clean)


def effective_priority(item_id: str, default) -> str:
    """查某项的生效优先级：覆盖优先，否则返回默认值（P0/P1/P2 字符串或 Priority 枚举均可透传）。"""
    override = load_overrides().get(item_id)
    if override:
        return override
    return getattr(default, "value", default)
