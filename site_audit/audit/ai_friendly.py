"""AI 友好度专项检查（8项）"""
import re
import json
from typing import List
from .checker import BaseChecker
from site_audit.core.models import CheckResult, Status, Priority, Category


class AIFriendlyChecker(BaseChecker):
    category = Category.AI_FRIENDLY

    def run(self) -> List[CheckResult]:
        results = []
        results.append(self._check_title())
        results.append(self._check_description())
        results.append(self._check_heading_structure())
        results.append(self._check_structured_data())
        results.append(self._check_answer_format())
        results.append(self._check_breadcrumb())
        results.append(self._check_multi_page())
        results.append(self._check_ai_indexing())
        results.append(self._check_nlp_friendly())
        results.append(self._check_content_chunking())
        return results

    # 12. Meta Title
    def _check_title(self) -> CheckResult:
        item = self._item(
            "ai-01", "Meta Title", Priority.P0,
            "页面标题是否存在、长度是否合理、是否含核心关键词",
            "检查 <title> 标签内容、长度和关键词",
            "唯一，含品牌+关键词，30-60字符",
            manual_check="右键查看网页源代码，找到 <title> 标签，看内容是否包含品牌名和核心业务关键词，长度是否在30-60字符",
            purpose="Title 是 AI 理解页面主题的第一信号，也是搜索结果里显示的标题。写得好能直接提升 AI 引用概率和点击率",
            excellent="每页唯一标题，含核心关键词+品牌名+差异化卖点，30-60字符，核心词前置",
            normal="有标题且包含品牌或关键词，长度基本合理，但可能缺少卖点或关键词布局不够精准",
            failed="缺少 title，或标题只有品牌名无业务关键词，或标题过短/过长",
        )
        title_tag = self.local_soup.find("title")
        if not title_tag or not title_tag.text.strip():
            return self._result(item, Status.FAIL, "缺少 <title> 标签")

        title = title_tag.text.strip()
        length = len(title)
        if length < 15:
            return self._result(item, Status.WARN, f"标题过短（{length}字符），建议加入核心关键词",
                                detail=f"标题: {title}")
        if length > 80:
            return self._result(item, Status.WARN, f"标题过长（{length}字符），搜索结果会被截断",
                                detail=f"标题: {title[:60]}...")
        return self._result(item, Status.PASS, f"标题合理（{length}字符）",
                            detail=f"标题: {title}")

    # 13. Meta Description
    def _check_description(self) -> CheckResult:
        item = self._item(
            "ai-02", "Meta Description-描述标签质量", Priority.P0,
            "页面描述是否存在、长度是否合理、是否有行动号召",
            "检查 meta name=description 的内容、长度和CTA",
            "唯一，自然语言概括，80-160字符，含行动号召",
            manual_check="右键查看源代码，搜索 name=\"description\"，看 content 里是否用自然语言概括了页面价值，是否有'了解更多''免费咨询'等行动号召",
            purpose="Description 是 AI 生成摘要和搜索结果描述的重要来源。好的描述能让 AI 准确理解页面内容，提升被摘为答案的概率，同时影响搜索结果点击率",
            excellent="每页唯一描述，80-160字符，含核心关键词+独特卖点+行动号召，自然不堆砌，与页面内容强相关",
            normal="有描述且长度合理，但缺少行动号召或卖点不够突出",
            failed="缺少 description，或描述过短（<50字符）无信息量",
        )
        desc = self.page.meta("description")
        if not desc:
            return self._result(item, Status.FAIL, "缺少 meta description")

        length = len(desc)
        has_cta = any(kw in desc for kw in ["了解", "咨询", "联系", "免费", "立即",
                                             "体验", "试用", "learn", "contact", "free"])
        if length < 50:
            return self._result(item, Status.WARN, f"描述偏短（{length}字符），建议补充",
                                detail=f"描述: {desc}")
        if length > 200:
            return self._result(item, Status.WARN, f"描述过长（{length}字符）",
                                detail=f"描述: {desc[:80]}...")
        if has_cta:
            return self._result(item, Status.PASS, f"描述含行动号召，长度合理（{length}字符）",
                                detail=f"描述: {desc}")
        return self._result(item, Status.WARN, f"描述长度合理（{length}字符）但缺少行动号召",
                            detail=f"描述: {desc}")

    # 14. 标题层级
    def _check_heading_structure(self) -> CheckResult:
        item = self._item(
            "ai-03", "大标题唯一性与子标题层级结构", Priority.P0,
            "标题层级是否规范，H1 是否唯一",
            "统计 H1/H2/H3 数量和嵌套关系",
            "仅1个H1，层级递进不跳级",
            manual_check="右键查看源代码，统计 <h1>、<h2>、<h3> 标签数量，看是否只有1个H1，H2/H3是否按层级递进",
            purpose="标题层级是 AI 理解页面内容结构的骨架。清晰的 H1→H2→H3 结构让 AI 能快速定位关键信息，更容易摘取成答案",
            excellent="仅1个H1（含核心关键词），H2按主题分块，H3在H2下细分，层级递进不跳级，每个标题都有实质内容",
            normal="有H1且数量≤2，有H2子标题，基本结构合理，但可能存在跳级或标题空泛",
            failed="缺少H1，或有多个H1（>3个），或完全没有H2/H3结构",
        )
        h1 = self.local_soup.find_all("h1")
        h2 = self.local_soup.find_all("h2")
        h3 = self.local_soup.find_all("h3")

        issues = []
        if len(h1) == 0:
            issues.append("缺少 H1 标签")
        elif len(h1) > 1:
            issues.append(f"有 {len(h1)} 个 H1（应仅1个）")
        if len(h2) == 0 and len(h1) > 0:
            issues.append("有 H1 但缺少 H2 子标题")

        if issues:
            return self._result(item, Status.WARN, "标题层级不规范",
                                detail=f"H1:{len(h1)} H2:{len(h2)} H3:{len(h3)}; {'; '.join(issues)}")
        return self._result(item, Status.PASS,
                            f"标题层级规范（H1:{len(h1)} H2:{len(h2)} H3:{len(h3)}）")

    # 15. 结构化数据
    def _check_structured_data(self) -> CheckResult:
        item = self._item(
            "ai-04", "结构化数据", Priority.P1,
            "是否有 Schema.org 结构化标记，是否含富结果类型",
            "检查 JSON-LD 和 Microdata，识别 Product/FAQ/Review 等富结果类型",
            "核心页面有 Organization/Product/Article/FAQPage 等标记",
            manual_check="右键查看源代码，搜索 application/ld+json，看是否有 Schema.org 结构化数据；或用 Google 富结果测试工具检测能出哪些富摘要",
            purpose="结构化数据是给 AI 和搜索引擎看的'内容标签'，能让 AI 精准识别你的产品、文章、评价等信息类型，大幅提升出富摘要和被引用的概率",
            excellent="有 Organization + Product/Article + FAQPage + BreadcrumbList 等多种结构化数据，含 Review/Product 等高价值富结果类型，字段完整无报错",
            normal="有至少1种结构化数据（如 Organization 或 Article），但缺少 Product/Review/FAQ 等高价值富结果类型，或字段不完整",
            failed="未检测到任何 JSON-LD 或 Microdata 结构化数据标记",
        )
        json_ld = self.local_soup.find_all("script", attrs={"type": "application/ld+json"})
        microdata = self.local_soup.find_all(attrs={"itemscope": True})

        schemas = []
        rich_types = ["Product", "Review", "FAQPage", "Article", "Organization",
                      "LocalBusiness", "Event", "Recipe", "BreadcrumbList", "Service"]
        found_rich = []
        for script in json_ld:
            try:
                data = json.loads(script.string)
                items = data if isinstance(data, list) else [data]
                for d in items:
                    if isinstance(d, dict):
                        t = d.get("@type", "")
                        if t:
                            schemas.append(str(t))
                            if t in rich_types:
                                found_rich.append(str(t))
            except Exception:
                pass

        if schemas:
            if found_rich:
                return self._result(item, Status.PASS,
                                    f"检测到 {len(set(schemas))} 种结构化数据，含富结果类型",
                                    detail=f"类型: {', '.join(set(schemas))}")
            return self._result(item, Status.WARN,
                                f"检测到 {len(set(schemas))} 种结构化数据，但缺少 Product/FAQ/Review 等高价值富结果类型",
                                detail=f"类型: {', '.join(set(schemas))}")
        if microdata:
            return self._result(item, Status.PASS,
                                f"检测到 {len(microdata)} 处 Microdata 标记")
        return self._result(item, Status.FAIL, "未检测到任何结构化数据标记")

    # 16. AI 答案格式优化
    def _check_answer_format(self) -> CheckResult:
        item = self._item(
            "ai-05", "AI 答案格式优化", Priority.P1,
            "内容是否包含 AI 易摘取的结构（列表、表格、定义、FAQ）",
            "检查页面是否有 ol/ul/table/definition 等结构",
            "有明确的是什么/为什么/怎么做结构",
            manual_check="浏览页面内容，看是否有编号列表、表格、定义列表、FAQ问答等 AI 容易直接摘取成答案的格式",
            purpose="AI 生成答案时偏好结构化内容。列表、表格、定义、FAQ 这些格式能被 AI 直接摘取，比大段文字更容易成为搜索答案",
            excellent="同时包含列表+表格+定义/FAQ等多种结构，每个核心知识点都有清晰的结构化呈现，可直接被AI摘取",
            normal="有1-2种结构化元素（如只有列表），但缺少表格或FAQ等其他格式",
            failed="内容全是大段文字，缺少列表、表格、FAQ等任何 AI 易摘取结构",
        )
        has_list = bool(self.local_soup.find(["ol", "ul"]))
        has_table = bool(self.local_soup.find("table"))
        has_definition = bool(self.local_soup.find(["dl", "dt"]))
        has_faq = bool(self.local_soup.find_all(class_=re.compile(r"faq", re.I)))

        structures = []
        if has_list:
            structures.append("列表")
        if has_table:
            structures.append("表格")
        if has_definition:
            structures.append("定义列表")
        if has_faq:
            structures.append("FAQ")

        if len(structures) >= 2:
            return self._result(item, Status.PASS,
                                f"内容包含多种 AI 友好结构: {', '.join(structures)}")
        if len(structures) == 1:
            return self._result(item, Status.WARN,
                                f"仅包含 1 种结构: {structures[0]}，建议增加列表/表格/FAQ")
        return self._result(item, Status.FAIL,
                            "内容缺少列表、表格、FAQ 等 AI 易摘取结构")

    # 17. 面包屑导航（全站级：检查内页是否有面包屑）
    def _check_breadcrumb(self) -> CheckResult:
        item = self._item(
            "ai-06", "面包屑导航", Priority.P2,
            "是否有面包屑导航和对应结构化标记",
            "检查 BreadcrumbList Schema 或常见面包屑 DOM",
            "有面包屑路径，对应 BreadcrumbList Schema",
            manual_check="看内页顶部是否有'首页 > 产品 > XXX'的路径导航；右键查看源代码是否有 BreadcrumbList 结构化数据",
            purpose="面包屑帮 AI 理解页面在网站中的层级位置，同时能在搜索结果中显示面包屑路径，提升点击率",
            excellent="内页有面包屑导航，且有 BreadcrumbList 结构化数据标记，路径层级清晰准确",
            normal="有面包屑导航 DOM，但缺少 BreadcrumbList 结构化标记",
            failed="内页未检测到面包屑导航",
            tier="bonus",
        )
        # 面包屑是内页专属，从抽样内页中检查
        check_page = self._pick_content_page()
        if not check_page:
            return self._result(item, Status.SKIP, "未抓取到内容页，无法检查面包屑导航",
                                detail="需抓取内页才能检测面包屑")
        target_soup = check_page.soup
        json_ld = target_soup.find_all("script", attrs={"type": "application/ld+json"})
        has_breadcrumb_schema = False
        for script in json_ld:
            try:
                data = json.loads(script.string)
                if isinstance(data, dict) and data.get("@type") == "BreadcrumbList":
                    has_breadcrumb_schema = True
                elif isinstance(data, list):
                    for d in data:
                        if isinstance(d, dict) and d.get("@type") == "BreadcrumbList":
                            has_breadcrumb_schema = True
            except Exception:
                pass

        breadcrumb_dom = target_soup.find(class_=re.compile(r"breadcrumb", re.I))

        _pg = self._pg(check_page)
        if has_breadcrumb_schema:
            return self._result(item, Status.PASS, "有 BreadcrumbList 结构化标记",
                                detail=f"检查页面: {check_page.final_url}", pages=_pg)
        if breadcrumb_dom:
            return self._result(item, Status.WARN, "有面包屑 DOM 但缺少结构化标记",
                                detail=f"检查页面: {check_page.final_url}", pages=_pg)
        return self._result(item, Status.WARN, "未检测到面包屑导航",
                            detail=f"检查页面: {check_page.final_url}", pages=_pg)

    # 18. 多页面抽样
    def _check_multi_page(self) -> CheckResult:
        item = self._item(
            "ai-07", "站内链接丰富度", Priority.P1,
            "是否有足够的内部链接指向其他页面",
            "统计站内链接数量",
            "核心页面间有互联",
            manual_check="浏览首页，统计有多少个链接指向网站内其他页面（产品、案例、文章等），看是否形成页面互联网络",
            purpose="内部链接是 AI 爬虫发现你其他页面的主要路径。链接丰富的站点能让 AI 抓取更多页面，理解更全面的品牌信息",
            excellent="有大量（≥10个）描述性内部链接，形成产品-案例-文章的互联网络，锚文本包含关键词",
            normal="有一定数量（5-10个）的内部链接，基本覆盖主要页面，但锚文本可能不够描述性",
            failed="站内链接极少（<5个），或几乎没有指向其他内容页面的链接",
        )
        from urllib.parse import urlparse
        base_domain = urlparse(self.page.final_url).netloc
        internal_links = []
        for a in self.local_soup.find_all("a", href=True):
            href = a["href"]
            if href.startswith(("#", "javascript:", "mailto:", "tel:")):
                continue
            if href.startswith("/") or base_domain in href:
                internal_links.append(href)

        unique_links = len(set(internal_links))
        if unique_links >= 5:
            return self._result(item, Status.PASS, f"有 {unique_links} 个站内链接")
        if unique_links >= 1:
            return self._result(item, Status.WARN,
                                f"站内链接较少（{unique_links}个），建议增加页面互联")
        return self._result(item, Status.FAIL, "几乎没有站内链接")

    # 19. AI 平台收录
    def _check_ai_indexing(self) -> CheckResult:
        item = self._item(
            "ai-08", "搜索引擎/AI 平台验证", Priority.P2,
            "是否有搜索引擎站长平台验证标签",
            "检查 google-site-verification、baidu-site-verification 等 meta",
            "已提交 sitemap，在主流平台可检索",
            manual_check="右键查看源代码，搜索 site-verification，看是否有百度、谷歌、必应等站长平台的验证标签",
            purpose="站长平台验证能让你主动提交 sitemap、查看收录情况、接收抓取异常通知，是 SEO 和 GEO 的基础操作",
            excellent="已验证百度+谷歌+必应等多个站长平台，已提交 sitemap，能查看收录和抓取数据",
            normal="已验证至少1个主流站长平台（百度或谷歌）",
            failed="未检测到任何搜索引擎站长平台验证标签",
        )
        verifications = []
        for name in ["google-site-verification", "baidu-site-verification",
                     "msvalidate.01", "360-site-verification", "sogou_site_verification"]:
            if self.page.meta(name):
                verifications.append(name.split("-")[0])

        if verifications:
            return self._result(item, Status.PASS,
                                f"已验证平台: {', '.join(verifications)}")
        return self._result(item, Status.WARN,
                            "未检测到搜索引擎站长平台验证标签")

    # AI-09 NLP/大模型语义友好度
    def _check_nlp_friendly(self) -> CheckResult:
        item = self._item(
            "ai-09", "NLP/大模型语义友好度", Priority.P1,
            "正文在语义层面是否利于大模型理解与摘取：概念定义、实体明确、句式简洁、因果清晰",
            "AI：分析正文概念是否先定义、指代是否明确、事实陈述是否完整（内容页检查）",
            "概念先定义、实体明确无歧义、句子简洁，AI可逐句直接摘取",
            manual_check="在文章/案例/产品详情页通读正文：专业概念首次出现是否有定义；是否有'它/这个/那种'等模糊指代；一段话是否只讲一个点；事实是否'主语+结论+依据'完整",
            purpose="结构标签解决'形'，这项解决'神'。语义清晰的内容大模型能准确提取事实；首页是导航页，需在内容页检查正文语义质量",
            excellent="概念先定义后展开，实体名称明确无模糊指代，句子简洁，事实陈述完整（主语+结论+依据），AI可直接逐句摘取",
            normal="整体可读，但部分段落冗长或存在指代不清，需要AI二次归纳才能引用",
            failed="大段绕弯文字、概念无定义、指代混乱、因果不清，AI无法准确提取事实",
        )
        if not self.ai_engine:
            return self._result(item, Status.SKIP, "未检查（需配置AI语义引擎）",
                                detail="未配置LLM，无法进行语义分析")
        content_page = self._pick_content_page()
        if not content_page:
            return self._result(item, Status.SKIP,
                                "NLP语义友好度为内容页检查项，首页不适用，且未抽样到内容页",
                                detail="语义分析需在有主体正文的内容页上进行，首页是品牌导航页不适用")
        status, summary, detail = self.ai_engine.consensus(lambda: self.ai_engine.check_nlp_friendly(content_page))
        page_label = content_page.soup.title.get_text(strip=True)[:30] if content_page.soup.title else content_page.final_url
        return self._result(item, status, summary, detail=f"检查页面: {page_label}。{detail}",
                            pages=self._pg(content_page))

    # AI-10 内容分块与逻辑组织
    def _check_content_chunking(self) -> CheckResult:
        item = self._item(
            "ai-10", "内容分块与逻辑组织", Priority.P2,
            "每个小标题下内容是否聚焦单一要点，块间逻辑是否递进，便于AI按问题整块摘取",
            "AI：逐块判断标题与内容是否匹配、块间逻辑顺序、单块长度（内容页检查）",
            "每块聚焦单一要点、块间逻辑递进、单块长度适中可独立引用",
            manual_check="在内容页逐个小标题检查其下内容是否都服务于该标题；块之间是否按'是什么→为什么→怎么做'递进；单个板块是否过长需要再拆分",
            purpose="好的分块让AI能按用户问题精准定位并整块摘取。首页是导航页，分块质量需在内容页上检查",
            excellent="每个分块聚焦单一要点，块间逻辑递进清晰，单块长度适中，可独立成段被AI直接引用",
            normal="分块基本合理，少数板块内容偏离主题或过长，整体仍可理解",
            failed="标题与内容不对应、逻辑跳跃混乱，或整篇无有效分块、大段文字堆砌",
        )
        if not self.ai_engine:
            return self._result(item, Status.SKIP, "未检查（需配置AI语义引擎）",
                                detail="未配置LLM，无法进行语义分析")
        content_page = self._pick_content_page()
        if not content_page:
            return self._result(item, Status.SKIP,
                                "内容分块为内容页检查项，首页不适用，且未抽样到内容页",
                                detail="分块分析需在有标题和正文的内容页上进行，首页是品牌导航页不适用")
        status, summary, detail = self.ai_engine.consensus(lambda: self.ai_engine.check_content_chunking(content_page))
        page_label = content_page.soup.title.get_text(strip=True)[:30] if content_page.soup.title else content_page.final_url
        return self._result(item, status, summary, detail=f"检查页面: {page_label}。{detail}",
                            pages=self._pg(content_page))
