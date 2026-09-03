"""权威与信任信号检查（6项，其中2项待实现）"""
import re
from typing import List
from .checker import BaseChecker
from site_audit.core.models import CheckResult, Status, Priority, Category


class AuthorityChecker(BaseChecker):
    category = Category.AUTHORITY

    def run(self) -> List[CheckResult]:
        results = []
        results.append(self._check_trust_signals())
        results.append(self._check_customer_testimonials())
        results.append(self._check_social_presence())
        results.append(self._check_external_links())
        results.append(self._check_knowledge_graph())
        results.append(self._check_cross_platform())
        results.append(self._check_eeat())
        return results

    # 29. 可信与安全信号
    def _check_trust_signals(self) -> CheckResult:
        item = self._item(
            "auth-01", "可信与安全信号", Priority.P0,
            "是否有 ICP 备案、联系方式、隐私政策",
            "检查页脚是否有备案号、联系方式、隐私政策链接",
            "有ICP备案，真实联系方式，隐私政策页",
            manual_check="拉到页面底部，看是否有 ICP 备案号、公司地址、电话/邮箱、隐私政策链接、关于我们页面",
            purpose="AI 判断网站可信度时会看这些基础信号。没有备案和联系方式的站点会被 AI 视为低可信度，不会优先推荐",
            excellent="有 ICP 备案号、完整公司信息（地址/电话/邮箱）、隐私政策、服务条款、关于我们页面，信息真实可验证",
            normal="有 ICP 备案和基本联系方式，但可能缺少隐私政策或公司信息不完整",
            failed="缺少 ICP 备案、联系方式、隐私政策中的2项以上，可信度严重不足",
        )
        page_text = self.page.get_text()
        footer = self.page.soup.find("footer") or self.page.soup.find(class_=re.compile(r"footer", re.I))
        footer_text = footer.get_text() if footer else page_text

        has_icp = bool(re.search(r"ICP备|ICP备案|京ICP|沪ICP|粤ICP|陕ICP", footer_text))
        has_contact = bool(re.search(r"电话|手机|邮箱|联系我们|contact|400-?\d{3}-?\d{4}", footer_text, re.I))
        has_privacy = bool(self.page.soup.find("a", href=re.compile(r"privacy|隐私", re.I)))

        signals = []
        if has_icp:
            signals.append("ICP备案")
        if has_contact:
            signals.append("联系方式")
        if has_privacy:
            signals.append("隐私政策")

        if len(signals) >= 2:
            return self._result(item, Status.PASS, f"可信信号完善: {', '.join(signals)}")
        if signals:
            return self._result(item, Status.WARN,
                                f"可信信号不足，仅有: {', '.join(signals)}")
        return self._result(item, Status.FAIL, "缺少 ICP 备案、联系方式、隐私政策等可信信号")

    # 30. 客户评价与证言
    def _check_customer_testimonials(self) -> CheckResult:
        item = self._item(
            "auth-02", "客户评价与证言", Priority.P1,
            "是否有客户 Logo、评价、案例",
            "检查客户 Logo 区域和评价板块",
            "有真实客户 Logo + 案例详情",
            manual_check="浏览页面，看是否有客户 Logo 墙、客户评价/证言、客户案例详情（含客户名称、行业、成果数据）",
            purpose="客户评价和案例是 AI 判断品牌权威性的重要社会证明。有真实客户背书的品牌更容易被 AI 推荐为可靠选择",
            excellent="有客户 Logo 墙（≥10个知名客户）+ 详细客户案例（含背景/挑战/方案/成果数据）+ 客户证言，信息真实可验证",
            normal="有客户 Logo 展示，但缺少详细案例或评价；或有案例但客户知名度不高",
            failed="未检测到任何客户 Logo、评价或案例展示",
        )
        testimonial_classes = ["testimonial", "review", "评价", "证言", "客户说", "customer-say"]
        has_testimonial = bool(self.page.soup.find(class_=re.compile(
            "|".join(testimonial_classes), re.I)))
        customer_logos = self.page.soup.find_all(class_=re.compile(r"customer|client|partner|logo-wall", re.I))

        if has_testimonial and customer_logos:
            return self._result(item, Status.PASS, "有客户评价和客户 Logo 展示")
        if has_testimonial:
            return self._result(item, Status.PASS, "有客户评价板块")
        if customer_logos:
            return self._result(item, Status.WARN, "有客户 Logo 但缺少评价/证言")
        return self._result(item, Status.WARN, "未检测到客户评价或客户 Logo 展示")

    # 31. 社交平台存在感
    def _check_social_presence(self) -> CheckResult:
        item = self._item(
            "auth-03", "社交平台存在感", Priority.P2,
            "是否有社交媒体链接",
            "检查页脚社交图标链接",
            "至少2个平台有官方账号",
            manual_check="拉到页面底部，看是否有微信、微博、知乎、领英、抖音等社交媒体的官方账号链接或二维码",
            purpose="社交媒体账号是品牌实体的延伸信号。AI 会综合社交平台上的品牌讨论度和官方存在来判断品牌权威性",
            excellent="有 ≥3 个主流社交平台的官方认证账号（微信公众号+微博+知乎/领英），账号活跃，内容与官网联动",
            normal="有 1-2 个社交平台链接，但可能账号不活跃或平台单一",
            failed="未检测到任何社交媒体链接或二维码",
        )
        social_platforms = {
            "微信": ["weixin", "wechat", "微信"],
            "微博": ["weibo", "微博"],
            "知乎": ["zhihu", "知乎"],
            "领英": ["linkedin", "领英"],
            "抖音": ["douyin", "抖音"],
            "B站": ["bilibili", "b站", "哔哩哔哩"],
            "GitHub": ["github"],
        }
        found = []
        for a in self.page.soup.find_all("a", href=True):
            href = a["href"].lower()
            text = a.text.strip().lower()
            for platform, keywords in social_platforms.items():
                if any(kw in href or kw in text for kw in keywords):
                    if platform not in found:
                        found.append(platform)

        if len(found) >= 2:
            return self._result(item, Status.PASS, f"社交平台链接: {', '.join(found)}")
        if found:
            return self._result(item, Status.WARN, f"社交平台较少: {', '.join(found)}")
        return self._result(item, Status.WARN, "未检测到社交媒体链接")

    # 28. 第三方引用与外链
    def _check_external_links(self) -> CheckResult:
        item = self._item(
            "auth-04", "外部链接质量", Priority.P1,
            "是否有指向权威网站的外链",
            "统计外部链接数量和域名",
            "有≥3个独立第三方域名的正向引用",
            manual_check="浏览页面，看是否有指向行业权威网站、媒体报道、合作伙伴官网的链接，而不是只链向自己的页面",
            purpose="链向权威网站能提升 AI 对你网站的可信度评估。同时，被权威网站引用（反向链接）是品牌权威性的核心指标",
            excellent="有 ≥3 个指向行业权威网站/媒体/合作伙伴的高质量外链，且自身有被权威网站引用的记录",
            normal="有 1-2 个外部链接，或外链质量一般（如链向普通博客）",
            failed="没有任何指向第三方权威网站的外链，或所有链接都是站内链接",
        )
        from urllib.parse import urlparse
        base_domain = urlparse(self.page.final_url).netloc
        external_domains = set()

        for a in self.page.soup.find_all("a", href=True):
            href = a["href"]
            if href.startswith(("http://", "https://")):
                domain = urlparse(href).netloc
                if domain and domain != base_domain and "www." + domain != base_domain:
                    if not any(s in domain for s in ["weibo", "weixin", "zhihu",
                                                      "linkedin", "github", "twitter",
                                                      "facebook", "youtube"]):
                        external_domains.add(domain)

        if len(external_domains) >= 3:
            return self._result(item, Status.PASS,
                                f"有 {len(external_domains)} 个权威外链")
        if external_domains:
            return self._result(item, Status.WARN,
                                f"外链较少（{len(external_domains)}个）",
                                detail=f"域名: {', '.join(list(external_domains)[:3])}")
        return self._result(item, Status.WARN, "未检测到指向第三方权威网站的外链")

    # 26. 知识图谱品牌实体
    def _check_knowledge_graph(self) -> CheckResult:
        item = self._item(
            "auth-05", "知识图谱品牌实体", Priority.P1,
            "品牌是否在百度百科/维基百科/知识面板中有实体条目",
            "搜索品牌名，检查是否有百科词条或知识卡片",
            "至少在1个主流知识平台有品牌词条，信息与官网一致",
            manual_check="在百度/谷歌搜索品牌名，看右侧是否有知识面板；搜索'品牌名 百科'看是否有百科词条",
            purpose="知识图谱实体是 AI 判断品牌权威性的核心信号。有百科词条的品牌会被 AI 视为已知实体，推荐概率大幅提升",
            excellent="百度百科+维基百科+谷歌知识面板均有品牌实体，信息完整准确，与官网一致，定期更新",
            normal="至少在百度百科或维基百科有1个品牌词条，基本信息准确，但可能不够完整",
            failed="无任何百科词条或知识面板，品牌在 AI 知识图谱中不存在",
        )
        if not self.ai_engine:
            return self._result(item, Status.SKIP, "未检查（需配置AI语义引擎）",
                                detail="未配置LLM，无法进行知识图谱检查")
        status, summary, detail = self.ai_engine.check_knowledge_graph(self.page)
        return self._result(item, status, summary, detail=detail)

    # 31. 跨平台内容分发
    def _check_cross_platform(self) -> CheckResult:
        item = self._item(
            "auth-06", "跨平台内容分发", Priority.P2,
            "官网内容是否同步分发到知乎/公众号/行业媒体等平台",
            "检查是否有跨平台内容及外链回官网",
            "核心内容有跨平台分发，外链回官网",
            manual_check="在知乎、微信公众号、行业媒体搜索品牌名或文章标题，看是否有同步发布的内容，且是否链接回官网",
            purpose="跨平台分发能扩大品牌在 AI 训练数据中的覆盖面，多平台一致的内容信号会强化 AI 对品牌权威性的判断",
            excellent="核心内容同步到≥3个平台（知乎+公众号+行业媒体），每篇都有外链回官网，平台账号与官网品牌一致",
            normal="有1-2个平台的内容分发，但部分内容无外链回官网，或更新不规律",
            failed="无任何跨平台内容分发，内容仅存在于官网",
        )
        if not self.ai_engine:
            return self._result(item, Status.SKIP, "未检查（需配置AI语义引擎）",
                                detail="未配置LLM，无法进行跨平台检查")
        status, summary, detail = self.ai_engine.check_cross_platform(self.page)
        return self._result(item, status, summary, detail=detail)

    # AUTH-07 E-E-A-T 内容可信度信号
    def _check_eeat(self) -> CheckResult:
        item = self._item(
            "auth-07", "E-E-A-T 内容可信度信号", Priority.P1,
            "内容是否体现经验(Experience)、专业(Expertise)、权威(Authoritativeness)、可信(Trustworthiness)",
            "AI：识别作者署名与资质、第一手经验描述、数据来源、编辑审核信息（内容页检查）",
            "有作者+资质、第一手经验、来源可查、审核机制，完整体现E-E-A-T",
            manual_check="在文章/案例/产品详情页检查：是否标注作者姓名及资质头衔；是否有'我们亲历/实测/服务过XX'等第一手经验；关键数据是否标注来源；是否有编辑或审核信息",
            purpose="E-E-A-T是AI判断内容是否值得引用的核心依据。AI倾向引用有明确作者、专业资质、第一手经验、来源可查的内容，匿名通用文案可信度最低。首页是品牌导航页，E-E-A-T评估对象是内容页",
            excellent="内容页明确标注作者姓名+专业资质/头衔，有第一手经验或实测描述，关键数据标注来源，有编辑审核与更新机制，完整体现E-E-A-T",
            normal="有作者署名但无资质说明，或部分体现经验但不系统，数据来源标注不全",
            failed="无作者信息、无经验描述、无来源标注，内容像匿名通用文案，无法建立信任",
        )
        if not self.ai_engine:
            return self._result(item, Status.SKIP, "未检查（需配置AI语义引擎）",
                                detail="未配置LLM，无法进行E-E-A-T分析")

        # E-E-A-T 是内容页检查项，首页是品牌导航页不适用
        # 优先在抽样的内容页（文章/案例/产品页）上检查
        content_page = self._pick_content_page()
        if not content_page:
            return self._result(item, Status.SKIP,
                                "E-E-A-T为内容页检查项，首页不适用，且未抽样到内容页",
                                detail="E-E-A-T评估对象是文章/案例/产品详情页等有主体内容的页面，首页是品牌导航页不适用")
        status, summary, detail = self.ai_engine.check_eeat(content_page)
        page_label = content_page.soup.title.get_text(strip=True)[:30] if content_page.soup.title else content_page.final_url
        return self._result(item, status, summary,
                            detail=f"检查页面: {page_label}。{detail}")
