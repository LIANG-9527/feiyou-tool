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
        # 全行业获客硬检查（纯规则、不依赖 AI）：能不能联系上、有没有行动号召、决策信息全不全
        results.append(self._check_contact_channels())
        results.append(self._check_cta())
        results.append(self._check_decision_info())
        return results

    # 全站文本/链接汇总（首页+抽样内页），供获客类检查使用
    def _all_text(self) -> str:
        parts = [p.get_text() for p in self.all_pages if p is not None]
        return "\n".join(parts)

    def _all_soups(self):
        return [p.soup for p in self.all_pages if p is not None and p.soup is not None]

    @staticmethod
    def _is_dead_href(href) -> bool:
        h = (href or "").strip().lower()
        return (not h) or h == "#" or h.startswith("#") or h.startswith("javascript:")

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
            tier="bonus",  # 2026-09-10 V6定版：转化引导项，转只提醒
        )
        from urllib.parse import urlparse
        base_domain = urlparse(self.page.final_url).netloc

        product_keywords = ["产品", "解决方案", "product", "solution", "功能", "features"]
        product_links = []
        for a in self.local_soup.find_all("a", href=True):
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
            tier="bonus",  # 2026-09-10 V6定版：发展阶段问题非故障，转只提醒
        )
        case_keywords = ["案例", "客户", "成功故事", "case", "customer", "story", "客户案例"]
        case_links = []
        for a in self.local_soup.find_all("a", href=True):
            text = a.text.strip()
            if any(kw in text.lower() for kw in case_keywords):
                if not a["href"].startswith(("#", "javascript:", "mailto:")):
                    case_links.append(text)

        customer_logos = self.local_soup.find_all(class_=re.compile(r"customer|client|logo|partner", re.I))

        if case_links:
            return self._result(item, Status.PASS,
                                f"有客户案例入口（{len(set(case_links))}个）",
                                detail=f"包含: {', '.join(list(set(case_links))[:3])}")
        if customer_logos:
            return self._result(item, Status.WARN, "有客户 Logo 但缺少案例详情页入口")
        return self._result(item, Status.WARN, "未检测到客户案例或客户展示")  # 2026-09-10 V6：标红降为不足

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
            tier="bonus",  # 2026-09-10 V6定版：垂直单行业站天然为0，转只提醒
        )
        industry_keywords = ["行业", "场景", "零售", "金融", "教育", "医疗", "制造",
                            "电商", "政务", "企业", "industry", "retail", "finance"]
        industry_links = []
        for a in self.local_soup.find_all("a", href=True):
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
            tier="bonus",
        )
        comparison_keywords = ["对比", "哪个好", "选型", "区别", "vs", "versus", "comparison", "比较", "选购", "difference"]
        found = []

        for a in self.local_soup.find_all("a", href=True):
            text = a.text.strip()
            if any(kw in text.lower() for kw in comparison_keywords):
                found.append(text)

        page_text = self.get_text()
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
            tier="bonus",
        )
        if not self.ai_engine:
            return self._result(item, Status.SKIP, "未检查（需配置AI语义引擎）",
                                detail="未配置LLM，无法进行技术文档分析")
        status, summary, detail = self.ai_engine.consensus(lambda: self.ai_engine.check_tech_docs(self.page, self.all_pages))
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
            # 规则兜底：统计站内可见的问答条目，不因未配置 AI 就跳过抬分
            n = 0
            samples = []
            for soup in self._all_soups():
                n += len(soup.find_all("summary"))
                dts = [d for d in soup.find_all("dt") if re.search(r"[?？]", d.get_text())]
                n += len(dts)
                n += len(soup.find_all(class_=re.compile(r"faq|question|qa-|ask", re.I)))
                for tag in soup.find_all(string=re.compile(r"[?？]")):
                    s = tag.strip()
                    if 6 <= len(s) <= 40 and len(samples) < 5:
                        samples.append(s)
            n = max(n, len(samples))
            if n >= 8:
                return self._result(item, Status.PASS, f"规则识别到约 {n} 条问答内容（未启用AI语义细评）")
            if n >= 3:
                return self._result(item, Status.WARN, f"问答内容偏少（规则识别约 {n} 条，建议≥10 个高频问题）")
            return self._result(item, Status.WARN, "几乎没有问答/FAQ 内容，客户和 AI 都找不到现成答案",
                                detail="规则未识别到成规模的问答条目，建议就价格、流程、售后等高频问题建立 FAQ")
        status, summary, detail = self.ai_engine.consensus(lambda: self.ai_engine.check_qa_content(self.page, self.all_pages))
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
            tier="bonus",
        )
        if not self.ai_engine:
            return self._result(item, Status.SKIP, "未检查（需配置AI语义引擎）",
                                detail="未配置LLM，无法进行转化路径分析")
        status, summary, detail = self.ai_engine.consensus(lambda: self.ai_engine.check_conversion_path(self.page))
        return self._result(item, status, summary, detail=detail)

    # b2b-08 联系方式触达（全行业获客底线：能不能找到你）
    def _check_contact_channels(self) -> CheckResult:
        item = self._item(
            "b2b-08", "联系方式可触达", Priority.P0,
            "访客在站内能否方便地找到至少一种真实可用的联系/咨询途径",
            "在首页+内页中识别电话、邮箱、微信/二维码、留言表单、在线客服等通道（排除空链接）",
            "提供≥2种有效联系通道，且在页眉/页脚等固定位置可见",
            purpose="无论卖车、卖货还是卖服务，转化第一步都是'能联系上'。联系方式难找或单一，高意向访客会直接流失，AI 也无法在答案里给出可信的联系路径",
            manual_check="以陌生访客身份浏览首页和产品页，看 10 秒内能否找到电话/微信/表单/在线客服，点一下确认不是空链接",
            excellent="≥2 种有效通道（如电话+微信、表单+在线客服），页眉页脚固定可见",
            normal="只有 1 种联系通道，或位置隐蔽",
            failed="全站找不到任何有效联系方式，获客链路断开",
            tier="bonus",  # 2026-09-10 V6定版：只用留资表单的合规站不该被标红，转只提醒（最高"不足"）
            scope="全站", applicable_pages="全站",
            suggestion="在页脚固定电话/微信/二维码，关键页面放咨询表单或在线客服，且确保链接真实可点",
        )
        text = self._all_text()
        soups = self._all_soups()
        channels = {}
        if re.search(r"400[-\s]?\d{3}[-\s]?\d{4}|1[3-9]\d{9}|0\d{2,3}[-\s]?\d{7,8}", text):
            channels["电话"] = True
        if re.search(r"[A-Za-z0-9_.+-]+@[A-Za-z0-9-]+\.[A-Za-z]{2,}", text):
            channels["邮箱"] = True
        if re.search(r"微信|加微|企微|wechat|weixin", text, re.I):
            channels["微信/企微"] = True
        for soup in soups:
            for img in soup.find_all("img"):
                blob = f"{img.get('alt','')}{img.get('class','')}{img.get('src','')}".lower()
                if re.search(r"qrcode|erweima|weixin|wechat|二维码", blob):
                    channels["二维码"] = True
        if any(soup.find("form") for soup in soups) or re.search(r"留言|留下.{0,6}(电话|联系方式)|提交.{0,4}(需求|信息)|leave a message|send (?:us )?a message|inquiry|message us", text, re.I):
            channels["留言/表单"] = True
        if re.search(r"在线咨询|在线客服|即时沟通|商务通|商桥|53kf|kf5|智齿|美洽|live ?chat|intercom|zendesk|tawk|crisp|chat ?with (us|an expert)", text, re.I):
            channels["在线客服"] = True
        k = len(channels)
        if k >= 2:
            return self._result(item, Status.PASS, f"检测到 {k} 种联系通道：{'、'.join(channels)}",
                                pages=[p.final_url for p in self.all_pages])
        if k == 1:
            return self._result(item, Status.WARN, f"仅检测到 1 种联系通道（{'、'.join(channels)}），建议再补一种",
                                detail="通道单一会漏掉不习惯该方式的客户，也缺少备用路径", pages=[p.final_url for p in self.all_pages])
        return self._result(item, Status.WARN, "全站未识别到有效联系方式（电话/微信/邮箱/表单/在线客服均无）",
                            pages=[p.final_url for p in self.all_pages])  # 2026-09-10 V6：标红降为不足

    # b2b-09 行动号召 CTA（全行业：下一步按钮是否清晰可用）
    def _check_cta(self) -> CheckResult:
        item = self._item(
            "b2b-09", "行动号召(CTA)清晰可用", Priority.P1,
            "页面是否有明确的下一步按钮（咨询/预约/购买/到店/领取等），且不是空链接",
            "识别全站 a/button 中的行动号召文案，区分有效链接与 #/javascript 空壳",
            "关键页面有≥3个有效行动入口，文案明确、链接可点",
            purpose="访客产生兴趣后需要一个明确的'下一步'。没有行动号召或按钮点不动，意向就在页面上流失；不同行业只是叫法不同（预约试驾/立即购买/免费咨询）",
            manual_check="检查首屏和产品页是否有醒目的咨询/购买/预约按钮，逐个点击确认能跳到表单、对话或下单，而不是'#'",
            excellent="首屏与关键页都有明确且有效的行动按钮，话术贴合业务",
            normal="有行动按钮但数量少或位置不突出",
            failed="全站没有可点的行动号召，或按钮都是空链接",
            scope="全站", applicable_pages="全站",
            suggestion="在首屏、产品/服务、案例页各放一个明确按钮（如免费咨询/预约到店/立即购买），链接指向真实表单或联系方式",
            tier="bonus",  # 2026-09-10 V6定版：引导项，转只提醒
        )
        cta_re = re.compile(
            r"咨询|预约|报名|领取|试用|报价|下单|购买|抢购|到店|联系(我们|客服)|拨打|获取.{0,4}(方案|报价)|"
            r"免费.{0,3}(检测|评估|试用|咨询)|get started|buy now|contact us|subscribe|order", re.I)
        valid, dead = set(), set()
        for soup in self._all_soups():
            for a in soup.find_all("a"):
                t = re.sub(r"\s+", "", a.get_text())
                if t and cta_re.search(t):
                    (dead if self._is_dead_href(a.get("href")) else valid).add(t[:12])
            for b in soup.find_all("button"):
                t = re.sub(r"\s+", "", b.get_text())
                if t and cta_re.search(t):
                    valid.add(t[:12])
        if len(valid) >= 3:
            note = f"（其中 {len(dead)} 个是空链接）" if dead else ""
            return self._result(item, Status.PASS, f"检测到 {len(valid)} 个有效行动入口{note}",
                                detail=("空壳按钮：" + "、".join(list(dead)[:5])) if dead else None, pages=[p.final_url for p in self.all_pages])
        if valid:
            return self._result(item, Status.WARN, f"有效行动入口偏少（{len(valid)} 个），空链接 {len(dead)} 个",
                                detail="有效：" + "、".join(list(valid)[:5]), pages=[p.final_url for p in self.all_pages])
        if dead:
            return self._result(item, Status.WARN, "行动按钮都是 #/javascript 空链接，点了没反应",
                                detail="空壳：" + "、".join(list(dead)[:5]), pages=[p.final_url for p in self.all_pages])
        return self._result(item, Status.WARN, "全站缺少明确的行动号召按钮", pages=[p.final_url for p in self.all_pages])

    # b2b-10 决策信息完整度（全行业：下决心前要知道的事）
    def _check_decision_info(self) -> CheckResult:
        item = self._item(
            "b2b-10", "决策信息完整度", Priority.P1,
            "是否讲清客户下决心前最关心的事：价格/费用、怎么买或合作、流程、售后保障、覆盖或配送",
            "在全站文本中识别五类决策信息的命中情况",
            "五类决策信息至少讲清四类",
            purpose="客户（以及替客户总结的 AI）在转化前需要回答'多少钱、怎么买、什么流程、出问题谁负责、能不能到我这'。这些信息缺失，意向会在犹豫中流失，AI 也无法替你给出完整答案",
            manual_check="站在初次了解的客户角度，检查能否在站内找到价格或获取报价方式、购买/合作流程、服务流程、售后承诺、服务范围/配送或门店",
            excellent="价格/购买方式/流程/售后/覆盖五类信息齐全且易找",
            normal="讲清 2-3 类，仍有客户高频疑问没回答",
            failed="决策信息严重缺失（0-1 类），客户无从下决心",
            tier="bonus",  # 2026-09-10 V6定版：报价制企业不公开价格是销售策略，转只提醒
            scope="全站", applicable_pages="全站",
            suggestion="补齐价格或获取报价入口、购买/合作流程、使用或服务流程、售后承诺、服务范围/配送/门店等信息",
        )
        text = self._all_text()
        # 中英双语词根：英文站（外贸/出海企业）的决策信息用英文表述，纯中文词会误报 0/5
        groups = {
            "价格/费用": r"价格|费用|收费|多少钱|报价|价目|套餐"
                     r"|price|pricing|cost|quotation|request a quote|get a quote",
            "怎么买/怎么合作": r"怎么买|如何购买|购买流程|下单|订购|合作流程|怎么合作|办理方式|咨询后"
                          r"|how to (buy|order|cooperat\w*|partner|become)|purchase|partnership"
                          r"|distributor|dealer|reseller|oem|odm|private label",
            "服务/使用流程": r"服务流程|使用流程|办理流程|流程步骤|第[一二三四五]步|几步骤|服务步骤"
                        r"|(?:work|service|production|ordering) process|how it works|step[- ]by[- ]step|workflow",
            "售后/保障": r"售后|保修|质保|退换|包退|服务承诺|服务保障|无忧"
                     r"|after-?sales|warranty|guarantee|refund|return policy|quality assurance",
            "覆盖/配送/地点": r"配送|发货|包邮|服务范围|覆盖|上门|全国|门店|到店|线下|所在城市"
                         r"|shipping|delivery|worldwide|global|coverage|countries|locat\w+|factory visit",
        }
        hit, lack = [], []
        for name, pat in groups.items():
            (hit if re.search(pat, text, re.I) else lack).append(name)
        if len(hit) >= 4:
            return self._result(item, Status.PASS, f"决策信息较完整（{len(hit)}/5）：{'、'.join(hit)}",
                                detail=("可补充：" + "、".join(lack)) if lack else None, pages=[p.final_url for p in self.all_pages])
        if len(hit) >= 2:
            return self._result(item, Status.WARN, f"决策信息不全（{len(hit)}/5），缺：{'、'.join(lack)}", pages=[p.final_url for p in self.all_pages])
        return self._result(item, Status.WARN, f"决策信息缺失（仅 {len(hit)}/5），缺：{'、'.join(lack)}", pages=[p.final_url for p in self.all_pages])  # 2026-09-10 V6：转只提醒
