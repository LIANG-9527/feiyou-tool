"""跨页面检查：断开内链、重复 Title/Description、关键词蚕食（规则版）
这些检查依赖多页面抽样数据，属于全站级检查。"""
import re
from typing import List
from urllib.parse import urlparse

from .checker import BaseChecker
from site_audit.core.models import CheckResult, Status, Priority, Category
from site_audit.core.crawler import SiteCrawler


def _bigrams(s: str) -> set:
    s = re.sub(r"[\s\-\|_–—,，。\.、:：;；()（）\[\]【】]+", "", s or "")
    return {s[i:i + 2] for i in range(len(s) - 1)} if len(s) >= 2 else set()


def _jaccard(a: set, b: set) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


class CrossPageChecker(BaseChecker):
    """跨页面检查器：一个 checker 产出分属三个维度的结果。"""
    category = Category.TECHNICAL

    def run(self) -> List[CheckResult]:
        return [
            self._check_broken_internal_links(),
            self._check_duplicate_meta(),
            self._check_keyword_cannibalization(),
        ]

    # 技术-13 断开内部链接
    def _check_broken_internal_links(self) -> CheckResult:
        item = self._item(
            "tech-13", "内部链接可访问性（断链检测）", Priority.P1,
            "站内链接是否都能正常打开，无 404/5xx 死链",
            "抓取首页及抽样内页中的同域链接，逐个请求状态码",
            "站内无死链，所有内链返回 200",
            manual_check="在浏览器逐个点击导航和正文链接，或用 Xenu/Screaming Frog 爬取，筛选状态码为 404/5xx 的链接",
            purpose="死链会让 AI 爬虫抓取中断、降低整站抓取效率，用户点击到 404 页面也会直接流失，是站点健康度的基础指标",
            excellent="全站抽样链接全部返回 200，无任何死链，重定向链短",
            normal="存在 1-2 个死链，集中在非核心页面",
            failed="存在 3 个及以上死链，或核心导航/产品链接打不开",
        )
        item.category = Category.TECHNICAL
        if not self.extra_pages:
            return self._result(item, Status.SKIP,
                                "未检查（需多页面抽样，当前仅抓取首页）")
        crawler = SiteCrawler(self.fetcher)
        broken = crawler.check_internal_link_status(self.all_pages)
        if not broken:
            return self._result(item, Status.PASS,
                                f"抽样检查的站内链接均可访问（覆盖 {len(self.all_pages)} 个页面）")
        detail = "；".join(f"{url} → {code or '无响应'}" for url, code, _ in broken[:8])
        status = Status.FAIL if len(broken) >= 3 else Status.WARN
        return self._result(item, status, f"发现 {len(broken)} 个失效内部链接", detail=detail)

    # AI-11 重复 Title/Description
    def _check_duplicate_meta(self) -> CheckResult:
        item = self._item(
            "ai-11", "Title/Description 唯一性", Priority.P1,
            "各页面的 Title 和 Meta Description 是否唯一、无多页重复",
            "对比首页与抽样内页的 Title、Description 文本是否重复",
            "每个页面有独立且差异化的 Title 和 Description",
            manual_check="把站内主要页面的 title 和 description 复制到表格对比，看是否有多页完全相同",
            purpose="多页共用同一 Title/Description 会让 AI 和搜索引擎无法区分页面主题，导致权重分散、该收录的页面不被收录",
            excellent="所有抽样页面 Title、Description 均唯一且准确描述各自主题，无重复",
            normal="个别页面 Title/Description 相似但不完全相同，或模板化程度较高",
            failed="多个页面 Title 或 Description 完全相同，页面主题无法区分",
        )
        item.category = Category.AI_FRIENDLY
        if not self.extra_pages:
            return self._result(item, Status.SKIP,
                                "未检查（需多页面抽样，当前仅抓取首页）")
        metas = []
        for p in self.all_pages:
            title = (p.soup.title.get_text(strip=True) if p.soup.title else "") or ""
            desc = p.meta("description") or ""
            metas.append((p.final_url, title, desc))

        def find_dups(idx, label):
            bucket = {}
            for url, title, desc in metas:
                key = (title if idx == 1 else desc).strip()
                if key:
                    bucket.setdefault(key, []).append(url)
            return {k: v for k, v in bucket.items() if len(v) >= 2}

        dup_titles = find_dups(1, "title")
        dup_descs = find_dups(2, "desc")
        problems = []
        if dup_titles:
            for t, urls in list(dup_titles.items())[:3]:
                problems.append(f"重复Title「{t[:25]}」×{len(urls)}页")
        if dup_descs:
            for d, urls in list(dup_descs.items())[:3]:
                problems.append(f"重复Description「{d[:25]}」×{len(urls)}页")
        if not problems:
            return self._result(item, Status.PASS,
                                f"{len(metas)} 个抽样页面的 Title/Description 均唯一")
        n = len(dup_titles) + len(dup_descs)
        status = Status.FAIL if n >= 3 else Status.WARN
        return self._result(item, status, f"发现 {n} 组重复的 Title/Description",
                            detail="；".join(problems))

    # 内容-08 关键词蚕食（规则版：标题主题相似度）
    def _check_keyword_cannibalization(self) -> CheckResult:
        item = self._item(
            "content-08", "关键词蚕食检测", Priority.P2,
            "站内多个页面是否争夺同一关键词（规则版按标题主题相似度初筛，语义版待AI层）",
            "提取各页 Title 主题核心，两两计算字符相似度，识别疑似重叠",
            "每页关键词定位差异化，无高相似主题页面",
            manual_check="列出站内主要页面及核心关键词，对比是否有2个以上页面瞄准同一个词，标题和正文主题是否高度重叠",
            purpose="多个页面争夺同一关键词时，AI 和搜索引擎分不清该推荐哪页，可能都不引用或引用错误页面，整站该词排名互相拖后腿",
            excellent="各页面 Title 主题清晰差异化，无高相似页面，形成明确关键词矩阵",
            normal="存在少量主题相近页面，但已通过 canonical 或内链锚文本区分主次",
            failed="多个页面标题主题高度重叠，疑似互相蚕食且无主次区分",
        )
        item.category = Category.CONTENT
        if not self.extra_pages:
            return self._result(item, Status.SKIP,
                                "未检查（需多页面抽样，当前仅抓取首页）")
        # 提取每页标题主题（去品牌词：出现≥2次的分隔片段视为品牌）
        raw = []
        for p in self.all_pages:
            t = p.soup.title.get_text(strip=True) if p.soup.title else ""
            raw.append((p.final_url, t))
        seg_counter = {}
        for _, t in raw:
            for seg in re.split(r"[\-\|_–—_]+", t):
                seg = seg.strip()
                if seg:
                    seg_counter[seg] = seg_counter.get(seg, 0) + 1
        brand_segs = {s for s, c in seg_counter.items() if c >= 2}

        def core(t):
            parts = [seg.strip() for seg in re.split(r"[\-\|_–—_]+", t) if seg.strip() and seg.strip() not in brand_segs]
            return "".join(parts)

        cores = [(url, _bigrams(core(t)), core(t)) for url, t in raw if core(t)]
        hits = []
        threshold = 0.7
        for i in range(len(cores)):
            for j in range(i + 1, len(cores)):
                sim = _jaccard(cores[i][1], cores[j][1])
                if sim >= threshold:
                    hits.append((sim, cores[i][2][:18], cores[j][2][:18],
                                 urlparse(cores[i][0]).path, urlparse(cores[j][0]).path))
        if not hits:
            return self._result(item, Status.PASS,
                                f"{len(cores)} 个页面标题主题差异化，未发现关键词蚕食")
        hits.sort(reverse=True)
        detail = "；".join(f"「{a}」↔「{b}」相似度{int(s*100)}%" for s, a, b, _, _ in hits[:5])
        status = Status.FAIL if len(hits) >= 3 else Status.WARN
        return self._result(item, status,
                            f"发现 {len(hits)} 对疑似关键词蚕食的页面（规则初筛，建议人工确认）",
                            detail=detail)
