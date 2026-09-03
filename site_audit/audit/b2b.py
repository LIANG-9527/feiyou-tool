"""B 端 GEO 增强检查（6项，其中2项待实现）"""
import re
from typing import List
from .checker import BaseChecker
from site_audit.core.models import CheckResult, Status, Priority, Category


class B2BChecker(BaseChecker):
    category = Category.B2B

    def run(self) -> List[CheckResult]:
        results = []
        results.append(self._check_product_pages())
        results.append(self._check_case_studies())
        results.append(self._check_solution_pages())
        results.append(self._check_comparison_content())
        results.append(self._check_tech_docs())
        results.append(self._check_qa_content())
        results.append(self._check_conversion_path())
        return results

    # 37. 产品/解决方案页面
    def _check_product_pages(self) -> CheckResult:
        item = self._item(
            "b2b-01", "产品/解决方案页面结构化", Priority.P0,
            "是否有清晰的产品矩阵和独立详情页",
            "检查导航中是否有产品/解决方案链接",
            "有产品列表页+独立详情页，含功能/参数/场景",
            manual_check="看网站导航是否有'产品''解决方案'入口，点进去是否有每个产品的独立详情页，包含功能、参数、应用场景",
            purpose="B端客户搜索具体产品或解决方案时，AI 需要找到对应的独立页面来回答。只有首页没有产品详情页，AI 无法精准匹配用户需求",
            excellent="有清晰的产品矩阵页，每个产品有独立详情页（含功能/参数/场景/价格或询价/FAQ），URL 结构规范",
            normal="有产品/解决方案入口，但详情页内容较简单（缺少参数或场景），或部分产品没有独立页",
            failed="没有产品/解决方案页面入口，或所有产品信息都挤在首页没有独立详情页",
        )
        from urllib.parse import urlparse
        base_domain = urlparse(self.page.final_url).netloc

        product_keywords = ["产品", "解决方案", "product", "solution", "功能", "features"]
        product_links = []
        for a in self.page.soup.find_all("a", href=True):
            text = a.text.strip().lower()
            href = a["href"].lower()
            if any(kw in text for kw in product_keywords) or \
               any(kw in href for kw in ["product", "solution", "features"]):
                if not href.startswith(("#", "javascript:", "mailto:")):
                    product_links.append(a.text.strip())

        if product_links:
            unique = list(set(product_links))[:5]
            return self._result(item, Status.PASS,
                                f"检测到 {len(set(product_links))} 个产品/方案入口",
                                detail=f"包含: {', '.join(unique)}")
        return self._result(item, Status.WARN, "未检测到明显的产品/解决方案页面入口")

    # 38. 案例研究
    def _check_case_studies(self) -> CheckResult:
        item = self._item(
            "b2b-02", "案例研究可抓取性", Priority.P0,
            "是否有客户案例页面且内容完整",
            "检查导航中是否有案例/客户链接",
            "案例为独立页面，含背景/挑战/方案/成果",
            manual_check="看导航是否有'客户案例''成功案例'入口，点进去看案例是否为独立页面，是否包含客户背景、挑战、方案、成果数据",
            purpose="B端客户做决策时高度依赖案例。AI 回答'XX公司用了什么方案''XX行业有什么成功案例'时，会优先引用有完整数据的案例页",
            excellent="有 ≥5 个详细客户案例（独立页面），每个案例含客户背景/挑战/方案/成果数据（量化），有客户 Logo 和证言",
            normal="有案例入口和案例列表，但案例详情较简单（缺少成果数据或挑战分析），或案例数量 <5",
            failed="没有客户案例页面，或只有客户 Logo 没有案例详情",
        )
        case_keywords = ["案例", "客户", "成功故事", "case", "customer", "story", "客户案例"]
        case_links = []
        for a in self.page.soup.find_all("a", href=True):
            text = a.text.strip()
            if any(kw in text.lower() for kw in case_keywords):
                if not a["href"].startswith(("#", "javascript:", "mailto:")):
                    case_links.append(text)

        customer_logos = self.page.soup.find_all(class_=re.compile(r"customer|client|logo|partner", re.I))

        if case_links:
            return self._result(item, Status.PASS,
                                f"有客户案例入口（{len(set(case_links))}个）",
                                detail=f"包含: {', '.join(list(set(case_links))[:3])}")
        if customer_logos:
            return self._result(item, Status.WARN, "有客户 Logo 但缺少案例详情页入口")
        return self._result(item, Status.FAIL, "未检测到客户案例或客户展示")

    # 39. 行业解决方案覆盖
    def _check_solution_pages(self) -> CheckResult:
        item = self._item(
            "b2b-03", "行业解决方案覆盖度", Priority.P1,
            "是否按行业/场景拆分解决方案",
            "检查是否有行业/场景相关链接",
            "覆盖≥3个目标行业，每页针对行业痛点",
            manual_check="看导航是否有'行业解决方案''应用场景'入口，是否按零售、金融、教育、医疗等行业拆分了独立页面",
            purpose="B端客户常搜索'XX行业解决方案''XX场景用什么工具'。按行业拆分的页面能精准匹配这些搜索意图，被 AI 引用的概率大幅提升",
            excellent="覆盖 ≥5 个目标行业，每个行业有独立页面，针对行业痛点定制内容（含场景/方案/案例/数据）",
            normal="覆盖 2-3 个行业，有行业页面但内容较通用，未针对行业痛点深度定制",
            failed="没有按行业/场景拆分，所有内容都在通用页面，或行业覆盖 <2 个",
        )
        industry_keywords = ["行业", "场景", "零售", "金融", "教育", "医疗", "制造",
                            "电商", "政务", "企业", "industry", "retail", "finance"]
        industry_links = []
        for a in self.page.soup.find_all("a", href=True):
            text = a.text.strip()
            if any(kw in text for kw in industry_keywords):
                if not a["href"].startswith(("#", "javascript:", "mailto:")):
                    industry_links.append(text)

        if len(set(industry_links)) >= 3:
            return self._result(item, Status.PASS,
                                f"覆盖 {len(set(industry_links))} 个行业/场景")
        if industry_links:
            return self._result(item, Status.WARN,
                                f"行业覆盖较少（{len(set(industry_links))}个），建议扩充")
        return self._result(item, Status.WARN, "未检测到明显的行业解决方案分类")

    # 40. 对比类内容
    def _check_comparison_content(self) -> CheckResult:
        item = self._item(
            "b2b-04", "对比类内容存在性", Priority.P1,
            "是否有竞品对比或选型指南内容",
            "检查是否有「对比」「哪个好」「选型」等链接或内容",
            "至少有2篇对比文章，客观中立含数据",
            manual_check="看网站是否有'XX对比XX''XX哪家好''选型指南'类文章，内容是否客观中立、有数据支撑",
            purpose="B端客户选型阶段会大量搜索'XX和XX哪个好''XX选型指南'。有这类内容的网站能截胡竞品流量，被 AI 选为对比答案的来源",
            excellent="有 ≥3 篇高质量对比/选型文章，客观中立（不贬低竞品），有数据/表格/场景分析，有独立页面且 URL 规范",
            normal="有 1-2 篇对比内容，但可能不够客观（过度自夸）或缺少数据支撑",
            failed="没有任何对比/选型类内容",
        )
        comparison_keywords = ["对比", "哪个好", "选型", "区别", "vs", "比较", "选购"]
        found = []

        for a in self.page.soup.find_all("a", href=True):
            text = a.text.strip()
            if any(kw in text.lower() for kw in comparison_keywords):
                found.append(text)

        page_text = self.page.get_text()
        text_matches = sum(1 for kw in comparison_keywords if kw in page_text)

        if found:
            return self._result(item, Status.PASS,
                                f"有对比类内容入口: {', '.join(list(set(found))[:3])}")
        if text_matches >= 2:
            return self._result(item, Status.WARN, "页面提及对比类关键词但无专门入口")
        return self._result(item, Status.WARN, "未检测到对比/选型类内容")

    # 39. 技术文档/API文档AI友好
    def _check_tech_docs(self) -> CheckResult:
        item = self._item(
            "b2b-05", "技术文档/API文档AI友好", Priority.P1,
            "技术文档是否有清晰目录、结构化、可被AI抓取",
            "检查文档站结构、代码块标记、搜索功能",
            "文档有侧边栏目录，代码块有语言标记，可被AI爬虫访问",
            manual_check="访问文档/API页面，看是否有侧边栏目录、代码块是否有语言高亮、是否有站内搜索、文档是否为静态HTML（非JS渲染）",
            purpose="B端客户和AI都高度依赖技术文档。文档结构清晰、可被AI抓取，能让AI在回答技术问题时引用你的文档，带来精准的技术流量",
            excellent="文档站有完整侧边栏目录+面包屑，代码块有语言标记，有OpenAPI/Swagger规范，静态HTML可抓取，有搜索功能，llms.txt包含文档页",
            normal="有基本文档结构和目录，但代码块缺少语言标记，或部分内容依赖JS渲染，或搜索功能不完善",
            failed="无技术文档，或文档为纯图片/JS渲染无法被AI抓取，或结构混乱无目录",
        )
        if not self.ai_engine:
            return self._result(item, Status.SKIP, "未检查（需配置AI语义引擎）",
                                detail="未配置LLM，无法进行技术文档分析")
        status, summary, detail = self.ai_engine.check_tech_docs(self.page, self.all_pages)
        return self._result(item, status, summary, detail=detail)

    # 42. 问答型内容覆盖
    def _check_qa_content(self) -> CheckResult:
        item = self._item(
            "b2b-06", "问答型内容覆盖", Priority.P1,
            "是否覆盖客户常问的问题（价格、部署、安全、合规等）",
            "检查是否有FAQ/博客/帮助中心覆盖高频问题",
            "有独立FAQ或博客覆盖≥10个高频问题，每个问题有完整答案",
            manual_check="浏览网站FAQ、博客、帮助中心，统计覆盖了多少个客户高频问题（价格、部署方式、数据安全、合规认证、售后支持等），每个问题是否有完整答案",
            purpose="B端客户决策前会大量搜索'XX多少钱''XX怎么部署''XX安全吗'等问题。覆盖这些问答型内容能让AI直接引用你的答案，截胡高意向流量",
            excellent="覆盖≥15个高频问题（价格/部署/安全/合规/售后等），每个问题有独立页面或FAQ结构化标记，答案完整含数据，有FAQPage Schema",
            normal="覆盖5-10个高频问题，有FAQ板块但可能缺少结构化标记，或部分问题答案较简单",
            failed="无FAQ或问答型内容，或覆盖问题<5个，答案不完整",
        )
        if not self.ai_engine:
            return self._result(item, Status.SKIP, "未检查（需配置AI语义引擎）",
                                detail="未配置LLM，无法进行问答覆盖分析")
        status, summary, detail = self.ai_engine.check_qa_content(self.page, self.all_pages)
        return self._result(item, status, summary, detail=detail)

    # B2B-07 转化引导与决策路径
    def _check_conversion_path(self) -> CheckResult:
        item = self._item(
            "b2b-07", "转化引导与决策路径", Priority.P2,
            "页面是否提供匹配B端决策阶段的下一步引导（资料/演示/咨询/试用）及清晰承接方式",
            "AI：识别正文CTA位置与话术、是否分层匹配决策阶段、联系方式是否多元",
            "CTA匹配内容决策阶段，提供分层转化路径，入口清晰不打扰",
            manual_check="通读页面看是否有明确的下一步入口；判断CTA是否匹配当前内容阶段（科普页给资料、产品页给演示）；是否提供多种低门槛联系方式",
            purpose="B端决策链长，需要在合适位置提供匹配决策阶段的引导。内容好但无下一步入口、或一上来就硬推销，都会让用户和AI找不到从了解到联系的路径",
            excellent="CTA与内容决策阶段匹配，提供分层转化路径（轻量资料→深度演示→直接咨询），入口清晰、话术自然不打扰，AI能识别服务承接方式",
            normal="有CTA但形式单一，或位置/话术与决策阶段不完全匹配，转化路径不够顺滑",
            failed="无任何转化引导，或全程硬推销引发反感，用户找不到下一步联系路径",
        )
        if not self.ai_engine:
            return self._result(item, Status.SKIP, "未检查（需配置AI语义引擎）",
                                detail="未配置LLM，无法进行转化路径分析")
        status, summary, detail = self.ai_engine.check_conversion_path(self.page)
        return self._result(item, status, summary, detail=detail)
