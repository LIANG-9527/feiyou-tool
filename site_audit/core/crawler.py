"""站点多页面爬虫：从首页提取同域内链，按页面类型优先级抽样抓取"""
from __future__ import annotations
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.parse import urljoin, urlparse
from typing import List, Optional

from .fetcher import Fetcher, PageData


# 非 HTML 资源后缀，不抓取
RESOURCE_EXT = re.compile(
    r"\.(pdf|zip|rar|7z|exe|dmg|pkg|png|jpe?g|gif|svg|webp|ico|css|js|"
    r"mp[34]|avi|mov|wmv|flv|docx?|xlsx?|pptx?|txt|xml|json|woff2?|ttf|rar)(\?|$)",
    re.I,
)

# 页面类型优先级关键词（数字越小越优先抓取）
TYPE_PATTERNS = [
    (0, re.compile(r"product|solution|service|方案|产品|服务|业务", re.I)),
    (1, re.compile(r"case|customer|client|story|案例|客户", re.I)),
    (2, re.compile(r"blog|news|article|post|know|资讯|新闻|文章|百科|问答", re.I)),
    (3, re.compile(r"about|company|contact|关于|联系|简介", re.I)),
]


class SiteCrawler:
    """从首页出发，抽样抓取同域典型内页。"""

    def __init__(self, fetcher: Fetcher, max_pages: int = 12, max_workers: int = 4):
        self.fetcher = fetcher
        self.max_pages = max_pages
        self.max_workers = max_workers

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
    def _type_score(url: str) -> int:
        for score, pat in TYPE_PATTERNS:
            if pat.search(url):
                return score
        return 4

    def extract_internal_links(self, home: PageData, extra_pages: List[PageData] = None) -> dict:
        """从首页（及已抓内页）提取同域内链，返回 {规范化url: 锚文本}，按类型优先级排序。"""
        home_p = urlparse(home.final_url)
        home_domain = home_p.netloc
        sources = [home] + (extra_pages or [])
        links: dict = {}
        for page in sources:
            for a in page.soup.find_all("a", href=True):
                href = (a.get("href") or "").strip()
                if not href or href.startswith(("#", "javascript:", "mailto:", "tel:", "data:")):
                    continue
                full = urljoin(page.final_url, href)
                p = urlparse(full)
                if p.netloc != home_domain:  # 仅同域
                    continue
                norm = self._normalize(full)
                if not norm:
                    continue
                norm_p = urlparse(norm)
                if not norm_p.path or norm_p.path == "/" or norm_p.path == home_p.path:
                    continue  # 排除首页自身
                if RESOURCE_EXT.search(norm_p.path):
                    continue
                if norm not in links:
                    links[norm] = a.get_text(strip=True)[:30]
        ordered = sorted(links.keys(), key=lambda u: (self._type_score(u), len(u)))
        return {u: links[u] for u in ordered}

    def crawl(self, home: PageData, progress=None) -> List[PageData]:
        """并发抓取优先级最高的 max_pages 个内页，返回抓取成功的 PageData 列表。"""
        candidate_urls = list(self.extract_internal_links(home).keys())[: self.max_pages]
        if progress:
            progress(f"发现 {len(candidate_urls)} 个候选内页，开始抽样抓取")
        pages: List[PageData] = []
        if not candidate_urls:
            return pages
        with ThreadPoolExecutor(max_workers=self.max_workers) as pool:
            future_map = {pool.submit(self.fetcher.fetch, u): u for u in candidate_urls}
            for fut in as_completed(future_map):
                try:
                    p = fut.result()
                    if p and p.status_code == 200 and p.soup is not None:
                        pages.append(p)
                except Exception:
                    continue
        # 按 URL 类型优先级+URL本身排序，保证完全确定性
        pages.sort(key=lambda p: (self._type_score(p.final_url), p.final_url))
        return pages

    def check_internal_link_status(self, pages: List[PageData], limit: int = 60) -> list:
        """检查首页及内页中所有同域链接的 HTTP 状态，返回 [(url, status_code, anchor)] 的异常列表。"""
        home_domain = urlparse(pages[0].final_url).netloc
        checked = set()
        targets = []
        for page in pages:
            for a in page.soup.find_all("a", href=True):
                href = (a.get("href") or "").strip()
                if not href or href.startswith(("#", "javascript:", "mailto:", "tel:")):
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
        return broken
