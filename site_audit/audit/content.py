"""内容质量检查（6项）"""
import re
from typing import List
from urllib.parse import urlparse
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

        # 2026-09-10 V6 拆分：时间标注保留计分；AI 原创性判断移出计分改只提醒。
        all_pages = [self.page] + self.extra_pages
        content_pages = [p for p in all_pages if p.word_count() > 100]
        if not content_pages:
            return self._result(item, Status.SKIP, "未抓取到内容页，无法检查时效性",
                                detail="抽样内页均无足够正文内容", skip_kind="missing")

        _DATE_RE = re.compile(
            r"(?:发布|更新|发表于|更新于|发布时间|更新时间)[:：]?\s*"
            r"(20\d{2}[-年/.]\d{1,2}(?:[-月/.]\d{1,2})?|20\d{2}年\d{1,2}月)", re.I)

        def _has_time_signal(p):
            if p.soup.find("time", attrs={"datetime": True}) is not None:
                return True
            if p.meta("article:published_time") or p.meta("article:modified_time") or p.meta("og:updated_time"):
                return True
            return bool(_DATE_RE.search(p.get_text()))

        with_time = sum(1 for p in content_pages if _has_time_signal(p))
        ratio = with_time / len(content_pages)
        pages_without = [p.final_url for p in content_pages if not _has_time_signal(p)]

        # 永不判不合格：没时间标注不阻断收录，只是 AI 找新鲜内容时优先别人
        status = Status.PASS if ratio >= 0.8 else Status.WARN
        msg = f"{with_time}/{len(content_pages)} 个内容页标注了发布/更新时间"
        if ratio < 0.3:
            msg += "（高优先提醒：AI 判断内容新鲜度主要依赖时间标注）"
        detail = f"未标注页面: {', '.join(pages_without[:8])}" if pages_without else None

        # 防造假：站点地图 lastmod 同日占比 ≥50% → 封顶"不足（疑似批量回填）"
        same_ratio, lastmod_n = self._sitemap_lastmod_same_day_ratio()
        if same_ratio is not None and lastmod_n and same_ratio >= 0.5:
            status = Status.WARN
            msg += f"；站点地图 {lastmod_n} 个网址中 {int(same_ratio * 100)}% 的 lastmod 集中在同一天（疑似批量回填）"

        if self.ai_engine:
            check_page = self._pick_content_page() or content_pages[0]
            _os, _om, _od = self.ai_engine.consensus(lambda: self.ai_engine.check_originality(check_page))
            if _om or _od:
                detail = ((detail + "。" if detail else "") +
                          f"原创性参考（不计分，需人工复核）: {_om or _od}。检查页面: {check_page.final_url}")

        return self._result(item, status, msg, detail=detail,
                            pages=[p.final_url for p in content_pages])

    def _sitemap_lastmod_same_day_ratio(self):
        """解析站点地图取 lastmod，返回 (同日占比, 样本数)；样本 <5 返回 (None, 0)。"""
        from collections import Counter
        sm_declared = None
        rbt = self.fetcher.fetch_raw(f"{self.base_url}/robots.txt") if self.fetcher else None
        if rbt is not None and rbt.status_code == 200:
            msm = re.search(r"(?im)^\s*Sitemap:\s*(\S+)", rbt.text)
            if msm:
                sm_declared = msm.group(1).strip()
        candidates = ([sm_declared] if sm_declared else []) + [f"{self.base_url}/sitemap.xml"]
        lms = []
        for u in candidates:
            if not u:
                continue
            resp = self.fetcher.fetch_raw(u) if self.fetcher else None
            if resp is None or resp.status_code != 200:
                continue
            xml = resp.text
            sub_maps = re.findall(r"<sitemap>.*?<loc>(.*?)</loc>", xml, re.S | re.I)
            blocks = re.findall(r"<url>(.*?)</url>", xml, re.S | re.I)
            if sub_maps and not blocks:
                for su in sub_maps[:5]:
                    sr = self.fetcher.fetch_raw(su)
                    if sr is not None and sr.status_code == 200:
                        lms += [m.group(1)[:10] for m in re.finditer(r"<lastmod>(.*?)</lastmod>", sr.text, re.I | re.S)]
            else:
                lms = [m.group(1)[:10] for m in re.finditer(r"<lastmod>(.*?)</lastmod>", xml, re.I | re.S)]
            break
        if len(lms) < 5:
            return None, 0
        top_day, top_n = Counter(lms).most_common(1)[0]
        return top_n / len(lms), len(lms)

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
                                "未抓取到内容详情页（栏目/列表页不作为检查对象），无法检查搜索意图匹配，需人工复核",
                                detail="搜索意图分析需在有明确主题的文章/产品详情页上进行；仅抓到栏目列表页时本项不判分",
                                skip_kind="missing")
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

        def _page_faq_profile(soup):
            """单页 FAQ 档案：(有FAQPage标记, 问答数, 有FAQ板块, 问题集合, 是否页脚模板复制)。"""
            has_schema = False
            for script in soup.find_all("script", attrs={"type": "application/ld+json"}):
                try:
                    data = json.loads(script.string)
                    items = data if isinstance(data, list) else [data]
                    for d in items:
                        if isinstance(d, dict) and d.get("@type") == "FAQPage":
                            has_schema = True
                except Exception:
                    pass
            q_texts = []
            for dt in soup.find_all("dt"):
                t = dt.get_text(strip=True)
                if t:
                    q_texts.append(t)
            q_texts += [d.get_text(strip=True) for d in soup.find_all("details")
                        if d.find("summary") and d.find("summary").get_text(strip=True)]
            for tag in soup.find_all(["h3", "h4", "h5", "button"]):
                t = tag.get_text(strip=True)
                if 5 <= len(t) <= 80 and (t.endswith("?") or t.endswith("？")):
                    q_texts.append(t)
            q_set = frozenset(re.sub(r"\s+", "", t) for t in q_texts)
            # 是否页脚/侧栏模板复制（J2：位于 footer/aside 的问答定性为 boilerplate）
            faq_dom = soup.find(class_=re.compile(r"faq|question|accordion|collapse", re.I))
            from_footer = bool(faq_dom and faq_dom.find_parent(["footer", "aside"]))
            has_faq = bool(faq_dom) or len(q_texts) >= 2 or bool(soup.find_all("details"))
            return has_schema, len(q_set), has_faq, q_set, from_footer

        # 全站扫描：允许多个地方写 FAQ，哪个页面有问题就报哪个页面（业务方口径）
        schema_pages, dom_only_pages = [], []
        faq_pages, qset_groups = 0, {}
        for p in self.all_pages:
            label = (urlparse(p.final_url).path or "/")
            has_schema, q_count, has_faq, q_set, from_footer = _page_faq_profile(p.soup)
            if has_schema:
                schema_pages.append((label, q_count))
            elif has_faq:
                is_faq_path = bool(re.search(r"/(faq|cjwt|changjianwenti)", label, re.I))
                dom_only_pages.append((label, q_count, is_faq_path))
            if has_faq and q_set:
                faq_pages += 1
                qset_groups.setdefault(q_set, []).append(label)
        # E1/G5：跨页重复率硬指标——多少页挂着同一组一模一样的问答（页脚模板复制）
        dup_note, dup_rate = "", 0.0
        if faq_pages >= 2:
            top_set = max(qset_groups.values(), key=len)
            k = len(top_set)
            dup_rate = k / faq_pages * 100
            if k >= 2:
                n_questions = len(next(iter(qset_groups)))
                dup_note = (f"页脚FAQ跨页重复率 {dup_rate:.0f}%（{k}/{faq_pages} 页挂着同一组 {n_questions} 问，"
                            f"如 {top_set[0]}）——属模板复制，建议收敛到独立 FAQ 单页再打 FAQPage 标记，勿逐页复制")
                if from_footer_note := ("" if False else ""):
                    pass
        footer_copy = any(x[3] if False else False for x in [])
        del footer_copy

        if schema_pages:
            note = ""
            if dom_only_pages:
                note = f"；另有 {len(dom_only_pages)} 页有 FAQ 板块但无结构化标记：{'、'.join(x[0] for x in dom_only_pages[:4])}"
            best = max(schema_pages, key=lambda x: x[1])
            dom_labels = {x[0] for x in dom_only_pages}
            return self._result(item, Status.PASS,
                                f"有 FAQPage 结构化标记（{best[1] or '若干'}个问答，{best[0]}）" + note,
                                detail=note.lstrip("；") or None,
                                pages=[self.page.final_url] +
                                      [p.final_url for p in self.all_pages
                                       if (urlparse(p.final_url).path or "/") in dom_labels])
        if dom_only_pages:
            labels = "、".join(f"{l}（{q or '若干'}问）" for l, q, _ in dom_only_pages[:5])
            # 来源按优先级排序输出：专职FAQ页→问答多的页→其余，与明细一致
            path_by = {urlparse(p.final_url).path or "/": p.final_url for p in self.all_pages}
            # /faq/ 等专职 FAQ 路径排最前（与明细 is_faq_path 优先一致）
            dom_only_pages.sort(key=lambda x: (not bool(re.search(r"/(faq|cjwt|changjianwenti)", x[0], re.I)), -x[1]))
            pages_sorted = [path_by[x[0]] for x in dom_only_pages if x[0] in path_by]
            return self._result(item, Status.PASS,
                                f"有 FAQ 问答板块（{len(dom_only_pages)} 个页面，其中 {len([x for x in dom_only_pages if not x[2]])} 个建议加 FAQPage 标记——属可选优化，标记富媒体展示已停显）",
                                detail=f"涉及页面: {labels}。"
                                       + (dup_note + "。" if dup_note else "")
                                       + "加 FAQPage JSON-LD 后 AI 可稳定摘取问答"
                                       + "（FAQ 富媒体自2023-08起仅政府/健康权威站展示；标记的价值在 Bing 与语义层理解）",
                                pages=pages_sorted)
        return self._result(item, Status.WARN, "全站未检测到 FAQ 问答结构",
                            detail="用户问具体问题时 AI 没有现成答案可摘，建 FAQ 页是提升被引用率最直接的抓手",
                            pages=[p.final_url for p in self.all_pages])

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

        # 2026-09-10 V6 三态口径：留空(alt="")=装饰图标准做法，剔除分母不判错；
        # 算错=无alt属性/占位词/模板残留；写清说明=好。坏文件名为独立提示不进判级。
        _tpl_residue = re.compile(r"\{\{|\{\$|\{%|\{:|%\}|\$field|\$vo|\$[a-zA-Z_]+\.|eyou:|volist|think\.")
        _meaningless = {"图片", "图像", "image", "img", "photo", "pic", "照片"}

        def _state(img):
            a = img.get("alt")
            if a is None:
                return "err"
            a = str(a).strip()
            if not a:
                return "blank"
            if len(a) <= 2 or a.lower() in _meaningless or _tpl_residue.search(a):
                return "err"
            return "ok"

        scored = [i for i in images if _state(i) != "blank"]
        blank_n = len(images) - len(scored)
        err = [i for i in scored if _state(i) == "err"]
        good = len(scored) - len(err)

        import os
        bad_filename_patterns = [
            r'^\d+\.', r'^img\d+\.', r'^image\d+\.', r'^dsc\d+\.',
            r'^pic\d+\.', r'^photo\d+\.', r'^u\d+\.', r'^nophoto',
            r'^default\.', r'^placeholder\.', r'^blank\.',
        ]
        bad_filenames = 0
        for img in images:
            filename = os.path.basename(img.get("src", "")).lower()
            if filename and any(re.match(pat, filename) for pat in bad_filename_patterns):
                bad_filenames += 1

        detail_parts = []
        if blank_n:
            detail_parts.append(f"{blank_n} 张留空（装饰图标准做法；如为信息图请补说明）")
        if err:
            detail_parts.append(f"{len(err)} 张该写说明的图没写/写了无意义占位或模板残留")
        if bad_filenames:
            detail_parts.append(f"无意义文件名 {bad_filenames} 张（提示项，不判级）")
        detail = "；".join(detail_parts) or None

        if not scored:
            return self._result(item, Status.PASS,
                                f"{len(images)} 张图片全部留空 alt（装饰图合规做法，无该写说明的图）",
                                detail=detail)
        ratio = good / len(scored)
        base_msg = f"该写说明的图片 {good}/{len(scored)} 写清了说明" + (f"（另有 {blank_n} 张装饰图留空）" if blank_n else "")
        if ratio >= 0.8:
            return self._result(item, Status.PASS, base_msg, detail=detail)
        if ratio >= 0.5:
            return self._result(item, Status.WARN, f"{base_msg}，覆盖偏低", detail=detail)
        return self._result(item, Status.FAIL,
                            f"{base_msg}——过半图片对搜索引擎和AI'不存在'",
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
            tier="bonus",  # 2026-09-10 V6定版：两档同为不足说明非决定性，转只提醒
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
            "是否有 hreflang 或语言切换；各语言版本内容语言是否与定位一致",
            "检查 link rel=alternate hreflang 和语言切换按钮；对声称中文/英文的页面核对正文实际语言",
            "如有多语言，使用 hreflang 标注，且各语言版本内容语言与定位一致",
            manual_check="看页面是否有语言切换按钮；右键查看源代码搜索 hreflang；点开'中文版'页面确认正文真是中文而不是英文镜像",
            purpose="hreflang 告诉 AI 每个语言版本对应哪个地区的用户；若'中文版'页面正文实际是英文，中文用户和 AI 拿到的都是错语种内容，多语言形同虚设还互相竞争排名",
            excellent="有多语言站点，每个页面都有正确的 hreflang 标记（含 x-default），语言切换功能正常，各语言内容完整且语言一致",
            normal="有语言切换功能，但缺少 hreflang 标记，或 hreflang 标注不完整",
            failed="单语言站点（不适用），或有多语言但完全没有 hreflang 且语言切换混乱，或语言版本正文与定位语言不符",
            tier="bonus",
        )
        hreflang = self.local_soup.find_all("link", attrs={"rel": "alternate", "hreflang": True})
        lang_switch = self.local_soup.find(class_=re.compile(r"lang|language|i18n", re.I))
        # 2026-09-10 V6：很多站的切换器是普通链接（如 <a href='/cn/…'>简体中文</a>），无特征类名——
        # 可见文本命中语言名称也算切换器
        if lang_switch is None:
            for a in self.local_soup.find_all("a"):
                if a.get_text(strip=True) in ("简体中文", "繁體中文", "中文", "English", "EN"):
                    lang_switch = a
                    break

        # ── 语言一致性（D2 决议口径）──
        # 第一步：识别语言切换器（语言菜单 / /cn//en/ 路径 / hreflang 声明）
        def _claimed_lang(p):
            path = (urlparse(p.final_url).path or "").lower()
            m = re.search(r"/(cn|zh|en)(?:[-/]|$)", path)
            if m:
                return "zh" if m.group(1) in ("cn", "zh") else "en"
            html_tag = p.soup.find("html")
            html_lang = (html_tag.get("lang") or "").lower() if html_tag else ""
            if html_lang.startswith("zh"):
                return "zh"
            if html_lang.startswith("en"):
                return "en"
            return ""

        # 第二步：统计实际存在的语言版本内容（同一语言 ≥1 个页面才算一个版本）
        lang_versions = {}
        for p in self.all_pages:
            lang_versions.setdefault(_claimed_lang(p) or "other", []).append(p)
        content_versions = {k for k, v in lang_versions.items() if k in ("zh", "en") and v}
        has_switcher = bool(lang_switch) or bool(hreflang) or len(content_versions) >= 2

        # 第三步：无切换器且只有单语言版本 = 单语言站，尊重母语设置，镜像检查跳过、不扣分
        if not has_switcher and len(content_versions) < 2:
            return self._result(item, Status.SKIP,
                                "站点为单语言，多语言/多地区检查不适用",
                                detail=f"未检测到语言切换器与多语言版本（识别语言: {', '.join(sorted(content_versions)) or '无'}）")

        # 第四步：有切换器才逐版本查镜像——中文页与英文页正文相似度 ≥70% = 半成品镜像（高优先级）
        def _sim(a, b):
            # a/b 为 PageData，get_text 无分隔符参数
            ta, tb = a.get_text(), b.get_text()
            A = {ta[i:i + 2] for i in range(len(ta) - 1)}
            B = {tb[i:i + 2] for i in range(len(tb) - 1)}
            if not A or not B:
                return 0.0
            return len(A & B) / len(A | B)

        mirrors = []
        for zp in lang_versions.get("zh", []):
            best = max((_sim(zp, ep) for ep in lang_versions.get("en", [])), default=0.0)
            if best >= 0.70:
                mirrors.append((urlparse(zp.final_url).path or "/", int(best * 100)))

        # 导航/按钮未翻译（声称中文页面上的纯英文菜单项），仅作明细提示
        menu_untranslated = {}
        for zp in lang_versions.get("zh", []):
            en_menu = []
            for a in zp.soup.find_all("a", href=True):
                t = a.get_text(" ", strip=True)
                if 3 <= len(t) <= 30 and not re.search(r"[\u4e00-\u9fff]", t) \
                        and re.search(r"[A-Za-z]{3,}", t) and not re.match(r"^(EN|PDF)$", t.upper()):
                    en_menu.append(t)
            if len(en_menu) >= 4:
                menu_untranslated[urlparse(zp.final_url).path or "/"] = en_menu

        # 镜像判定：中文页与英文页正文相似度 ≥70% 即"半成品镜像"（高优先级）
        zh_total = len(lang_versions.get("zh", []))
        mirror_pages = [p for p in lang_versions.get("zh", [])
                        if (urlparse(p.final_url).path or "/") in {m[0] for m in mirrors}]
        menu_note = "；".join(f"{path} 存在未翻译的英文导航/按钮（如 {'、'.join(items[:3])}）"
                          for path, items in list(menu_untranslated.items())[:3])
        mirror_note = ""
        if mirrors:
            mirror_note = "镜像页面: " + "、".join(f"{path}（与英文版相似 {sim}%）" for path, sim in mirrors[:6])
        hl_note = "" if hreflang else "缺少 hreflang 标记（各语言版本未互指，会互相竞争排名）"

        if mirrors:
            detail = "；".join(x for x in (mirror_note, hl_note, menu_note) if x)
            return self._result(item, Status.FAIL,
                                f"{len(mirrors)}/{zh_total} 个中文版页面实为英文镜像，多语言配置仅完成一半",
                                detail=detail,
                                pages=[p.final_url for p in mirror_pages])
        # 无镜像：按 hreflang/切换器情况给结论
        if hreflang:
            langs = [l.get("hreflang") for l in hreflang if l.get("hreflang")]
            msg = f"有 hreflang 标记（{len(set(langs))}种语言），中英版本内容独立，未发现镜像"
            return self._result(item, Status.PASS, msg + ("；" + menu_note if menu_note else ""),
                                detail=f"语言: {', '.join(set(langs))}" + (f"。{hl_note}" if hl_note else ""),
                                pages=[p.final_url for p in self.all_pages])
        if lang_switch:
            msg = "有语言切换功能，各语言版本内容独立，未发现镜像"
            return self._result(item, Status.WARN, msg + "，但缺少 hreflang 标记" + ("；" + menu_note if menu_note else ""),
                                detail=(hl_note or "") + (f"。{menu_note}" if menu_note else "") or None,
                                pages=[p.final_url for p in self.all_pages])
        # 有 ≥2 语言版本内容但无切换器/hreflang（少见）：提示配置
        return self._result(item, Status.WARN, "检测到多语言版本内容，但缺少语言切换器与 hreflang 标记",
                            detail=(hl_note or "建议配置语言切换与 hreflang") + (f"。{menu_note}" if menu_note else ""),
                            pages=[p.final_url for p in self.all_pages])
