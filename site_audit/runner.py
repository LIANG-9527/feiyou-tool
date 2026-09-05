"""GEO 官网诊断封装：供 FastAPI 调用，静默运行并返回报告摘要与文件路径"""
from __future__ import annotations
import io
import re
import json
import contextlib
import logging
from datetime import datetime
from urllib.parse import urlparse
from pathlib import Path

# 公司全称里需要剥掉的地域前缀与主体后缀，用于从工商全称得到对外品牌简称
_BRAND_CITY = r"(?:深圳|北京|上海|广州|杭州|成都|武汉|南京|苏州|西安|重庆|天津|青岛|长沙|郑州|东莞|佛山|福州|合肥|济南|昆明|南宁|石家庄|太原|贵阳|兰州|海口|浙江|广东|江苏|山东|四川|福建|湖南|湖北|河南|河北|陕西|安徽|江西|辽宁|云南|贵州|甘肃|海南|厦门市|宁波市|青岛市)"
_BRAND_SUFFIX = r"(?:有限责任公司|股份有限公司|集团有限公司|科技有限公司|技术有限公司|有限公司|实业集团|集团)"
_BRAND_SLOGAN = re.compile(r"领先|专业|专注|一站式|解决方案|服务公司|哪家好|怎么样|官网首页|官方网站")
# 这些是通用页面名，不是品牌，遇到要跳过继续找
_BRAND_STOP = {"首页", "主页", "网站首页", "官网首页", "home", "index", "website", "welcome", "默认首页"}


def _clean_brand(name: str) -> str:
    name = re.sub(r"[【\[\(（][^】\]\)）]*[】\]\)）]\s*$", "", name or "").strip()
    name = re.sub(rf"^{_BRAND_CITY}+", "", name).strip()
    name = re.sub(rf"{_BRAND_SUFFIX}$", "", name).strip()
    return name


def extract_site_brand(page) -> str:
    """从首页直接识别对外品牌名：og:site_name > 标题里的品牌段 > 结构化名片 name；识别不出返回空。"""
    soup = getattr(page, "soup", None)
    if soup is None:
        return ""
    og = soup.find("meta", attrs={"property": "og:site_name"})
    if og and og.get("content", "").strip():
        c = _clean_brand(og["content"])
        if c:
            return c
    title = soup.title.get_text(strip=True) if soup.title else ""
    if title:
        segs = [s.strip() for s in re.split(r"[-_｜|–—~]+", title) if s.strip()]
        for s in segs:  # 带「官网」标记的段基本就是品牌
            if "官网" in s:
                c = _clean_brand(re.sub(r"[【\[\(（]?官网[】\]\)）]?", "", s))
                if c:
                    return c
        for s in reversed(segs):  # 否则取尾段，跳过口号式描述与通用页面名
            c = _clean_brand(s)
            if (c and 1 < len(c) <= 14 and c.lower() not in _BRAND_STOP
                    and not _BRAND_SLOGAN.search(c)):
                return c
    for sc in soup.find_all("script", attrs={"type": "application/ld+json"}):
        try:
            data = json.loads(sc.string or "")
        except Exception:
            continue
        stack = [data]
        while stack:
            n = stack.pop()
            if isinstance(n, dict):
                nm = n.get("name")
                if isinstance(nm, str) and nm.strip():
                    c = _clean_brand(nm)
                    if c:
                        return c
                stack.extend(v for v in n.values() if isinstance(v, (dict, list)))
            elif isinstance(n, list):
                stack.extend(n)
    return ""

from site_audit.core.fetcher import Fetcher
from site_audit.core.crawler import SiteCrawler
from site_audit.core.scorer import build_report
from site_audit.core.ai_engine import AIEngine, PROMPT_VERSION, estimate_cost_cny
from site_audit.core.snapshot import save_snapshot
from site_audit.audit.technical import TechnicalChecker
from site_audit.audit.ai_friendly import AIFriendlyChecker
from site_audit.audit.content import ContentChecker
from site_audit.audit.authority import AuthorityChecker
from site_audit.audit.b2b import B2BChecker
from site_audit.audit.multi_page import MultiPageChecker
from site_audit.audit.cross_page import CrossPageChecker
from site_audit.report.json_report import save_json_report
from site_audit.report.html_report import save_html_report

logger = logging.getLogger("site_audit.runner")

# 单页面检查器（基于首页内容）
PAGE_CHECKERS = [
    TechnicalChecker,
    AIFriendlyChecker,
    ContentChecker,
    AuthorityChecker,
    B2BChecker,
    MultiPageChecker,  # 内页多页聚合扫描（2026-09-05 整改）
]


def _create_ai_engine(llm_config: dict = None) -> AIEngine | None:
    """从配置创建AI语义引擎，无配置则返回None。温度强制为0确保结果确定性。"""
    if not llm_config:
        return None
    try:
        return AIEngine(
            base_url=llm_config["base_url"],
            api_key=llm_config["api_key"],
            model=llm_config.get("model", "deepseek-chat"),
            temperature=0,
            timeout=llm_config.get("timeout", 60),
            max_retries=llm_config.get("max_retries", 2),
        )
    except Exception as e:
        logger.warning(f"AI引擎初始化失败: {e}")
        return None


def run_site_audit(url: str, output_dir, max_pages: int = 12,
                   llm_config: dict = None) -> dict:
    """运行一次完整诊断（首页 + 多页面抽样 + 跨页面检查），生成 HTML+JSON 报告。"""
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    # 初始化AI语义引擎
    ai_engine = _create_ai_engine(llm_config)

    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        fetcher = Fetcher(timeout=15)
        page = fetcher.fetch(url)
        if page is None:
            return {"ok": False, "error": f"无法访问 {url}，请检查网址是否正确或网络是否正常"}

        # 多页面抽样爬取
        crawler = SiteCrawler(fetcher, max_pages=max_pages, max_workers=4)
        try:
            extra_pages = crawler.crawl(page)
        except Exception:
            extra_pages = []
        # 必查类型覆盖情况（关于/联系/产品服务/案例/资讯/FAQ/隐私 各发现/选中多少）
        page_coverage = getattr(crawler, "last_coverage", {})

        # 单页面检查（首页），传入 extra_pages 和 ai_engine
        all_results = []
        for checker_cls in PAGE_CHECKERS:
            checker = checker_cls(fetcher, page, extra_pages, ai_engine=ai_engine)
            all_results.extend(checker.run())

        # 跨页面检查（断链 / 重复元信息 / 关键词蚕食）
        cross = CrossPageChecker(fetcher, page, extra_pages, ai_engine=ai_engine)
        all_results.extend(cross.run())

        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        sampled = [
            {
                "url": p.final_url,
                "title": (p.soup.title.get_text(strip=True) if p.soup.title else "")[:60],
                "status": p.status_code,
                "size_kb": round(p.content_size / 1024, 0),
            }
            for p in extra_pages
        ]
        site_brand = extract_site_brand(page)
        report = build_report(url, page.final_url, timestamp, all_results,
                              sampled_pages=sampled,
                              prompt_version=PROMPT_VERSION if ai_engine else "",
                              token_usage=ai_engine.token_usage if ai_engine else None,
                              site_brand=site_brand)
        # 按 DeepSeek 官方价格折算本次诊断费用（元）
        if report.token_usage:
            report.token_usage["cost_cny"] = estimate_cost_cny(report.token_usage)

        domain = urlparse(report.final_url).netloc.replace(":", "_")
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        html_name = f"{domain}_{ts}.html"
        json_name = f"{domain}_{ts}.json"
        save_html_report(report, str(output_dir / html_name))
        save_json_report(report, str(output_dir / json_name))

        # 快照落盘：留存页面抽取内容+LLM原始响应，供复检重放与前后对比取证
        snapshot_name = f"{domain}_{ts}"
        snapshot_dir = save_snapshot(output_dir, snapshot_name,
                                     pages=[page, *extra_pages],
                                     ai_engine=ai_engine)

    return {
        "ok": True,
        "url": url,
        "final_url": report.final_url,
        "domain": domain,
        "score": report.total_score,
        "grade": report.grade,
        "summary": report.summary,
        "sampled_count": len(sampled),
        "page_coverage": page_coverage,
        "categories": [
            {
                "category": c.category.value,
                "score": c.score,
                "total": c.total,
                "pass": c.passed,
                "warn": c.warned,
                "fail": c.failed,
                "skip": c.skipped,
                "weight": c.weight,
            }
            for c in report.categories
        ],
        "html_file": html_name,
        "json_file": json_name,
        "snapshot_dir": f"snapshots/{snapshot_name}" if snapshot_dir else "",
        "response_time": round(page.response_time, 2),
        "page_kb": round(page.content_size / 1024, 0),
        "ai_enabled": ai_engine is not None,
        "token_usage": report.token_usage,
    }
