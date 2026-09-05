"""数据模型：检查项、检查结果、诊断报告"""
from dataclasses import dataclass, field
from enum import Enum
from typing import List, Optional


class Status(str, Enum):
    PASS = "pass"      # 通过
    WARN = "warn"      # 警告
    FAIL = "fail"      # 失败
    SKIP = "skip"      # 跳过（无法检测）


class Priority(str, Enum):
    P0 = "P0"
    P1 = "P1"
    P2 = "P2"


class Category(str, Enum):
    TECHNICAL = "技术可抓取性"
    AI_FRIENDLY = "AI友好度专项"
    CONTENT = "内容质量与结构化"
    AUTHORITY = "权威与信任信号"
    B2B = "商业转化与决策内容"


@dataclass
class CheckItem:
    """检查项定义"""
    id: str
    name: str
    category: Category
    priority: Priority
    description: str
    check_method: str
    standard: str
    # 新增：手动检查方法、作用、三级标准
    manual_check: str = ""
    purpose: str = ""
    impact: str = ""
    scope: str = "首页"
    applicable_pages: str = "首页"
    suggestion: str = ""
    excellent: str = ""
    normal: str = ""
    failed: str = ""
    tier: str = "core"  # core=参与计分, bonus=仅建议不扣分


@dataclass
class CheckResult:
    """单项检查结果"""
    item_id: str
    item_name: str
    category: Category
    priority: Priority
    status: Status
    message: str
    detail: Optional[str] = None
    score: float = 0.0  # 该项得分 0-1
    # 检查项详细说明
    manual_check: str = ""
    purpose: str = ""
    impact: str = ""
    scope: str = "首页"
    applicable_pages: str = "首页"
    suggestion: str = ""
    excellent: str = ""
    normal: str = ""
    failed: str = ""
    tier: str = "core"  # core=参与计分, bonus=仅建议不扣分
    # 供"检查项计分配置详情"展示：检查什么、怎么查、合格标准
    description: str = ""
    check_method: str = ""
    standard: str = ""
    # 仅当 status=SKIP 时有意义：na=天然不适用(剔出分母,不罚)；missing=该查却没抽到/证据不足(按中性0.5计入,防止不检查反抬分)
    skip_kind: str = "na"
    # 该结论具体涉及哪些页面（URL 或路径），用于报告"来源"精确到页面；为空时报告再从文案抽取/回退
    pages: list = None


@dataclass
class CategoryScore:
    """维度得分"""
    category: Category
    total: int
    passed: int
    warned: int
    failed: int
    skipped: int
    score: float  # 0-100
    weight: float


@dataclass
class AuditReport:
    """完整诊断报告"""
    url: str
    final_url: str
    timestamp: str
    total_score: float
    grade: str
    categories: List[CategoryScore] = field(default_factory=list)
    results: List[CheckResult] = field(default_factory=list)
    # 多页面抽样信息：[{url, title, status, size_kb, depth}]
    sampled_pages: List[dict] = field(default_factory=list)
    # AI 语义检查的 prompt 版本（未启用 AI 引擎时为空串）
    prompt_version: str = ""
    # 本次诊断 LLM token 用量（ai_engine 累计）：{prompt_tokens, completion_tokens, total_tokens, calls}
    token_usage: dict = field(default_factory=dict)
    # 从首页直接提取的对外品牌名（标题尾段/og:site_name/结构化名片），避免依赖会跳过的检查项
    site_brand: str = ""

    @property
    def summary(self) -> dict:
        # CMS诊断项不计入统计
        scored = [r for r in self.results if r.item_id not in ("tech-14", "tech-15")]
        # 核心项口径：锦上添花项（bonus）不参与计分统计
        core = [r for r in scored if getattr(r, "tier", "core") != "bonus"]
        passed = sum(1 for r in scored if r.status == Status.PASS)
        warned = sum(1 for r in scored if r.status == Status.WARN)
        failed = sum(1 for r in scored if r.status == Status.FAIL)
        skipped = sum(1 for r in scored if r.status == Status.SKIP)
        core_passed = sum(1 for r in core if r.status == Status.PASS)
        core_warned = sum(1 for r in core if r.status == Status.WARN)
        core_failed = sum(1 for r in core if r.status == Status.FAIL)
        return {
            "total": len(scored),
            "pass": passed,
            "warn": warned,
            "fail": failed,
            "skip": skipped,
            "core_total": len(core),
            "core_pass": core_passed,
            "core_warn": core_warned,
            "core_fail": core_failed,
            "bonus_total": len(scored) - len(core),
        }
