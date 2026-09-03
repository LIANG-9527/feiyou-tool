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

    @property
    def summary(self) -> dict:
        # CMS诊断项不计入统计
        scored = [r for r in self.results if r.item_id not in ("tech-14", "tech-15")]
        passed = sum(1 for r in scored if r.status == Status.PASS)
        warned = sum(1 for r in scored if r.status == Status.WARN)
        failed = sum(1 for r in scored if r.status == Status.FAIL)
        skipped = sum(1 for r in scored if r.status == Status.SKIP)
        return {
            "total": len(scored),
            "pass": passed,
            "warn": warned,
            "fail": failed,
            "skip": skipped,
        }
