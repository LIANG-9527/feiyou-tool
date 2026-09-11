"""权威与信任信号检查（6项，其中2项待实现）"""
import re
import json
import time
from typing import List
from urllib.parse import urlparse, urljoin
from .checker import BaseChecker
from site_audit.core.models import CheckResult, Status, Priority, Category
from site_audit.core.crawler import SiteCrawler

# 被视作"组织/公司实体"的隐形名片类型
_ORG_TYPES = ("Organization", "Company", "Corporation", "LocalBusiness",
              "ProfessionalService", "Enterprise")


def _iter_ld_nodes(node):
    """递归展开 JSON-LD：兼容 dict / list / @graph。"""
    if isinstance(node, list):
        for n in node:
            yield from _iter_ld_nodes(n)
    elif isinstance(node, dict):
        if "@graph" in node:
            yield from _iter_ld_nodes(node["@graph"])
        yield node


def _first_number(text):
    m = re.search(r"\d+", str(text or ""))
    return int(m.group()) if m else None


def _norm_tel(t):
    """电话归一：只留数字、去国家码 86。"""
    d = re.sub(r"\D", "", str(t or ""))
    if d.startswith("86") and len(d) > 11:
        d = d[2:]
    return d


def _norm_host(u):
    """官网写法归一：去 scheme/www，仅留 host，用于判断是不是同一个站。"""
    try:
        host = urlparse(str(u)).netloc.lower()
        return host[4:] if host.startswith("www.") else host
    except Exception:
        return ""


def _readable_name(s) -> bool:
    """名称可读性校验：正常公司名/品牌以中文、英文、数字和常见符号为主；
    若混入大量西里尔字母等异常字符（多为页面编码错乱产生的乱码）则丢弃。"""
    s = str(s or "").strip()
    if not s:
        return False
    allowed = re.findall(r"[一-龥A-Za-z0-9（）()·\-&.,、\s]", s)
    return len(allowed) >= 0.8 * len(s)


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
        results.append(self._check_entity_consistency())
        return results

    # 29. 可信与安全信号
    def _check_trust_signals(self) -> CheckResult:
        item = self._item(
            "auth-01", "可信与安全信号", Priority.P0,
            "是否有 ICP 备案、联系方式、隐私政策",
            "检查页脚是否有备案号、联系方式、隐私政策链接，并校验备案号位数是否为工信部规范的 8 位",
            "有ICP备案，真实联系方式，隐私政策页",
            manual_check="拉到页面底部，看是否有 ICP 备案号、公司地址、电话/邮箱、隐私政策链接、关于我们页面",
            purpose="AI 判断网站可信度时会看这些基础信号。没有备案和联系方式的站点会被 AI 视为低可信度，不会优先推荐",
            excellent="有 ICP 备案号、完整公司信息（地址/电话/邮箱）、隐私政策、服务条款、关于我们页面，信息真实可验证",
            normal="有 ICP 备案和基本联系方式，但可能缺少隐私政策或公司信息不完整",
            failed="缺少 ICP 备案、联系方式、隐私政策中的2项以上，可信度严重不足",
        )
        page_text = self.get_text()
        soup = self.local_soup
        footer = soup.find("footer") or soup.find(class_=re.compile(r"footer", re.I))
        footer_text = footer.get_text() if footer else page_text
        raw_html = self.page.html

        # ICP 备案号可能由 JS 动态渲染，直接在原始 HTML 中搜索
        # 格式：[省份简称]ICP备XXXXXXXX号[-数字]，不限定省份，兜底所有变体
        has_icp = bool(re.search(r"ICP备\d+号", raw_html))
        # 备案号为省简称+ICP备+数字+可选-序号。
        # 2026-09-11 修正：工信部号码已扩展——老站 8 位（如 11044640），新站 10 位（如 2025459413），
        # 原「必须 8 位」会把新号误判为异常，故放宽为 8~10 位。
        icp_nums = re.findall(r"[京津沪渝冀晋辽吉黑苏浙皖闽赣鲁豫鄂湘粤桂琼川贵云陕甘青蒙宁新藏]ICP备(\d+)号(?:-\d+)?", raw_html)
        bad_icp = [n for n in icp_nums if len(n) not in (8, 10)]

        # 版权年份过期只作提示、不扣分：AI 评估企业活跃度时会往"可能停运"方向推一小步
        current_year = time.localtime().tm_year
        cp_years = [int(e or s) for s, e in
                    re.findall(r"(?:©|&copy;|Copyright\s*©?)\s*(\d{4})(?:\s*[-–—~]\s*(\d{4}))?", page_text, re.I)]
        stale_cp = max(cp_years) if cp_years else None
        copyright_note = (f"页脚版权年份停留在 {stale_cp}（当前 {current_year}），"
                          "AI 评估企业活跃度时可能视为停运信号，建议更新" if stale_cp and stale_cp < current_year else None)
        has_contact = bool(re.search(r"电话|手机|邮箱|联系我们|contact|400-?\d{3}-?\d{4}", footer_text, re.I))

        # 隐私政策/服务协议：必须是真实可达页面，'#'空锚点、javascript:、打不开(404)都不算
        priv_links = soup.find_all("a", href=re.compile(
            r"privacy|yinsi|隐私|agreement|xieyi|协议|terms|zhengce|政策", re.I))
        priv_ok, priv_empty, priv_dead = False, 0, []
        for a in priv_links[:5]:
            href = (a.get("href") or "").strip()
            if (not href or href == "#" or href.startswith("#")
                    or href.lower().startswith("javascript:")):
                priv_empty += 1
                continue
            full = urljoin(self.page.final_url, href)
            if full.startswith("http") and self.fetcher is not None and self.fetcher.exists(full):
                priv_ok = True
                break
            priv_dead.append(href)
        has_privacy = priv_ok
        if priv_links and not priv_ok:
            privacy_note = ("隐私政策/服务协议是"
                            + ("空链接（#或javascript:）" if priv_empty and not priv_dead else "空链接或页面打不开(疑似404)")
                            + "，等于没有合规页面")
        elif not priv_links:
            privacy_note = "页脚/页面中未找到隐私政策或服务协议入口（合规与信任信号缺一项）"
        else:
            privacy_note = None

        # 支撑理由：站点存在收集个人信息的功能时，说明"为什么需要隐私政策"
        features = []
        url_blob = " ".join((p.final_url or "").lower() for p in self.all_pages)                    + " " + " ".join((u or "").lower() for u in (self.all_site_urls or []))
        if re.search(r"member-(register|login)|/register|/signup", url_blob):
            features.append("会员注册/登录")
        if re.search(r"submit-question|my-question|/message|/liuyan", url_blob) or self.local_soup.find("form"):
            features.append("留言/提交问题表单")
        feature_note = (f"站点存在{'、'.join(features)}功能（会收集个人信息），按《个人信息保护法》"
                        "应有隐私政策作为告知载体" if features else None)

        signals = []
        if has_icp:
            signals.append("ICP备案")
        if has_contact:
            signals.append("联系方式")
        if has_privacy:
            signals.append("隐私政策")

        icp_note = ("检测到 ICP 备案号位数异常（" + "、".join(bad_icp)
                    + "），工信部备案号为 8 位（老站）或 10 位（新站）数字，位数不对会查不到，请核对"
                    if bad_icp else None)
        notes = "；".join(n for n in (privacy_note, feature_note, icp_note, copyright_note) if n)

        # 2026-09-10 V6：结论/示例/影响三段统一以"实际缺失清单"为准，不再各说各话
        missing = [n for n, has in (("ICP备案", has_icp), ("联系方式", has_contact),
                                    ("隐私政策入口", has_privacy)) if not has]
        if missing:
            item.suggestion = ("补充缺失的可信信号：" + "、".join(missing)
                               + "；律所/金融等特殊行业还需执业资质许可")
            item.impact = (f"缺少 {'、'.join(missing)} 会直接削弱 AI 与用户对站点可信度的判断，"
                           "降低推荐与引用概率")
        if len(signals) >= 2:
            if icp_note:
                return self._result(item, Status.WARN,
                                    f"可信信号基本完善，但备案号无法验证: {', '.join(signals)}", detail=notes)
            return self._result(item, Status.PASS, f"可信信号完善: {', '.join(signals)}",
                                detail=notes)
        if signals:
            return self._result(item, Status.WARN,
                                f"可信信号不足，仅有: {', '.join(signals)}；缺少: {'、'.join(missing)}",
                                detail=notes)
        return self._result(item, Status.FAIL,
                            "可信信号严重缺失：ICP备案、联系方式、隐私政策入口均未检出",
                            detail=notes)

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
            tier="bonus",  # 2026-09-10 V6定版：转化引导项，转只提醒
        )
        soup = self.local_soup
        testimonial_classes = ["testimonial", "review", "评价", "证言", "客户说", "customer-say"]
        has_testimonial = bool(soup.find(class_=re.compile(
            "|".join(testimonial_classes), re.I)))
        customer_logos = soup.find_all(class_=re.compile(r"customer|client|partner|logo-wall", re.I))

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
            tier="bonus",
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
        for a in self.local_soup.find_all("a", href=True):
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
            tier="bonus",  # 2026-09-10 V6定版：绿萝算法后外链权重大减，且只数数量不验权威，转只提醒
        )
        from urllib.parse import urlparse
        base_domain = urlparse(self.page.final_url).netloc
        external_domains = set()

        for a in self.local_soup.find_all("a", href=True):
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
            tier="bonus",  # 2026-09-10 V6定版：不联网AI凭训练知识判断，方法无效，转只提醒（需人工复核）
        )
        if not self.ai_engine:
            return self._result(item, Status.SKIP, "未检查（需配置AI语义引擎）",
                                detail="未配置LLM，无法进行知识图谱检查")
        status, summary, detail = self.ai_engine.consensus(lambda: self.ai_engine.check_knowledge_graph(self.page))
        return self._result(item, status, summary,
                            detail=detail + "（AI 基于训练知识判定，非实时联网，需人工复核）")

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
            tier="bonus",
        )
        if not self.ai_engine:
            return self._result(item, Status.SKIP, "未检查（需配置AI语义引擎）",
                                detail="未配置LLM，无法进行跨平台检查")
        status, summary, detail = self.ai_engine.consensus(lambda: self.ai_engine.check_cross_platform(self.page))
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
            tier="bonus",  # 2026-09-10 V6定版：AI引用不做交叉验证，署名/经验/来源不构成扣分依据，转只提醒
        )
        if not self.ai_engine:
            return self._result(item, Status.SKIP, "未检查（需配置AI语义引擎）",
                                detail="未配置LLM，无法进行E-E-A-T分析")

        # E-E-A-T 是内容页检查项，首页是品牌导航页不适用
        # 优先在抽样的内容页（文章/案例/产品页）上检查
        content_page = self._pick_content_page()
        if not content_page:
            return self._result(item, Status.SKIP,
                                "未抓取到内容详情页（栏目/列表页不作为检查对象），无法检查E-E-A-T内容可信度，需人工复核",
                                detail="E-E-A-T评估对象是文章/案例/产品详情页等有主体内容的页面；"
                                       "首页是品牌导航页不适用，与首页高度重复的克隆页、栏目列表页也不能作为评估对象",
                                skip_kind="missing")
        status, summary, detail = self.ai_engine.consensus(lambda: self.ai_engine.check_eeat(content_page))
        page_label = content_page.soup.title.get_text(strip=True)[:30] if content_page.soup.title else content_page.final_url
        return self._result(item, status, summary,
                            detail=f"检查页面: {page_label}。{detail}",
                            pages=self._pg(content_page))

    # AUTH-08 公司信息三处一致性（2026-09-05 整改新增，纯规则，不依赖AI）
    def _check_entity_consistency(self) -> CheckResult:
        item = self._item(
            "auth-08", "公司信息多处一致性", Priority.P0,
            "隐形名片、页脚、正文/关于页里的主体名、成立年份、人数、电话、官网写法是否一致",
            "抽取结构化名片字段与页面可见信息，逐字段交叉比对",
            "同一事实在站内各处口径唯一、互不矛盾",
            manual_check="分别打开首页、关于我们、页脚和源代码里的 Organization 名片，对照公司全称、成立时间、人数、电话、官网地址是否写的是同一个",
            purpose="AI 和搜索引擎做实体对齐时，会把站内各处出现的公司信息拼成一个'你是谁'的档案。同一件事出现两个说法（成立年、规模、电话对不上），AI 无法判断哪个可信，会降低采信甚至弃用，这正是 GEO 的硬伤",
            excellent="名片、页脚、正文/关于页的主体名称、成立年份、规模、电话、官网写法完全一致，可被 AI 稳定识别为同一实体",
            normal="主要信息一致，个别写法不统一（如官网 www/http 混用）",
            failed="成立年份、规模或电话等关键信息在名片与页面上自相矛盾",
            scope="全站", applicable_pages="全站",
            suggestion="先内部定一份唯一的官方事实表（主体全称、成立年、规模、对外电话、官网标准写法），再同步改正文、页脚与隐形名片",
            tier="bonus",  # 2026-09-10 V6定版：矛盾未阻止引用，转只提醒
        )

        # ── 来源一：全站隐形名片（JSON-LD 组织节点）──
        ld_names, ld_legal, ld_years, ld_emp, ld_tels, ld_urls = set(), set(), set(), set(), set(), set()
        for p in self.all_pages:
            for sc in p.soup.find_all("script", attrs={"type": "application/ld+json"}):
                if not sc.string:
                    continue
                try:
                    data = json.loads(sc.string)
                except Exception:
                    continue
                for n in _iter_ld_nodes(data):
                    if not isinstance(n, dict):
                        continue
                    t = n.get("@type", "")
                    types = t if isinstance(t, list) else [t]
                    if not any(any(ot in str(x) for ot in _ORG_TYPES) for x in types):
                        continue
                    if n.get("name") and _readable_name(n["name"]):
                        ld_names.add(str(n["name"]).strip())
                    if n.get("legalName") and _readable_name(n["legalName"]):
                        ld_legal.add(str(n["legalName"]).strip())
                    fy = _first_number(n.get("foundingDate") or n.get("foundingYear"))
                    if fy and 1900 < fy < 2100:
                        ld_years.add(fy)
                    emp = _first_number(n.get("numberOfEmployees"))
                    if emp:
                        ld_emp.add(emp)
                    tel_keys = ["telephone", "phone"]
                    cps = n.get("contactPoint") or []
                    cp_list = cps if isinstance(cps, list) else [cps]
                    tel_src = [n.get(k) for k in tel_keys if n.get(k)]
                    for cp in cp_list:
                        if isinstance(cp, dict) and cp.get("telephone"):
                            tel_src.append(cp["telephone"])
                    for tt in tel_src:
                        nt = _norm_tel(tt)
                        if nt:
                            ld_tels.add(nt)
                    u = n.get("url")
                    for uu in (u if isinstance(u, list) else [u]):
                        if uu:
                            ld_urls.add(str(uu).strip())

        # ── 来源二：可见文本（首页 + 关于页）──
        about = [p for p in self.all_pages if SiteCrawler.classify(p.final_url, "") == "about"]
        visible_pages = ([self.page] + about) if about else [self.page]
        body_text, footer_text = "", ""
        for p in visible_pages:
            body_text += "\n" + p.soup.get_text(" ", strip=True)
            f = p.soup.find("footer") or p.soup.find(class_=re.compile(r"footer", re.I))
            footer_text += "\n" + (f.get_text(" ", strip=True) if f else "")
        if not footer_text:
            footer_text = body_text

        # 成立年份：中英双语（成立于2003 / Founded in 2003 / Established 1998 / Est. 2003）
        body_years = {int(y) for y in re.findall(
            r"(?:成立于|成立时间|成立|founded(?:\s+in)?|established(?:\s+in)?|est\.)[^\d]{0,8}((?:19|20)\d{2})",
            body_text, re.I)}
        # 员工规模：中英双语（120名员工 / 100+ employees / 50 team members）
        body_emp = {int(x) for x in re.findall(
            r"(\d{2,5})\s*\+?\s*(?:多名|余|多)?\s*(?:名|位)?\s*(?:员工|人才|技术人员|专业人才|技术人才|团队成员|人员|人技术团队|人团队|人专业团队)",
            body_text)}
        body_emp |= {int(x) for x in re.findall(
            r"(\d{2,5})\s*\+?\s*(?:employees?|staff members?|team members?|people|personnel)",
            body_text, re.I)}
        body_tels = {_norm_tel(x) for x in re.findall(r"400[-\s]?\d{3}[-\s]?\d{4}|1[3-9]\d{9}", footer_text + body_text)}
        # 公司主体：中文后缀 + 英文后缀（Ltd./Inc./LLC/Corp./GmbH 等）
        footer_companies = set(re.findall(
            r"[一-龥（）()]{2,28}?(?:有限责任公司|股份有限公司|集团有限公司|有限公司)", footer_text))
        footer_companies |= set(x.strip() for x in re.findall(
            r"\b[A-Z][A-Za-z0-9 .&'\-]{1,50}?\s(?:Co\.,?\s?Ltd\.?|Ltd\.?|LLC|Inc\.?|Corp(?:oration)?\.?|GmbH|Limited|Company)\b",
            footer_text))
        ld_company_names = {x for x in (ld_names | ld_legal)
                            if re.search(r"(?:公司|集团|Inc\.?|Ltd\.?|LLC|Corp|GmbH|Limited|Company)", x, re.I)}

        # ── 逐槽比对，hard=硬冲突，soft=写法不统一 ──
        hard, soft, lines = [], [], []

        # 1) 成立年份
        years = ld_years | body_years
        if years:
            src = []
            if ld_years:
                src.append(f"隐形名片写{('/'.join(map(str, sorted(ld_years))))}")
            if body_years:
                src.append(f"页面正文写成立于{('/'.join(map(str, sorted(body_years))))}")
            if len(years) >= 2:
                hard.append(f"成立年份不一致：{'，'.join(src)}")
            else:
                lines.append(f"成立年份：{src[0]}")
        # 2) 团队规模
        if ld_emp or body_emp:
            tag = []
            if ld_emp:
                tag.append(f"隐形名片写{'/'.join(map(str, sorted(ld_emp)))}人")
            if body_emp:
                tag.append(f"页面正文写{'/'.join(map(str, sorted(body_emp)))}人规模")
            nums = sorted(ld_emp | body_emp)
            if ld_emp and body_emp and (max(nums) > min(nums) * 2):
                hard.append("团队规模对不上：" + "，".join(tag))
            elif len(body_emp) >= 2 and max(body_emp) > min(body_emp) * 1.5:
                soft.append("正文里团队规模有多种说法：" + "/".join(map(str, sorted(body_emp))))
            else:
                lines.append("团队规模：" + "，".join(tag))
        # 3) 电话：名片留的号 vs 页面展示的号，完全不交集才算冲突（400与手机同时都列且两边一致不算）
        if ld_tels or body_tels:
            if ld_tels and body_tels and not (ld_tels & body_tels):
                hard.append(f"联系电话对不上：隐形名片留 {sorted(ld_tels)}，页面展示 {sorted(body_tels)}")
            else:
                lines.append(f"联系电话：名片{sorted(ld_tels) or '未写'} / 页面{sorted(body_tels) or '未写'}")
        # 4) 官网写法
        if ld_urls:
            hosts = {_norm_host(u) for u in ld_urls if _norm_host(u)}
            if len(hosts) >= 2:
                hard.append("隐形名片里官网指向多个不同域名：" + "、".join(sorted(ld_urls)))
            elif len({u.strip().lower() for u in ld_urls}) >= 2:
                soft.append("隐形名片里官网写法不统一（www/非www、http/https 混用）：" + "、".join(sorted(ld_urls)))
            else:
                lines.append("官网写法：" + list(ld_urls)[0])
        # 5) 主体全称：名片里的每个公司主体，都要能在页脚找到对应（互相包含即可）；
        #    页脚额外出现的母公司/关联公司名不要求名片都覆盖，品牌名也不参与比对
        if ld_company_names or footer_companies:
            no_match = []
            for a in ld_company_names:
                if not any(a in b or b in a for b in footer_companies):
                    no_match.append(a)
            if no_match:
                hard.append("隐形名片里的公司主体在页脚找不到对应：" + "、".join(sorted(no_match)[:3])
                            + f"（页脚写的是：{'、'.join(sorted(footer_companies)[:3])}）")
            else:
                names = sorted(ld_names | footer_companies)
                lines.append("识别到的名称（品牌/主体/母公司并存属正常）：" + "、".join(names[:5]))

        slots_found = sum(bool(x) for x in [years, ld_emp | body_emp, ld_tels | body_tels,
                                            ld_urls, ld_company_names | footer_companies])
        if slots_found < 2:
            return self._result(item, Status.SKIP,
                                "未从名片/页脚/正文抽取到足够公司信息，无法做一致性交叉核对",
                                detail="可能页面由JS渲染或未标注公司主体信息")
        detail_parts = hard + soft + lines
        # 「隐形名片」是站内术语，首次出现必须交代查看路径与要改哪些页面（业务方反馈）
        if any("隐形名片" in x for x in detail_parts):
            detail_parts.append(
                "【在哪看】隐形名片=写进网页源代码、给AI看的公司信息标签。打开出现不一致的那个页面→"
                "空白处右键→「查看网页源代码」→Ctrl+F 搜索 ld+json，代码里 name/telephone/foundingDate 等就是名片内容；"
                "完整不一致明细见本页下方「技术明细」该项说明区。"
                "【改哪里】① 每个页面源代码里自己的 ld+json 标签（由建站后台/模板统一输出，通常改模板一处全站生效）；"
                "② 页脚的公司主体与版权信息（模板页脚）；③ 正文里写错成立年份的关于我们/介绍页。"
                "改前先定一份唯一的官方事实表（全称/成立年/电话/官网写法），所有地方照它改齐")
        detail = "；".join(detail_parts)
        auth_pages = [(urlparse(p.final_url).path or "/") for p in visible_pages]
        if hard:
            return self._result(item, Status.FAIL,
                                f"有 {len(hard)} 处公司关键信息自相矛盾——矛盾会让 AI 随机采用其中一个版本，你失去对自己叙事的控制",
                                detail=detail, pages=auth_pages)
        if soft:
            return self._result(item, Status.WARN,
                                f"关键信息基本一致，但有 {len(soft)} 处写法不统一",
                                detail=detail, pages=auth_pages)
        return self._result(item, Status.PASS, "隐形名片、页脚、正文的公司信息口径一致",
                            detail=detail, pages=auth_pages)
