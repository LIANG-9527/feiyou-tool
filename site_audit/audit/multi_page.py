"""内页质量扫描（2026-09-05 整改·改动1）

以前各类"页面级检查"只在首页跑一遍，抽样抓到的内页只供少量跨页对比，导致内页的
标题/描述/主标题层级/结构化/正文厚度/图片说明问题天然看不见。本检查器对抽样到的
每个内页用"与首页同一把尺子"检查，并把问题**按类型聚合成固定几条结果**（不随页数
膨胀），detail 里列出具体问题页，做到"任何一页不达标都报、报告不刷屏"。

另外 mp-06 依据必查类型覆盖，报告"关于/产品服务/FAQ/隐私"这类必备页是否缺失，
其中 FAQ 缺失按业务定调（全行业计分、缺了降低被 AI 引用概率）明确点名。
"""
import re
from typing import List
from urllib.parse import urlparse

from .checker import BaseChecker
from site_audit.core.models import CheckResult, Status, Priority, Category
from site_audit.core.crawler import SiteCrawler


def _short(url: str) -> str:
    p = urlparse(url)
    return (p.path or "/") + (f"?{p.query}" if p.query else "")


def _bigrams(text: str) -> set:
    s = re.sub(r"\s+", "", text or "")
    return {s[i:i + 2] for i in range(max(0, len(s) - 1))}


def _text_jaccard(a: str, b: str) -> float:
    """两个页面正文的字符二元组 Jaccard 重合度（0-1），对公共头尾不敏感、线性复杂度。"""
    A, B = _bigrams(a), _bigrams(b)
    if not A or not B:
        return 0.0
    return len(A & B) / len(A | B)


class MultiPageChecker(BaseChecker):
    """内页多页聚合检查，一个 checker 产出 AI 维度与内容维度的多条结果。"""
    category = Category.AI_FRIENDLY

    def run(self) -> List[CheckResult]:
        return [
            self._check_inner_title_desc(),
            self._check_inner_heading(),
            self._check_inner_structured(),
            self._check_inner_depth(),
            self._check_inner_img_alt(),
            self._check_required_pages(),
            self._check_page_clone(),
        ]

    # 通用聚合：problems=[(路径, 说明)]，按问题页占比定状态
    @staticmethod
    def _verdict(problem_pages: int, total: int, fail_ratio: float = 0.5):
        if total == 0:
            return Status.SKIP
        if problem_pages == 0:
            return Status.PASS
        return Status.FAIL if problem_pages / total >= fail_ratio else Status.WARN

    def _inner_pages(self):
        """只扫抽样内页（首页由原有各项负责，避免重复）。"""
        return self.extra_pages or []

    # mp-01 内页标题与描述
    def _check_inner_title_desc(self) -> CheckResult:
        item = self._item(
            "mp-01", "内页标题与描述规范", Priority.P0,
            "抽样内页是否都有独立、长度合理的标题和描述",
            "逐页检查 <title> 与 meta description 是否存在、长度是否合理、是否套用首页",
            "每个内页都有独立标题(15-80字)和描述(50-200字)",
            purpose="内页标题/描述是该页能否被搜索和 AI 正确识别的第一信号；多个内页套用首页或留空，会导致该收录的页面不被收录",
            manual_check="逐页查看浏览器标签标题和源代码 description，确认关于、案例、产品、文章等内页各不相同且非空",
            excellent="所有抽样内页标题、描述独立且长度合理，准确描述本页主题",
            normal="多数内页合格，个别标题/描述缺失或长度不当",
            failed="大量内页缺标题/描述或直接套用首页",
            scope="全站", applicable_pages="全站",
            suggestion="为每个栏目/详情页填写独立的标题与描述，不要复用首页 SEO 字段",
        )
        pages = self._inner_pages()
        if not pages:
            return self._result(item, Status.SKIP, "未抽样到内页，无法检查内页标题描述",
                                skip_kind="missing")
        problems = []
        for p in pages:
            t = p.soup.title.get_text(strip=True) if p.soup.title else ""
            d = p.meta("description") or ""
            why = []
            if not t:
                why.append("缺标题")
            elif len(t) < 15:
                why.append(f"标题过短({len(t)})")
            elif len(t) > 80:
                why.append(f"标题过长({len(t)})")
            if not d:
                why.append("缺描述")
            elif len(d) < 50:
                why.append(f"描述过短({len(d)})")
            if why:
                problems.append(f"{_short(p.final_url)}：{'、'.join(why)}")
        status = self._verdict(len(problems), len(pages))
        if not problems:
            return self._result(item, Status.PASS, f"{len(pages)} 个抽样内页标题、描述均齐全且长度合理",
                                pages=[p.final_url for p in pages])
        return self._result(item, status,
                            f"{len(problems)}/{len(pages)} 个内页标题或描述不达标",
                            detail="；".join(problems[:8]) + ("…" if len(problems) > 8 else ""),
                            pages=[x.split("：")[0] for x in problems])

    # mp-02 内页主标题层级（每页 H1 唯一；详情页还需有子标题层级）
    def _check_inner_heading(self) -> CheckResult:
        item = self._item(
            "mp-02", "内页主标题(H1)唯一性", Priority.P0,
            "抽样内页是否每页只有 1 个 H1，正文小节是否误用 H1；有实质正文的详情页是否具备 H2/H3 子标题层级",
            "逐页统计 H1 数量：0 个或多于 1 个即为不达标；详情页有实质正文却通篇无 H2/H3 子标题同样计入",
            "每页恰好 1 个 H1，正文小节用 H2/H3",
            purpose="多个 H1 会稀释页面主题，AI 无法判断主标题，正文小节误用 H1 时更难抓到重点；详情页没有子标题层级，AI 难以定位可摘取的答案片段",
            manual_check="逐页查看源代码里 <h1> 数量，文章正文的小节标题应为 <h2>/<h3>；案例/文章详情页应有分节小标题而不是一整段到底",
            excellent="每个内页恰好 1 个 H1，层级清晰",
            normal="个别内页 H1 数量不为 1，或个别详情页缺少子标题层级",
            failed="多个内页缺 H1 或 H1 泛滥（如一篇文章多个 H1）",
            scope="全站", applicable_pages="全站",
            suggestion="每页只保留 1 个 H1，把正文里误用的 H1 降为 H2/H3；详情页按内容分节加 H2/H3 小标题",
        )
        pages = self._inner_pages()
        if not pages:
            return self._result(item, Status.SKIP, "未抽样到内页，无法检查内页标题层级",
                                skip_kind="missing", pages=[p.final_url for p in self.all_pages])
        problems = []
        for p in pages:
            n = len(p.soup.find_all("h1"))
            if n != 1:
                problems.append(f"{_short(p.final_url)}：{n} 个 H1")
                continue
            # 详情页层级：有实质正文却通篇无 H2/H3，AI 难以定位可摘取内容（列表页/短页不要求）
            if (SiteCrawler._is_detail(p.final_url) and p.word_count() >= 200
                    and not p.soup.find_all("h2") and not p.soup.find_all("h3")):
                problems.append(f"{_short(p.final_url)}：详情页有正文但通篇无 H2/H3 子标题，AI 难定位可摘取内容")
        status = self._verdict(len(problems), len(pages))
        if not problems:
            return self._result(item, Status.PASS, f"{len(pages)} 个抽样内页标题层级规范（均为单一 H1）",
                                pages=[p.final_url for p in pages])
        return self._result(item, status,
                            f"{len(problems)}/{len(pages)} 个内页标题层级不达标",
                            detail="；".join(problems[:8]) + ("…" if len(problems) > 8 else ""),
                            pages=[x.split("：")[0] for x in problems])

    # mp-03 详情页结构化数据
    def _check_inner_structured(self) -> CheckResult:
        item = self._item(
            "mp-03", "详情页结构化数据", Priority.P1,
            "文章/产品/案例详情页是否有与其类型匹配的结构化标记",
            "对抽样中的详情页检查 JSON-LD / Microdata",
            "详情页有 Article/Product/Service 等结构化标记",
            purpose="详情页带结构化标记，AI 才能稳定识别它是一篇文章/一个产品/一个案例并被精准引用",
            manual_check="打开文章、产品、案例详情页源代码，搜索 application/ld+json",
            excellent="各详情页均有对应类型的结构化标记",
            normal="部分详情页缺少结构化标记",
            failed="所有详情页都没有结构化标记",
            scope="全站", applicable_pages="全站",
            suggestion="文章页加 Article、产品页加 Product、案例页加 Article/Service 结构化标记",
        )
        detail_pages = [p for p in self._inner_pages() if SiteCrawler._is_detail(p.final_url)]
        if not detail_pages:
            return self._result(item, Status.SKIP, "未抽样到文章/产品/案例详情页")
        missing = []
        for p in detail_pages:
            has_ld = bool(p.soup.find_all("script", attrs={"type": "application/ld+json"}))
            has_micro = bool(p.soup.find_all(attrs={"itemscope": True}))
            if not has_ld and not has_micro:
                missing.append(_short(p.final_url))
        status = self._verdict(len(missing), len(detail_pages))
        if not missing:
            return self._result(item, Status.PASS, f"{len(detail_pages)} 个详情页均含结构化标记",
                                pages=[p.final_url for p in detail_pages])
        return self._result(item, status,
                            f"{len(missing)}/{len(detail_pages)} 个详情页无任何结构化标记",
                            detail="；".join(missing[:8]) + ("…" if len(missing) > 8 else ""),
                            pages=list(missing))

    # mp-04 内页正文厚度
    def _check_inner_depth(self) -> CheckResult:
        item = self._item(
            "mp-04", "内页正文厚度", Priority.P1,
            "抽样内页是否有足够正文（与首页同一口径：<200 字过薄、<500 字偏薄）",
            "逐页统计纯文本字数",
            "内页正文充实（建议≥500字）",
            purpose="过薄的内页给不出有效信息，AI 不会选为答案来源，也撑不起该页要竞争的关键词",
            manual_check="打开各内页，确认不是只有标题和几张图、是否有足够说明文字",
            scope="全站", applicable_pages="全站",
        )
        item.category = Category.CONTENT
        pages = self._inner_pages()
        if not pages:
            return self._result(item, Status.SKIP, "未抽样到内页", skip_kind="missing")
        thin = []
        for p in pages:
            n = p.word_count()
            if n < 500:
                thin.append(f"{_short(p.final_url)}：{n}字{'（过薄）' if n < 200 else '（偏薄）'}")
        status = self._verdict(len(thin), len(pages))
        if not thin:
            return self._result(item, Status.PASS, f"{len(pages)} 个抽样内页正文均达到基本厚度",
                                pages=[p.final_url for p in pages])
        return self._result(item, status,
                            f"{len(thin)}/{len(pages)} 个内页正文偏薄或过薄",
                            detail="；".join(thin[:8]) + ("…" if len(thin) > 8 else ""),
                            pages=[x.split("：")[0] for x in thin])

    # mp-05 内页图片说明
    def _check_inner_img_alt(self) -> CheckResult:
        item = self._item(
            "mp-05", "内页图片说明(alt)覆盖", Priority.P2,
            "抽样内页的图片是否配有说明文字（与首页同一口径：覆盖率<50% 不达标）",
            "逐页统计 img 的 alt 覆盖率",
            "内页图片 alt 覆盖率≥80%",
            purpose="没有 alt，AI 看不懂图片内容，图片搜索流量和无障碍都受损",
            manual_check="逐页查看源代码 <img> 是否带 alt 且非空",
            scope="全站", applicable_pages="全站",
        )
        item.category = Category.CONTENT
        pages = self._inner_pages()
        if not pages:
            return self._result(item, Status.SKIP, "未抽样到内页", skip_kind="missing")
        bad = []
        checked = 0
        checked_pages = []
        for p in pages:
            imgs = p.soup.find_all("img")
            if not imgs:
                continue
            checked += 1
            checked_pages.append(p.final_url)
            with_alt = [i for i in imgs if (i.get("alt") or "").strip()]
            ratio = len(with_alt) / len(imgs)
            if ratio < 0.5:
                bad.append(f"{_short(p.final_url)}：覆盖率{int(ratio * 100)}%（{len(with_alt)}/{len(imgs)}）")
        if checked == 0:
            return self._result(item, Status.SKIP, "抽样内页均无图片")
        status = self._verdict(len(bad), checked)
        if not bad:
            return self._result(item, Status.PASS, f"{checked} 个含图内页图片说明覆盖率均达标",
                                pages=checked_pages)
        return self._result(item, status,
                            f"{len(bad)}/{checked} 个内页图片说明覆盖率低于50%",
                            detail="；".join(bad[:8]) + ("…" if len(bad) > 8 else ""),
                            pages=[x.split("：")[0] for x in bad])

    # mp-06 必备页面完备性（全行业；FAQ 缺失重点提示）
    def _check_required_pages(self) -> CheckResult:
        item = self._item(
            "mp-06", "必备页面完备性", Priority.P0,
            "是否具备关于、产品/服务、FAQ、隐私政策这类每个官网都该有的页面",
            "对首页+抽样内页按网址栏目归类，核对必备类型是否在可达范围内出现",
            "关于、产品/服务、FAQ、隐私政策齐备且可达",
            purpose="这些页面分别回答'你是谁、你卖什么、客户常问什么、是否正规'。缺 FAQ 时 AI 没有现成问答可摘，"
                    "在推荐答案里引用你的概率实打实下降；缺关于/隐私则实体与可信度立不起来",
            manual_check="对照导航与站点地图，确认关于我们、产品/服务、常见问题、隐私政策都有独立可打开页面",
            excellent="关于、产品/服务、FAQ、隐私政策齐全，联系信息全站可见",
            normal="缺 1-2 类必备页",
            failed="缺 3 类及以上必备页，站点信息严重不完整",
            scope="全站", applicable_pages="全站",
            suggestion="补齐缺失的必备页；尤其建立 FAQ 页（客户高频问答），这是被 AI 摘取引用率最高的内容形式",
        )
        item.category = Category.CONTENT
        all_urls = [p.final_url for p in self.all_pages]
        types = {SiteCrawler.classify(u, "") for u in all_urls}
        # 强必备（全行业，缺了计入状态）
        required = [("about", "关于我们"), ("offer", "产品/服务"),
                    ("faq", "FAQ常见问题"), ("privacy", "隐私政策")]
        missing_strong = [name for key, name in required if key not in types]
        # 提示性（行业差异/条件型：站内本应有这类业务才要求，只在 detail 提示，不决定状态、不判丢分）
        optional = [("case", "客户案例"), ("news", "资讯/文章"), ("contact", "独立联系页"),
                    ("help", "帮助文档"), ("honor", "资质荣誉")]
        missing_optional = [name for key, name in optional if key not in types]

        if not all_urls:
            return self._result(item, Status.SKIP, "无页面可核对")
        detail = []
        if missing_strong:
            note = "、".join(missing_strong)
            if "FAQ常见问题" in missing_strong:
                note += "（缺FAQ会降低AI回答时引用你的概率）"
            detail.append("缺少：" + note)
        if missing_optional:
            detail.append("以下非强制、建议补充：" + "、".join(missing_optional))
        detail.append(f"本次可达范围（首页+两层内链+站点地图）共识别到页面类型：{('、'.join(sorted(types))) or '无'}")

        if not missing_strong:
            return self._result(item, Status.PASS, "关于、产品/服务、FAQ、隐私政策均在可达范围内",
                                detail="；".join(detail[1:]), pages=all_urls)
        n = len(missing_strong)
        status = Status.FAIL if n >= 3 else Status.WARN
        return self._result(item, status, f"缺少 {n} 类必备页：{'、'.join(missing_strong)}",
                            detail="；".join(detail), pages=all_urls)

    # mp-07 关键页面与首页正文雷同（页面撞脸）
    def _check_page_clone(self) -> CheckResult:
        item = self._item(
            "mp-07", "页面与首页正文雷同", Priority.P1,
            "关于我们等内页是否只是首页内容的复制副本、缺少独立信息增量",
            "逐页计算内页正文与首页正文的字符重合度，超过 85% 判为高度雷同",
            "内页与首页正文重合度 <70%，关于/介绍类页面有独立信息",
            purpose="关于、介绍这类页面本该讲清主体、沿革、团队等增量信息；若只是把首页换个链接再放一遍，属于重复内容，搜索引擎不重复收录、AI 也得不到可引用的新事实",
            manual_check="并排打开首页与关于我们页，对比正文是否大段相同",
            excellent="各内页与首页正文重合度低，关于/介绍页有独立内容",
            normal="个别内页与首页相似度偏高",
            failed="关于等关键内页几乎是首页的完整副本",
            scope="全站", applicable_pages="首页+内页",
            suggestion="把关于/介绍页做成真正的实体页：公司主体、发展历程、团队、资质、办公网络等独立信息，导航指向独立 URL 而非首页锚点",
        )
        item.category = Category.CONTENT
        pages = self._inner_pages()
        home_text = self.page.get_text()
        if not pages or len(home_text) < 200:
            return self._result(item, Status.SKIP, "缺少可对比的内页或首页正文", skip_kind="missing")
        clones = []
        for p in pages:
            txt = p.get_text()
            if len(txt) < 200:
                continue
            sim = _text_jaccard(home_text, txt)
            if sim > 0.85:
                clones.append((sim, _short(p.final_url)))
        if not clones:
            return self._result(item, Status.PASS, f"{len(pages)} 个内页均未与首页正文高度雷同",
                                pages=[p.final_url for p in pages])
        clones.sort(reverse=True)
        status = Status.FAIL if len(clones) >= 3 else Status.WARN
        detail = "；".join(f"{path} 重合{int(s * 100)}%" for s, path in clones[:8])
        return self._result(item, status,
                            f"{len(clones)} 个内页疑似首页内容副本",
                            detail=detail + "（关于/介绍类页面应提供独立信息，不应整页复制首页）",
                            pages=[path for _, path in clones])
