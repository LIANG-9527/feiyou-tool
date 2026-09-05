"""内容质量检查（6项）"""
import re
from typing import List
from .checker import BaseChecker
from site_audit.core.models import CheckResult, Status, Priority, Category


class ContentChecker(BaseChecker):
    category = Category.CONTENT

    def run(self) -> List[CheckResult]:
        results = []
        results.append(self._check_content_depth())
        results.append(self._check_content_freshness())
        results.append(self._check_faq())
        results.append(self._check_image_optimization())
        results.append(self._check_internal_link_quality())
        results.append(self._check_multilingual())
        results.append(self._check_search_intent())
        # content-08 关键词蚕食已移至 cross_page.CrossPageChecker（需多页面）
        return results

    # 20. 内容深度
    def _check_content_depth(self) -> CheckResult:
        item = self._item(
            "content-01", "核心内容深度", Priority.P0,
            "正文长度和信息密度是否足够",
            "统计页面纯文本字数",
            "核心页面正文 > 500 字，有具体参数/场景/数据",
            manual_check="浏览页面正文，看是否有详细的产品参数、应用场景、数据支撑、具体案例，而不是空泛的营销口号",
            purpose="AI 引用内容时偏好信息密度高的页面。内容过薄的页面 AI 无法提取有效信息，不会被选为答案来源",
            excellent="正文 > 1000 字，包含具体参数、数据、场景、对比，信息密度高，有独特观点和深度分析",
            normal="正文 500-1000 字，有基本的产品介绍和场景说明，但数据和细节不足",
            failed="正文 < 500 字，内容过薄，多为空泛营销语，AI 难以提取有效信息",
        )
        text = self.get_text()
        word_count = len(text)

        if word_count < 200:
            return self._result(item, Status.FAIL,
                                f"内容过薄（约 {word_count} 字），AI 难以提取有效信息")
        if word_count < 500:
            return self._result(item, Status.WARN,
                                f"内容偏少（约 {word_count} 字），建议补充细节")
        return self._result(item, Status.PASS, f"内容充足（约 {word_count} 字）")

    # 21. 内容原创性与时效性（全站级：检查所有内容页的时间标注与原创性）
    def _check_content_freshness(self) -> CheckResult:
        item = self._item(
            "content-02", "内容原创性与时效性", Priority.P1,
            "是否标注发布/更新时间；内容是否有原创价值（规则检测时效性，AI判断原创性）",
            "规则：检查 time 标签、article:published_time；AI：判断是否有独到观点/一手数据还是通用水文",
            "标注发布/更新时间且近6个月更新，同时内容具备原创观点或一手信息",
            manual_check="逐页看是否有发布/最后更新时间；通读内容判断是否有自己的观点、一手数据、原创案例，还是全网通用套话",
            purpose="AI 偏好新鲜且有独特价值的内容。更新时间让 AI 判断内容是否过时；原创性决定 AI 是否认为该内容比其他来源更值得引用，拼凑/AI水文会被降权",
            excellent="全部内容页标注发布与更新时间（近3个月，ISO 8601格式），且内容含独到观点、一手数据或原创案例",
            normal="部分页面有时间标注但更新较久（超6个月），或内容原创性一般、部分为通用资料整理",
            failed="多数页面未标注时间，且内容为全网通用套话/AI水文，无原创观点和一手数据",
        )

        # 收集所有页面（首页+内页），但只对有实质内容的页面检查
        all_pages = [self.page] + self.extra_pages
        content_pages = [p for p in all_pages if p.word_count() > 100]

        if not content_pages:
            return self._result(item, Status.SKIP, "未抓取到内容页，无法检查原创性与时效性",
                                detail="抽样内页均无足够正文内容")

        # 逐页检查时间标注
        pages_with_time = 0
        pages_without_time = []
        for p in content_pages:
            time_tags = p.soup.find_all("time")
            pub_time = p.meta("article:published_time")
            mod_time = p.meta("article:modified_time")
            if time_tags or pub_time or mod_time:
                pages_with_time += 1
            else:
                pages_without_time.append(p.final_url)

        time_ratio = pages_with_time / len(content_pages) if content_pages else 0

        # AI原创性分析：检查最具代表性的内容页
        orig_status, orig_summary, orig_detail = (Status.SKIP, "", "")
        check_page = self._pick_content_page()
        if not check_page:
            check_page = content_pages[0] if content_pages else self.page
        if self.ai_engine:
            orig_status, orig_summary, orig_detail = self.ai_engine.consensus(lambda: self.ai_engine.check_originality(check_page))

        # 综合判断
        _cp = self._pg(check_page)
        # 时间标注的结论覆盖全部内容页，来源须挂全量实测页面，不能只挂原创性抽中的那一页
        _all_content = [p.final_url for p in content_pages] or _cp
        if time_ratio >= 0.8:
            time_msg = f"{pages_with_time}/{len(content_pages)} 个内容页标注了时间"
            if orig_status != Status.SKIP:
                return self._result(item, orig_status,
                                    f"{time_msg}。{orig_summary}",
                                    detail=f"时间标注: {time_msg}。原创性: {orig_detail}。检查页面: {check_page.final_url}",
                                    pages=_all_content)
            return self._result(item, Status.PASS, f"{time_msg}",
                                detail=f"检查页面: {check_page.final_url}", pages=_all_content)
        elif time_ratio >= 0.3:
            time_msg = f"{pages_with_time}/{len(content_pages)} 个内容页标注了时间，{len(pages_without_time)} 个未标注"
            if orig_status != Status.SKIP:
                return self._result(item, Status.WARN,
                                    f"{time_msg}。{orig_summary}",
                                    detail=f"未标注页面: {', '.join(pages_without_time[:8])}。原创性: {orig_detail}。检查页面: {check_page.final_url}",
                                    pages=_all_content)
            return self._result(item, Status.WARN, time_msg,
                                detail=f"未标注页面: {', '.join(pages_without_time[:8])}", pages=_all_content)
        else:
            time_msg = f"仅 {pages_with_time}/{len(content_pages)} 个内容页标注了时间"
            if orig_status != Status.SKIP:
                status = Status.WARN if orig_status == Status.PASS else orig_status
                return self._result(item, status,
                                    f"{time_msg}。{orig_summary}",
                                    detail=f"未标注页面: {', '.join(pages_without_time[:8])}。原创性: {orig_detail}。检查页面: {check_page.final_url}",
                                    pages=_all_content)
            return self._result(item, Status.WARN, time_msg,
                                detail=f"未标注页面: {', '.join(pages_without_time[:8])}", pages=_all_content)

    # 内容-07 搜索意图匹配
    def _check_search_intent(self) -> CheckResult:
        item = self._item(
            "content-07", "搜索意图匹配", Priority.P1,
            "页面内容类型是否匹配目标关键词背后的用户意图（信息型/比较型/交易型）",
            "AI：识别页面核心关键词与用户意图，判断内容类型是否对应",
            "内容类型与目标搜索意图高度匹配，覆盖该决策阶段的核心问题",
            manual_check="先确定这页想被搜到的核心词，想清楚用户搜它时是想'学知识/比方案/直接买'，再看页面内容类型是否对得上——想找方案却看到公司介绍就是错位",
            purpose="搜索意图匹配是'有流量没转化'和AI不引用的根因。AI回答问题时会优先选择内容类型与用户意图一致的页面，首页意图天然是品牌型，搜索意图匹配需在内容页上检查",
            excellent="核心搜索意图明确，内容类型（科普/方案/产品/对比）与意图高度匹配，完整覆盖用户在该决策阶段的核心问题",
            normal="意图基本相关但覆盖不全，或一页混合多种意图导致重点不够聚焦",
            failed="内容类型与目标意图明显错位（如方案词对应公司介绍页），或页面无明确主题焦点",
        )
        if not self.ai_engine:
            return self._result(item, Status.SKIP, "未检查（需配置AI语义引擎）",
                                detail="未配置LLM，无法进行意图分析")
        content_page = self._pick_content_page()
        if not content_page:
            return self._result(item, Status.SKIP,
                                "搜索意图匹配为内容页检查项，首页不适用，且未抽样到内容页",
                                detail="搜索意图分析需在有明确主题的内容页上进行，首页是品牌导航页不适用")
        status, summary, detail = self.ai_engine.consensus(lambda: self.ai_engine.check_search_intent(content_page))
        page_label = content_page.soup.title.get_text(strip=True)[:30] if content_page.soup.title else content_page.final_url
        return self._result(item, status, summary, detail=f"检查页面: {page_label}。{detail}",
                            pages=self._pg(content_page))

    # 22. FAQ 结构
    def _check_faq(self) -> CheckResult:
        item = self._item(
            "content-03", "FAQ 问答结构", Priority.P1,
            "是否有 FAQ 板块和对应结构化标记",
            "检查 FAQPage Schema 和常见 FAQ DOM",
            "有 FAQ 板块，使用 FAQPage 结构化标记",
            manual_check="看页面是否有'常见问题'板块，每个问题是否有完整答案；右键查看源代码是否有 FAQPage 结构化数据",
            purpose="FAQ 是 AI 摘取答案的最高效格式。用户问'XX怎么样''XX多少钱'时，AI 会优先从 FAQ 中提取答案",
            excellent="有 FAQ 板块（≥5个问答），每个问题有完整答案，且有 FAQPage 结构化数据标记，覆盖客户高频问题",
            normal="有 FAQ 板块但缺少结构化标记，或问答数量较少（<5个）",
            failed="未检测到任何 FAQ 问答结构",
        )
        import json
        json_ld = self.local_soup.find_all("script", attrs={"type": "application/ld+json"})
        has_faq_schema = False
        for script in json_ld:
            try:
                data = json.loads(script.string)
                items = data if isinstance(data, list) else [data]
                for d in items:
                    if isinstance(d, dict) and d.get("@type") == "FAQPage":
                        has_faq_schema = True
            except Exception:
                pass

        # 方式1：class 含 faq/question/accordion/collapse
        faq_dom = self.local_soup.find(class_=re.compile(r"faq|question|accordion|collapse", re.I))
        # 方式2：dt/dd 标签
        has_qa_pattern = bool(self.local_soup.find_all(["dt", "dd"]))
        # 方式3：details/summary 折叠面板
        has_details = bool(self.local_soup.find_all("details"))
        # 方式4：通过"常见问题/FAQ/常见问答"关键词定位标题，统计所在区块的问答数
        faq_question_count = 0
        faq_heading = None
        for tag in self.local_soup.find_all(["h1", "h2", "h3", "h4"]):
            text = tag.get_text(strip=True)
            if re.search(r"常见问题|常见问答|FAQ|Q&A|问与答|帮助中心", text, re.I):
                faq_heading = tag
                break
        if faq_heading:
            # 向上找到 section/div 容器
            container = faq_heading.parent
            for _ in range(4):
                if container and container.name not in ["section", "div"]:
                    container = container.parent
                else:
                    break
            if container:
                # 统计容器内以问号结尾的短句（问题特征）
                for el in container.find_all(["h3", "h4", "h5", "button", "a", "p", "div", "span"]):
                    t = el.get_text(strip=True)
                    if 5 <= len(t) <= 80 and (t.endswith("?") or t.endswith("？")):
                        faq_question_count += 1
        # 方式5：全局统计以问号结尾的短标题
        global_questions = 0
        for tag in self.local_soup.find_all(["h3", "h4", "h5", "button"]):
            t = tag.get_text(strip=True)
            if 5 <= len(t) <= 80 and (t.endswith("?") or t.endswith("？")):
                global_questions += 1

        has_faq_dom = bool(faq_dom) or has_qa_pattern or has_details or faq_question_count >= 2 or global_questions >= 3
        question_count = max(faq_question_count, global_questions)

        if has_faq_schema:
            return self._result(item, Status.PASS, f"有 FAQPage 结构化标记（{question_count or '若干'}个问答）")
        if has_faq_dom:
            detail = f"检测到约 {question_count} 个问题" if question_count else "检测到FAQ板块结构"
            return self._result(item, Status.WARN, "有 FAQ 板块但缺少 FAQPage 结构化标记",
                                detail=detail)
        return self._result(item, Status.WARN, "未检测到 FAQ 问答结构")

    # 23. 图片优化
    def _check_image_optimization(self) -> CheckResult:
        item = self._item(
            "content-04", "图片与多媒体优化", Priority.P2,
            "图片是否有 alt 文本，文件名是否描述性",
            "统计图片 alt 覆盖率和文件名质量",
            "重要图片有描述性 alt 和描述性文件名",
            manual_check="右键查看源代码，统计 <img> 标签的 alt 属性覆盖率，看重要图片是否有描述性 alt，文件名是 img1.jpg 还是 geo-optimization.png",
            purpose="图片 alt 让 AI 理解图片内容，也是图片搜索排名的基础。描述性文件名能进一步帮助搜索引擎和 AI 理解图片主题",
            excellent="≥80% 图片有描述性 alt（含关键词），文件名描述性（非 img1.jpg/纯数字），图片已压缩",
            normal="50-80% 图片有 alt，但部分重要图片缺少或文件名不描述性",
            failed="<50% 图片有 alt，或重要产品图完全没有 alt",
        )
        images = self.local_soup.find_all("img")
        if not images:
            return self._result(item, Status.SKIP, "页面无图片")

        # 无效 alt 分三类：空 / 模板残留（如 {{$field.title}}、{$vo.xxx}、eyou 标签）/ 无意义占位
        _tpl_residue = re.compile(r"\{\{|\{\$|\{%|\{:|%\}|\$field|\$vo|\$[a-zA-Z_]+\.|eyou:|volist|think\.")
        _meaningless = {"图片", "图像", "image", "img", "photo", "pic", "-", "--", "|",
                        "alt", "图片占位", "暂无图片", "nophoto", "default", "icon"}

        def _alt_problem(img):
            a = img.get("alt")
            if a is None or not str(a).strip():
                return "空"
            a = str(a).strip()
            if _tpl_residue.search(a):
                return "模板残留"
            if a.lower() in _meaningless or len(a) <= 1:
                return "无意义"
            return None

        no_alt, residue_alt, meaningless_alt, residue_samples = [], [], [], []
        for img in images:
            kind = _alt_problem(img)
            if kind == "空":
                no_alt.append(img)
            elif kind == "模板残留":
                residue_alt.append(img)
                if len(residue_samples) < 3:
                    residue_samples.append(str(img.get("alt")).strip())
            elif kind == "无意义":
                meaningless_alt.append(img)
        invalid_alt = no_alt + residue_alt + meaningless_alt  # 写了等于没写的都算无效
        missing_ratio = len(invalid_alt) / len(images)

        # 检查文件名描述性（排除纯数字、img1、DSC、无意义名称）
        import os
        bad_filename_patterns = [
            r'^\d+\.', r'^img\d+\.', r'^image\d+\.', r'^dsc\d+\.',
            r'^pic\d+\.', r'^photo\d+\.', r'^u\d+\.', r'^nophoto',
            r'^default\.', r'^placeholder\.', r'^blank\.',
        ]
        bad_filenames = 0
        for img in images:
            src = img.get("src", "")
            filename = os.path.basename(src).lower()
            if not filename:
                continue
            import re as _re
            if any(_re.match(p, filename) for p in bad_filename_patterns):
                bad_filenames += 1
        bad_filename_ratio = bad_filenames / len(images) if images else 0

        # 分析无效 alt 的图片类型（沿用原分类）
        if no_alt:
            from collections import Counter
            types = Counter()
            for img in no_alt:
                src = img.get("src", "")
                if "logo" in src.lower():
                    types["Logo"] += 1
                elif "slide" in src.lower() or "banner" in src.lower():
                    types["Banner图"] += 1
                elif "sort" in src.lower() or "category" in src.lower() or "class" in src.lower():
                    types["分类/产品图标"] += 1
                elif "template" in src.lower():
                    types["模板装饰图"] += 1
                elif "upload" in src.lower():
                    types["上传图片"] += 1
                else:
                    types["其他"] += 1
            detail_parts = ["空白alt: " + "、".join(f"{k}{v}张" for k, v in types.most_common())]
        else:
            detail_parts = []
        # 模板残留与无意义占位单独点名（这是"写了等于没写"的模板 bug）
        if residue_alt:
            detail_parts.append(f"{len(residue_alt)} 张图片的 alt 是没渲染出来的模板代码（如 {'、'.join(residue_samples)}），等于没写")
        if meaningless_alt:
            detail_parts.append(f"{len(meaningless_alt)} 张图片的 alt 只是'图片/image'之类无意义占位")

        if bad_filenames > 0:
            detail_parts.append(f"无意义文件名: {bad_filenames}/{len(images)} 张（{bad_filename_ratio:.0%}）")
        detail = "；".join(detail_parts) if detail_parts else None

        # 综合判断：有效 alt 覆盖率为主，文件名问题作为补充
        if missing_ratio == 0 and bad_filename_ratio < 0.3:
            return self._result(item, Status.PASS,
                                f"全部 {len(images)} 张图片都有有效 alt，文件名质量良好")
        if missing_ratio < 0.3:
            return self._result(item, Status.WARN,
                                f"{len(invalid_alt)}/{len(images)} 张图片缺少有效 alt",
                                detail=detail)
        return self._result(item, Status.FAIL,
                            f"{len(invalid_alt)}/{len(images)} 张图片缺少有效 alt（{missing_ratio:.0%}，含空白/模板残留/无意义占位）",
                            detail=detail)

    # 24. 内部链接质量
    def _check_internal_link_quality(self) -> CheckResult:
        item = self._item(
            "content-05", "内部链接布局", Priority.P1,
            "内部链接锚文本是否描述性",
            "检查站内链接的锚文本，统计「点击这里」类无意义链接",
            "锚文本描述性，非「点击这里」",
            manual_check="浏览页面，看内部链接的文字是'点击这里''了解更多'还是具体的'GEO优化服务''客户案例'等描述性文字",
            purpose="锚文本告诉 AI 被链接页面是关于什么的。描述性锚文本能帮助 AI 理解页面间关系，提升目标页面的关键词相关性",
            excellent="几乎所有内部链接都使用描述性锚文本（含关键词），无'点击这里'类无意义链接，锚文本与目标页面强相关",
            normal="大部分链接有描述性，但存在少量（<40%）'了解更多''详情'类通用锚文本",
            failed="大量内部链接使用'点击这里''了解更多'等无意义锚文本（≥40%）",
        )
        from urllib.parse import urlparse
        base_domain = urlparse(self.page.final_url).netloc
        generic_texts = ["点击这里", "点击查看", "了解更多", "更多", "here", "click here",
                         "learn more", "read more", "详情", "查看详情"]

        internal_links = []
        for a in self.local_soup.find_all("a", href=True):
            href = a["href"]
            text = a.text.strip()
            if href.startswith(("#", "javascript:", "mailto:", "tel:")):
                continue
            if href.startswith("/") or base_domain in href:
                if text and len(text) < 20:
                    internal_links.append(text)

        if not internal_links:
            return self._result(item, Status.SKIP, "无内部链接可分析")

        generic_count = sum(1 for t in internal_links
                           if any(g in t.lower() for g in generic_texts))
        ratio = generic_count / len(internal_links)

        if ratio == 0:
            return self._result(item, Status.PASS,
                                f"内部链接锚文本均为描述性（{len(internal_links)}个）")
        if ratio < 0.4:
            return self._result(item, Status.WARN,
                                f"{generic_count}/{len(internal_links)} 个链接锚文本无意义")
        return self._result(item, Status.WARN,
                            f"较多链接锚文本无意义（{generic_count}/{len(internal_links)}），建议优化")

    # 25. 多语言
    def _check_multilingual(self) -> CheckResult:
        item = self._item(
            "content-06", "多语言/多地区设置", Priority.P2,
            "是否有 hreflang 或语言切换",
            "检查 link rel=alternate hreflang 和语言切换按钮",
            "如有多语言，使用 hreflang 标注",
            manual_check="看页面是否有语言切换按钮；右键查看源代码搜索 hreflang，看是否标注了各语言版本",
            purpose="hreflang 告诉 AI 每个语言版本对应哪个地区的用户，避免不同语言版本互相竞争排名，提升海外用户的 AI 搜索可见性",
            excellent="有多语言站点，每个页面都有正确的 hreflang 标记（含 x-default），语言切换功能正常，各语言内容完整",
            normal="有语言切换功能，但缺少 hreflang 标记，或 hreflang 标注不完整",
            failed="单语言站点（不适用），或有多语言但完全没有 hreflang 且语言切换混乱",
            tier="bonus",
        )
        hreflang = self.local_soup.find_all("link", attrs={"rel": "alternate", "hreflang": True})
        lang_switch = self.local_soup.find(class_=re.compile(r"lang|language|i18n", re.I))

        if hreflang:
            langs = [l.get("hreflang") for l in hreflang if l.get("hreflang")]
            return self._result(item, Status.PASS,
                                f"有 hreflang 标记（{len(set(langs))}种语言）",
                                detail=f"语言: {', '.join(set(langs))}")
        if lang_switch:
            return self._result(item, Status.WARN, "有语言切换但缺少 hreflang 标记")
        return self._result(item, Status.PASS, "单语言站点，无需多语言设置",
                            detail="未检测到 hreflang 和语言切换，站点为单语言，符合预期")
