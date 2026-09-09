"""站点多页面爬虫：从首页提取同域内链，按"必查类型保底 + 多样化补充"抽样抓取。

2026-09-05 整改（改动2）：
1. 页面类型识别从"只看网址"升级为"网址 + 链接文字"双线索（解决拼音网址认不出）；
2. 必查类型（关于/联系/产品服务/案例/资讯/FAQ/隐私）保底占名额，同类页面不得占满；
3. 首页页内锚点（/index.php#about 之类）与首页同体路径不算独立页面；
4. 记录每类必查页"发现/选取"情况（last_coverage），整站找不到时由上层报"未发现该类页面"，
   不拿别的页面凑数。
"""
from __future__ import annotations
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.parse import urljoin, urlparse
from typing import List, Optional, Dict

from .fetcher import Fetcher, PageData


# 非 HTML 资源后缀，不抓取
RESOURCE_EXT = re.compile(
    r"\.(pdf|zip|rar|7z|exe|dmg|pkg|png|jpe?g|gif|svg|webp|ico|css|js|"
    r"mp[34]|avi|mov|wmv|flv|docx?|xlsx?|pptx?|txt|xml|json|woff2?|ttf|rar)(\?|$)",
    re.I,
)

# 首页同体路径：去锚点后若落到这些路径，本质还是首页，不算独立内页
HOME_ALIAS_PATHS = {
    "/", "/index", "/index.php", "/index.html", "/index.shtml",
    "/index.asp", "/index.aspx", "/default.html", "/home",
}

# ----------------------------------------------------------------------------
# 页面类型识别（双线索：网址路径 + 链接文字）。关键词覆盖英文、拼音缩写、中文。
# 顺序即"必查保底"的优先级：越靠前越优先占名额。
#   key         语义类型
#   required    always=全行业必查 / conditional=条件必查（命中站点特征才要求，先尽量抓）
#   quota       保底选取数量
#   list/detail 是否区分列表页与详情页（列表1+详情1）
# ----------------------------------------------------------------------------
TYPE_SPECS = [
    # key, 正则(网址|锚文本通用), required, quota, 分列表详情
    ("about",   r"about|guanyu|jianjie|profile|intro|company|qiyejianjie|关于|简介|走进|了解我们|公司概况", "always", 1, False),
    ("contact", r"contact|lianxi|lxwm|liuyan|message|guestbook|feedback|联系|留言|联系方式|联系我们", "always", 1, False),
    ("offer",   r"product|chanpin|service|fuwu|solution|fangan|jiejue|yewu|/cp/|/fw/|产品|服务|解决方案|业务", "always", 2, True),
    ("case",    r"case|anli|customer|kehu|client|story|gushi|chenggong|案例|客户案例|成功案例|客户", "always", 2, True),
    ("news",    r"news|zixun|xinwen|blog|article|dongtai|ganhuo|资讯|新闻|文章|动态|干货|博客|百科", "always", 2, True),
    ("faq",     r"faq|q&a|\bqa\b|question|wenti|cjwt|changjian|常见问题|问答|有问必答|疑问|帮助中心问答", "always", 1, False),
    ("privacy", r"privacy|yinsi|agreement|xieyi|terms|zhengce|隐私|服务协议|用户协议|条款|政策", "always", 1, False),
    ("help",    r"help|docs|document|manual|guide|support|wiki|apidoc|jiaocheng|shouce|shuoming|帮助|教程|手册|文档|使用说明", "conditional", 1, False),
    ("honor",   r"zizhi|rongyu|honor|cert|qualification|zhengshu|资质|荣誉|证书|专利|软著|实力", "conditional", 1, False),
]

# 旧逻辑的兼容排序（供外部仍想按类型粗排时使用）
_LEGACY_PATTERNS = [
    (0, re.compile(r"product|solution|service|方案|产品|服务|业务", re.I)),
    (1, re.compile(r"case|customer|client|story|案例|客户", re.I)),
    (2, re.compile(r"blog|news|article|post|know|资讯|新闻|文章|百科|问答", re.I)),
    (3, re.compile(r"about|company|contact|关于|联系|简介", re.I)),
]

# 详情页判定：路径里出现纯数字段（如 /article/news/415.html）或以多位数字结尾文件名
_DETAIL_RE = re.compile(r"(/\d{1,6}(?:/|$))|(/\d{1,6}\.(s?html?|asp|aspx|php)$)", re.I)


# 类型识别正则（网址栏目段、链接文字通用）。网址按"/"切段后在段内匹配，
# 段内允许拼音词根连写（geoganhuozixun 段含 zixun、fuwuanli 段含 anli 都能认出）。
_TYPE_RES = [(key, re.compile(pat, re.I)) for key, pat, *_ in TYPE_SPECS]
# 同一对象同时命中多类时的裁决顺序：越具体越优先，offer（产品/服务）最宽泛、垫底
_SPECIFICITY = {"faq": 0, "privacy": 1, "about": 2, "contact": 3, "honor": 4,
                "help": 5, "case": 6, "news": 7, "offer": 8, "other": 99}


class SiteCrawler:
    """从首页出发，按必查类型保底 + 多样化补充，抽样抓取同域典型内页。"""

    def __init__(self, fetcher: Fetcher, max_pages: int = 12, max_workers: int = 4):
        self.fetcher = fetcher
        self.max_pages = max_pages            # 基准下限：至少抓这么多
        # 必查保底占满后，再多抓多少个多样化补充页；以及总数硬上限（防抓取爆炸）
        self.extra_padding = 6
        self.hard_cap = 20
        self.max_workers = max_workers
        # 最近一次选页的覆盖情况：{type: {required, found, selected:[url]}}
        self.last_coverage: Dict[str, dict] = {}
        # 最近一次累计发现的全部内链 {url: 锚文本}（供外层报告/调试取类型）
        self.last_links: dict = {}
        # 站点地图发现的链接缓存（一轮抓取只拉一次 sitemap）
        self._sitemap_links: Optional[dict] = None
        # sitemap posts 型子地图声明的文章页（保底进抽样，供署名/原创性等深度检查）
        self._sitemap_article_urls: set = set()

    @staticmethod
    def _normalize(url: str) -> Optional[str]:
        """规范化 URL：去 fragment，保留 path/query，统一小写域名。"""
        p = urlparse(url)
        if p.scheme not in ("http", "https"):
            return None
        path = p.path or "/"
        if path != "/":
            path = path.rstrip("/") or "/"
        return f"{p.scheme}://{p.netloc}{path}" + (f"?{p.query}" if p.query else "")

    @staticmethod
    def _is_home_alias(norm: str, home_path: str = "/") -> bool:
        """去锚点后是否本质还是首页（/index.php、/index.html 等）。"""
        path = urlparse(norm).path.lower()
        if path in HOME_ALIAS_PATHS:
            return True
        if path == (home_path or "/").lower():
            return True
        return False

    @staticmethod
    def _is_detail(url: str) -> bool:
        """URL 是否像详情页：含数字 id 段/数字文件名，或剔除入口段后语义深度≥3。
        （index.php 这类入口段不算语义层，否则两层的列表页会被误判成详情。）"""
        path = urlparse(url).path
        if _DETAIL_RE.search(path):
            return True
        entry = {"index.php", "index.html", "index.asp", "index.aspx",
                 "default.aspx", "default.html", "home.php", "portal.php"}
        segs = [s for s in path.split("/") if s and s.lower() not in entry]
        return len(segs) >= 3

    @classmethod
    def classify(cls, url: str, anchor: str = "") -> str:
        """判断页面类型：网址栏目段最可靠（先按"/"切段、段内认拼音词根），
        链接文字仅在网址没有任何语义段时兜底；同层命中多类取更具体者。
        这样不会把标题里带"服务商""常见问题"的资讯文章误判成产品页或FAQ页。"""
        def most_specific(hits):
            return sorted(hits, key=lambda k: _SPECIFICITY.get(k, 90))[0] if hits else None

        segments = [s for s in urlparse(url).path.split("/") if s]
        url_hits = set()
        for seg in segments:
            for key, rx in _TYPE_RES:
                if rx.search(seg):
                    url_hits.add(key)
        if url_hits:
            return most_specific(url_hits)
        if anchor:
            anchor_hits = {key for key, rx in _TYPE_RES if rx.search(anchor)}
            if anchor_hits:
                return most_specific(anchor_hits)
        return "other"

    @classmethod
    def homepage_section_types(cls, home) -> set:
        """单页站兜底：从首页自身的区块证据识别页面类型。

        很多官网（尤其外贸单页站）把 关于我们/FAQ/联系 等做成首页锚点区块而非独立页面，
        只按 URL 分类会把它们误报成"缺页"。证据源限定在结构化位置（避免误判正文闲聊）：
        ①页内锚点链接（#about、#faq 及其文字）②区块元素 id ③H1-H3 标题 ④页脚链接文字。
        返回类型集合，供必备页完备性等检查把"首页区块"计入覆盖。"""
        try:
            soup = home.soup
        except Exception:
            return set()
        texts = []
        for a in soup.find_all("a", href=True):
            h = (a.get("href") or "").strip()
            if h.startswith("#") and len(h) > 1:
                texts.append(h[1:].replace("-", " ").replace("_", " "))
            t = a.get_text(strip=True)
            if t:
                texts.append(t[:60])
        for el in soup.find_all(id=True):
            i = (el.get("id") or "").strip()
            if i and len(i) <= 40:
                texts.append(i.replace("-", " ").replace("_", " "))
        for h in soup.find_all(["h1", "h2", "h3"]):
            t = h.get_text(strip=True)
            if t:
                texts.append(t[:80])
        footer = soup.find("footer") or soup.find(class_=re.compile(r"footer", re.I))
        if footer:
            for a in footer.find_all("a"):
                t = a.get_text(strip=True)
                if t:
                    texts.append(t[:60])
        hits = set()
        for t in texts:
            hits |= {key for key, rx in _TYPE_RES if rx.search(t)}
        return hits

    @staticmethod
    def _legacy_type_score(url: str) -> int:
        for score, pat in _LEGACY_PATTERNS:
            if pat.search(url):
                return score
        return 4

    def extract_internal_links(self, home: PageData, extra_pages: List[PageData] = None) -> dict:
        """从首页（及已抓内页）提取同域内链，返回 {规范化url: 锚文本}。

        排序：必查类型按 TYPE_SPECS 顺序优先，同类型列表页在前、详情页在后、再按网址长短；
        其它页面最后。保证后续保底选页时候选顺序是确定的。
        """
        home_p = urlparse(home.final_url)
        home_domain = home_p.netloc
        # 抓取失败的内页以 None 占位存于 fetched，这里必须过滤，否则 page.soup 直接崩溃
        sources = [home] + [p for p in (extra_pages or []) if p is not None]
        links: dict = {}
        for page in sources:
            for a in page.soup.find_all("a", href=True):
                href = (a.get("href") or "").strip()
                if not href or href.startswith(("javascript:", "mailto:", "tel:", "data:")):
                    continue
                # 纯页内锚点（#xxx）直接跳过；带 path 的（/index.php#about）在归一化后按首页同体剔除
                if href.startswith("#"):
                    continue
                full = urljoin(page.final_url, href)
                p = urlparse(full)
                if p.netloc != home_domain:  # 仅同域
                    continue
                norm = self._normalize(full)
                if not norm:
                    continue
                norm_p = urlparse(norm)
                if not norm_p.path:
                    continue
                if self._is_home_alias(norm, home_p.path):
                    continue  # 首页自身及其同体写法不算独立内页
                if RESOURCE_EXT.search(norm_p.path):
                    continue
                if norm not in links:
                    links[norm] = a.get_text(strip=True)[:30]
        type_order = {spec[0]: i for i, spec in enumerate(TYPE_SPECS)}
        type_order["other"] = len(TYPE_SPECS)

        def sort_key(u: str):
            t = self.classify(u, links[u])
            # 列表页(0)排在详情页(1)前
            detail = 1 if self._is_detail(u) else 0
            return (type_order.get(t, 99), detail, len(u))

        ordered = sorted(links.keys(), key=sort_key)
        return {u: links[u] for u in ordered}

    def _select_with_quota(self, links: dict):
        """必查类型保底 + 多样化补充，返回(选中的url列表, 覆盖情况)。"""
        # 分桶
        buckets: Dict[str, List[str]] = {}
        for url, anchor in links.items():
            t = self.classify(url, anchor)
            buckets.setdefault(t, []).append(url)

        coverage: Dict[str, dict] = {}
        selected: List[str] = []
        selected_set = set()

        def take(t: str, want: int, prefer_detail: bool = False):
            """从某类型桶里取 want 个；列表/详情类尽量"列表1+详情(want-1)"形态多样，
            详情缺失时用列表补足，绝不因形态不全而丢掉该类型的保底名额。"""
            urls = buckets.get(t, [])
            picked = []

            def grab(pool):
                for u in pool:
                    if len(picked) >= want:
                        break
                    if u not in selected_set:
                        picked.append(u)
                        selected_set.add(u)

            if prefer_detail:
                lists = [u for u in urls if not self._is_detail(u)]
                details = [u for u in urls if self._is_detail(u)]
                grab(lists[:1])          # 先保证 1 个列表页
                grab(details)            # 再取详情页
                grab(lists[1:])          # 详情不够，用剩余列表页补满
            else:
                grab(urls)
            return picked

        # 第一遍：必查类型保底（按 TYPE_SPECS 顺序）。
        # always（全行业必查）无条件保底；conditional（条件必查）只有站点确实存在时才占名额。
        for key, _pat, required, quota, split in TYPE_SPECS:
            found = buckets.get(key, [])
            if required == "conditional" and not found:
                coverage[key] = {"required": required, "found": 0, "selected": []}
                continue
            picked = take(key, quota, prefer_detail=split)
            selected.extend(picked)
            coverage[key] = {
                "required": required,
                "found": len(found),
                "selected": picked,
            }

        # 名额动态化：总数=max(基准下限, 必查保底实际占用 + 补充名额)，且不超过硬上限。
        # 这样必查页齐全时总数会高于基准、保底页绝不会被基准数砍掉；页面稀缺时补到基准数。
        base_n = len(selected)
        target = max(self.max_pages, min(self.hard_cap, base_n + self.extra_padding))

        # 第二遍：跨类型"轮流取"补足 max_pages——每轮每个还剩候选的类型最多出 1 个，
        # 避免某一类（哪怕它保底顺序靠前、或该站这类页面特别多）把剩余名额一次碾满。
        type_order = {spec[0]: i for i, spec in enumerate(TYPE_SPECS)}
        rem_by_type: Dict[str, List[str]] = {}
        for t, urls in buckets.items():
            pool = [u for u in urls if u not in selected_set]
            if pool:
                rem_by_type[t] = pool
        order = sorted(rem_by_type.keys(), key=lambda t: type_order.get(t, 99))
        while len(selected) < target:
            progressed = False
            for t in order:
                if len(selected) >= target:
                    break
                pool = rem_by_type[t]
                while pool:  # 该类型本轮出 1 个（桶内已按 列表在前/网址长短 排序）
                    u = pool.pop(0)
                    if u in selected_set:
                        continue
                    selected.append(u)
                    selected_set.add(u)
                    progressed = True
                    break
            if not progressed:  # 所有类型都取空
                break

        return selected[:target], coverage

    @staticmethod
    def _strip_www(host: str) -> str:
        return host[4:] if host.lower().startswith("www.") else host

    def _load_sitemap_links(self, home: PageData) -> dict:
        """第三个发现源：解析 /sitemap.xml（支持一层 sitemap-index 嵌套）。
        首页/内页都没链到的"孤岛"必查页（如导航只用页内锚点时的关于页）靠它兜底；
        站点地图里若写的是非 www 主机，统一改写到当前首页主机。"""
        hp = urlparse(home.final_url)
        home_root = self._strip_www(hp.netloc.lower())
        result: dict = {}
        sub_maps = [f"{hp.scheme}://{hp.netloc}/sitemap.xml"]
        seen = set()
        for _ in range(2):  # 最多下钻两层 sitemap-index
            next_maps = []
            for sm in sub_maps:
                if sm in seen:
                    continue
                seen.add(sm)
                resp = self.fetcher.fetch_raw(sm)
                if resp is None or resp.status_code != 200:
                    continue
                text = getattr(resp, "text", "") or ""
                is_index = "<sitemapindex" in text.lower()
                is_posts = bool(re.search(r"posts?-post-\d+", sm))  # posts-post=N文章清单（posts-page=N是独立页面，不算文章）
                for raw in re.findall(r"<loc[^>]*>(.*?)</loc>", text, re.I | re.S):
                    loc = raw.strip()
                    p = urlparse(loc)
                    if self._strip_www(p.netloc.lower()) != home_root:
                        continue  # 只收同注册域
                    if p.path.lower().endswith(".xml"):
                        if is_index:
                            next_maps.append(loc)
                        continue
                    rewritten = f"{hp.scheme}://{hp.netloc}{p.path}"
                    norm = self._normalize(rewritten)
                    if norm and not self._is_home_alias(norm, hp.path):
                        result.setdefault(norm, "")  # sitemap 无锚文本，交给网址段分类
                        if is_posts:
                            self._sitemap_article_urls.add(norm)
            if not next_maps:
                break
            sub_maps = next_maps
        return result

    def _fetch_concurrent(self, urls: List[str]):
        """并发抓取，返回 [(请求url, PageData)]（仅保留 200 且可解析的）。"""
        out = []
        if not urls:
            return out
        with ThreadPoolExecutor(max_workers=self.max_workers) as pool:
            future_map = {pool.submit(self.fetcher.fetch, u): u for u in urls}
            for fut in as_completed(future_map):
                req_u = future_map[fut]
                try:
                    p = fut.result()
                    if p and p.status_code == 200 and p.soup is not None:
                        out.append((req_u, p))
                except Exception:
                    continue
        return out

    def crawl(self, home: PageData, progress=None, rounds: int = 2) -> List[PageData]:
        """两轮发现 + 必查保底抓取，确定性返回 PageData 列表。

        第 1 轮：只从首页取链选保底页；第 2 轮：连同已抓内页（其页脚/导航是全站链接，
        常含首页没直链的关于/隐私/联系/FAQ）再发现一次，补捞必查页。解决"导航用页内
        锚点、必查栏目在首页无直链"的准孤岛问题。"""
        known_links: dict = {}
        fetched: Dict[str, PageData] = {}     # 请求url -> 页面
        final_sel: List[str] = []
        for rnd in range(max(1, rounds)):
            links = self.extract_internal_links(home, list(fetched.values()))
            if self._sitemap_links is None:
                try:
                    self._sitemap_links = self._load_sitemap_links(home)
                except Exception:
                    self._sitemap_links = {}
            # 站点地图先垫底（无锚文本），页面真实链接及其锚文本覆盖在前
            known_links.update(self._sitemap_links)
            known_links.update(links)
            final_sel, coverage = self._select_with_quota(known_links)
            todo = [u for u in final_sel if u not in fetched]
            for u in todo:
                fetched[u] = None  # 占位，避免失败后反复重试
            if progress and todo:
                progress(f"第{rnd + 1}轮发现 {len(known_links)} 个内链，抓取 {len(todo)} 个")
            for req_u, p in self._fetch_concurrent(todo):
                fetched[req_u] = p
            if not todo:
                break

        # 最终覆盖情况基于全部已知链接重算（found=候选数，selected=理想保底名单）
        final_sel, self.last_coverage = self._select_with_quota(known_links)

        # sitemap 声明的文章页保底进抽样：署名/原创性/意图等深度检查需要真实文章页，
        # 不能只落在栏目列表页或模板页上（ZSTEK 教训：sitemap 声明的文章从未被抽查）。
        # 注意放在覆盖重算之后，避免被重算覆盖。
        if self._sitemap_article_urls and not any(
                u in final_sel for u in self._sitemap_article_urls):
            pick = sorted(self._sitemap_article_urls, key=len, reverse=True)[0]
            if fetched.get(pick) is None:
                fetched[pick] = self.fetcher.fetch(pick)
            if fetched.get(pick) is not None:
                final_sel.append(pick)
        self.last_links = dict(known_links)
        # 输出严格对齐最终保底名单，名单外多抓的不返回
        pages, seen = [], set()
        for u in final_sel:
            p = fetched.get(u)
            if p is not None and p.final_url not in seen:
                seen.add(p.final_url)
                pages.append(p)
        order = {spec[0]: i for i, spec in enumerate(TYPE_SPECS)}

        def out_key(p: PageData):
            anchor = known_links.get(
                next((u for u in final_sel if fetched.get(u) is p), p.final_url), "")
            t = self.classify(p.final_url, anchor)
            return (order.get(t, 99), 1 if self._is_detail(p.final_url) else 0, p.final_url)

        pages.sort(key=out_key)
        return pages

    def check_internal_link_status(self, pages: List[PageData], limit: int = 60) -> tuple:
        """检查首页及内页中所有同域链接的 HTTP 状态。

        返回 (broken, bad_schemes)：
        - broken: [(url, status_code, anchor)] 状态异常的同域链接
        - bad_schemes: [(href, is_internal, anchor)] 疑似 http/https 笔误的协议（如 htts://），
          站内链接应视为死链，外链仅提示
        """
        home_domain = urlparse(pages[0].final_url).netloc
        checked = set()
        targets = []
        bad_schemes = []
        for page in pages:
            for a in page.soup.find_all("a", href=True):
                href = (a.get("href") or "").strip()
                if not href or href.startswith(("#", "javascript:", "mailto:", "tel:")):
                    continue
                # 疑似 http/https 笔误的协议（htts/htp/ttps 等）：只由 h/t/p/s 组成且含 h
                m = re.match(r"^([a-z][a-z0-9+.-]*):", href, re.I)
                if m and m.group(1).lower() not in ("http", "https"):
                    s = m.group(1).lower()
                    if len(s) <= 5 and set(s) <= set("htps") and "h" in s:
                        href_host = href.split("://", 1)[-1].split("/")[0].lower()
                        is_internal = href_host in (home_domain,
                                                    home_domain[4:] if home_domain.startswith("www.") else "www." + home_domain)
                        bad_schemes.append((href, is_internal, a.get_text(strip=True)[:20]))
                    continue
                full = urljoin(page.final_url, href)
                p = urlparse(full)
                if p.netloc != home_domain:
                    continue
                norm = self._normalize(full)
                if not norm or norm in checked:
                    continue
                if RESOURCE_EXT.search(urlparse(norm).path):
                    continue
                checked.add(norm)
                targets.append((norm, a.get_text(strip=True)[:20]))
                if len(targets) >= limit:
                    break
        broken = []
        for url, anchor in targets:
            try:
                resp = self.fetcher.fetch_raw(url)
                code = resp.status_code if resp else 0
            except Exception:
                code = 0
            if code == 0 or code >= 400:
                broken.append((url, code, anchor))
        return broken, bad_schemes
