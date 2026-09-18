"""网页抓取层：纯 Requests 方案 + 重试机制 + UA 池。

设计目标：
- 解决百炼 enable_search 只有摘要、拿不到全文的问题
- 国内网络环境优先（Requests 轻量稳定，不依赖浏览器）
- 稳定性优化：UA 池轮换、自动重试、Referer 伪装、并发控制
- 自带内存缓存，避免重复抓取
- 彻底屏蔽需登录的工商网站（企查查/天眼查等），不浪费时间

使用方式：
    from web_fetcher import WebFetcher
    fetcher = WebFetcher()
    # 搜索
    urls = await fetcher.search("深圳极联信息技术股份有限公司 统一社会信用代码", max_results=5)
    # 抓全文
    for url in urls:
        result = await fetcher.fetch(url)
        if result:
            print(result.text[:500])
    await fetcher.close()
"""
from __future__ import annotations
import asyncio
import random
import re
import time
import logging
from dataclasses import dataclass, field
from typing import Optional
from urllib.parse import urlparse, urljoin

import httpx
from bs4 import BeautifulSoup

logger = logging.getLogger("geo-agent.fetcher")

# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------

# UA 池：多个真实浏览器 UA，每次请求随机选择，降低被风控概率
UA_POOL = [
    # Mac Chrome
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    # Windows Chrome
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
    # Mac Safari
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.5 Safari/605.1.15",
    # Edge
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36 Edg/126.0.0.0",
]

# 默认 UA（兼容旧代码）
BROWSER_UA = UA_POOL[0]

# Requests 默认请求头（基础版，UA 会在请求时随机替换）
DEFAULT_HEADERS = {
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
    "Accept-Encoding": "gzip, deflate",
    "Connection": "keep-alive",
}

# 抓取超时（秒）
FETCH_TIMEOUT = 20
SEARCH_TIMEOUT = 25

# 重试次数
MAX_RETRIES = 3
# 重试间隔（秒，指数退避基础值）
RETRY_DELAY = 1.0

# 缓存 TTL（秒），同一 URL 在这个时间内不重复抓取
CACHE_TTL = 3600

# 非正文内容标签（提取纯文本时移除）
NOISE_TAGS = ("script", "style", "noscript", "iframe", "svg", "canvas", "header", "footer", "nav")

# 需登录/强反爬的工商网站黑名单（彻底屏蔽，不浪费时间尝试）
_LOGIN_WALL_DOMAINS = (
    "qcc.com", "tianyancha.com", "qixin.com", "aiqicha.baidu.com",
    "zhipin.com", "jobui.com", "liepin.com", "kanzhun.com",
    "maimai.cn", "linkedin.com", "51job.com", "zhaopin.com",
)


# ---------------------------------------------------------------------------
# 数据类
# ---------------------------------------------------------------------------

@dataclass
class FetchResult:
    """抓取结果。"""
    url: str
    final_url: str
    status_code: int
    title: str
    text: str
    html: str = ""
    fetch_method: str = "requests"
    response_time: float = 0.0


@dataclass
class _CacheEntry:
    result: Optional[FetchResult]
    timestamp: float


# ---------------------------------------------------------------------------
# 工具函数
# ---------------------------------------------------------------------------

def _random_ua() -> str:
    """从 UA 池中随机选择一个 User-Agent。"""
    return random.choice(UA_POOL)


def _decode_html(raw: bytes) -> str:
    """自动检测编码并解码 HTML。"""
    for enc in ("utf-8", "gbk", "gb2312", "gb18030", "latin-1"):
        try:
            return raw.decode(enc)
        except (UnicodeDecodeError, LookupError):
            continue
    return raw.decode("utf-8", errors="replace")


def _extract_text(html: str) -> tuple[str, str]:
    """从 HTML 中提取标题和纯文本正文（去噪）。"""
    soup = BeautifulSoup(html, "lxml")
    # 标题
    title = ""
    if soup.title and soup.title.string:
        title = soup.title.string.strip()
    # 移除噪声标签
    for tag in soup(NOISE_TAGS):
        tag.decompose()
    # 提取正文（按优先级尝试不同选择器）
    # 百度百科专用：.J-lemma-content 是桌面端正文容器
    # 通用：article / main / div.content|article|main
    # 注意：移动端百度百科的正文分散在 body 中，不在特定容器里，
    #       如果选择的容器文本太少（<500字），需要 fallback 到 body
    main = (
        soup.find("div", class_="J-lemma-content")
        or soup.find("article")
        or soup.find("main")
        or soup.find("div", class_=re.compile(r"content|article|main", re.I))
    )
    if main:
        text = main.get_text(separator="\n", strip=True)
        # 如果容器文本太少，可能选错了容器（如移动端百度百科的参考资料div），
        # fallback 到 body 获取完整内容
        if len(text) < 500 and soup.body:
            body_text = soup.body.get_text(separator="\n", strip=True)
            if len(body_text) > len(text):
                text = body_text
    elif soup.body:
        text = soup.body.get_text(separator="\n", strip=True)
    else:
        text = soup.get_text(separator="\n", strip=True)
    # 压缩多余空行
    text = re.sub(r"\n{3,}", "\n\n", text)
    text = re.sub(r"[ \t]+", " ", text)
    return title, text.strip()


def _score_url(url: str) -> int:
    """URL 质量评分：优先选择容易抓取的公开页面。"""
    score = 0
    # 百度百科加分（信息全面，已验证可抓取）
    if "baike.baidu.com" in url:
        score += 30
    # 新闻/资讯网站加分
    if any(d in url for d in ("news.", "sina.", "sohu.", "163.com", "qq.com", "thepaper", "caixin", "36kr", "huxiu", "iyiou")):
        score += 15
    # 企业官网加分
    if any(d in url for d in (".com.cn", ".com/", ".net/", ".org/", ".cn/")) and not any(d in url for d in ("baidu.com", "sogou.com", "so.com")):
        score += 10
    # 百度搜索结果页减分
    if "baidu.com/s?wd" in url or "baidu.com/link?url" in url:
        score -= 20
    # 需登录网站减分
    if any(d in url for d in _LOGIN_WALL_DOMAINS):
        score -= 100
    return score


# ---------------------------------------------------------------------------
# 百度搜索（Requests 实现，不会触发验证码）
# ---------------------------------------------------------------------------

async def _search_baidu_requests(query: str, max_results: int = 5) -> list[str]:
    """用 Requests 搜索百度，返回真实 URL 列表。

    百度搜索结果页是静态 HTML，Requests 可以直接解析，不会触发验证码。
    搜索结果中的链接是百度跳转链接(baidu.com/link?url=...)，需要解析出真实 URL。
    """
    search_url = "https://www.baidu.com/s"
    params = {"wd": query, "rn": max_results * 2}
    headers = {**DEFAULT_HEADERS, "User-Agent": _random_ua()}

    try:
        async with httpx.AsyncClient(timeout=SEARCH_TIMEOUT, follow_redirects=True, headers=headers) as client:
            resp = await client.get(search_url, params=params)
            if resp.status_code != 200:
                logger.warning(f"Baidu search status {resp.status_code} for '{query[:30]}'")
                return []
            html = _decode_html(resp.content)
    except Exception as e:
        logger.warning(f"Baidu search request failed: {e}")
        return []

    # 解析搜索结果中的百度跳转链接
    soup = BeautifulSoup(html, "lxml")
    baidu_links = []
    for a in soup.find_all("a", href=True):
        href = a["href"]
        if "baidu.com/link?url=" in href or href.startswith("/link?url="):
            if href.startswith("/"):
                href = "https://www.baidu.com" + href
            baidu_links.append(href)

    if not baidu_links:
        logger.info(f"Baidu search '{query[:30]}' -> no links found")
        return []

    # 解析百度跳转链接，获取真实 URL
    # 百度跳转链接是 302 重定向，用 httpx 解析（带 Referer 模拟从搜索页点击）
    resolve_headers = {**DEFAULT_HEADERS, "Referer": "https://www.baidu.com/"}
    candidates = []
    seen_real = set()

    async with httpx.AsyncClient(timeout=FETCH_TIMEOUT, follow_redirects=False, headers=resolve_headers) as client:
        for baidu_link in baidu_links:
            if len(candidates) >= max_results * 3:
                break
            try:
                resp = await client.get(baidu_link)
                # 302 跳转的 Location 就是真实 URL
                final = resp.headers.get("Location", "")
                if not final:
                    continue
                # 彻底屏蔽需登录/强反爬的工商网站
                if any(domain in final.lower() for domain in _LOGIN_WALL_DOMAINS):
                    continue
                # 仍然停在百度域且不是跳转链接 = 验证码/安全页，跳过
                if ("baidu.com" in final and "link?url" not in final
                        and "baike.baidu.com" not in final):
                    continue
                score = _score_url(final)
                if score > -50:
                    candidates.append((final, score))
            except Exception:
                # 解析失败，跳过
                continue

    # 按质量评分降序，取 Top N
    candidates.sort(key=lambda x: x[1], reverse=True)
    real_urls = []
    for url, score in candidates:
        if url not in seen_real:
            seen_real.add(url)
            real_urls.append(url)
        if len(real_urls) >= max_results:
            break

    logger.info(f"Baidu search '{query[:30]}...' -> {len(real_urls)} urls")
    return real_urls


async def _search_bing_requests(query: str, max_results: int = 5) -> list[str]:
    """用 Requests 搜索必应（cn.bing.com），返回真实 URL 列表。

    必应搜索结果页是静态 HTML，Requests 可以直接解析，不会触发验证码。
    搜索结果中的链接是真实 URL，不需要解析跳转链接。
    作为百度搜索被限流时的兜底搜索引擎。
    """
    search_url = "https://cn.bing.com/search"
    params = {"q": query, "count": max_results * 2, "setlang": "zh-CN"}
    headers = {**DEFAULT_HEADERS, "User-Agent": _random_ua()}

    try:
        async with httpx.AsyncClient(timeout=SEARCH_TIMEOUT, follow_redirects=True, headers=headers) as client:
            resp = await client.get(search_url, params=params)
            if resp.status_code != 200:
                logger.warning(f"Bing search status {resp.status_code} for '{query[:30]}'")
                return []
            html = _decode_html(resp.content)
    except Exception as e:
        logger.warning(f"Bing search request failed: {e}")
        return []

    # 解析搜索结果中的链接
    soup = BeautifulSoup(html, "lxml")
    candidates = []
    seen_urls = set()

    # 必应搜索结果在 <li class="b_algo"> 中，链接在 <h2><a href="...">
    for li in soup.find_all("li", class_="b_algo"):
        h2 = li.find("h2")
        if not h2:
            continue
        a = h2.find("a", href=True)
        if not a:
            continue
        url = a["href"]
        # 跳过必应自身的链接
        if "bing.com" in url.lower():
            continue
        # 彻底屏蔽需登录/强反爬的工商网站
        if any(domain in url.lower() for domain in _LOGIN_WALL_DOMAINS):
            continue
        if url not in seen_urls:
            seen_urls.add(url)
            score = _score_url(url)
            if score > -50:
                candidates.append((url, score))

    # 也解析一些其他可能的链接（如 <a class="tilk">）
    for a in soup.find_all("a", href=True):
        url = a["href"]
        if not url.startswith("http"):
            continue
        if "bing.com" in url.lower() or "microsoft.com" in url.lower():
            continue
        if any(domain in url.lower() for domain in _LOGIN_WALL_DOMAINS):
            continue
        if url not in seen_urls:
            seen_urls.add(url)
            score = _score_url(url)
            if score > 0:  # 只加质量评分较高的
                candidates.append((url, score))

    if not candidates:
        logger.info(f"Bing search '{query[:30]}' -> no links found")
        return []

    # 按质量评分降序，取 Top N
    candidates.sort(key=lambda x: x[1], reverse=True)
    real_urls = []
    for url, score in candidates:
        if url not in real_urls:
            real_urls.append(url)
        if len(real_urls) >= max_results:
            break

    logger.info(f"Bing search '{query[:30]}...' -> {len(real_urls)} urls")
    return real_urls


# ---------------------------------------------------------------------------
# Requests 抓取器（带重试和 UA 轮换）
# ---------------------------------------------------------------------------

class _RequestsFetcher:
    """用 httpx 抓取，带 UA 池轮换、自动重试、Referer 伪装。"""

    def __init__(self):
        self._client: Optional[httpx.AsyncClient] = None

    async def _ensure_client(self):
        if self._client is None:
            self._client = httpx.AsyncClient(
                timeout=FETCH_TIMEOUT,
                follow_redirects=True,
                headers=DEFAULT_HEADERS,
            )
        return self._client

    async def fetch(self, url: str) -> Optional[FetchResult]:
        """抓取单个 URL，带重试机制。"""
        start = time.time()
        last_error = None

        for attempt in range(MAX_RETRIES):
            try:
                client = await self._ensure_client()
                # 百度百科使用移动端 UA（移动端页面是 SSR，内容更完整；
                # 桌面端页面是 CSR，正文需要 JS 渲染，Requests 拿不到）
                if "baike.baidu.com" in url:
                    headers = {
                        "User-Agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 16_0 like Mac OS X) "
                                      "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/16.0 "
                                      "Mobile/15E148 Safari/604.1",
                        "Referer": "https://www.baidu.com/",
                    }
                else:
                    # 其他网站每次请求随机选择桌面端 UA
                    headers = {"User-Agent": _random_ua()}
                    # 百度跳转链接需要 Referer
                    if "baidu.com/link" in url:
                        headers["Referer"] = "https://www.baidu.com/"

                resp = await client.get(url, headers=headers)
                if resp.status_code >= 400:
                    last_error = f"HTTP {resp.status_code}"
                    # 403/429 可能是风控，重试时换 UA
                    if resp.status_code in (403, 429):
                        await asyncio.sleep(RETRY_DELAY * (attempt + 1))
                        continue
                    return None

                raw = resp.content
                html = _decode_html(raw)
                title, text = _extract_text(html)

                # 内容太少可能是空壳页，重试
                if len(text) < 100:
                    last_error = f"text too short ({len(text)} chars)"
                    await asyncio.sleep(RETRY_DELAY * (attempt + 1))
                    continue

                return FetchResult(
                    url=url, final_url=str(resp.url), status_code=resp.status_code,
                    title=title, text=text, html=html[:500000],
                    fetch_method="requests", response_time=time.time() - start,
                )
            except Exception as e:
                last_error = str(e)
                logger.debug(f"Requests fetch attempt {attempt+1} failed for {url[:60]}: {e}")
                if attempt < MAX_RETRIES - 1:
                    await asyncio.sleep(RETRY_DELAY * (attempt + 1))

        logger.debug(f"Requests fetch failed after {MAX_RETRIES} retries for {url[:60]}: {last_error}")
        return None

    async def close(self):
        if self._client:
            try:
                await self._client.aclose()
            except Exception:
                pass
        self._client = None


# ---------------------------------------------------------------------------
# 统一入口：WebFetcher
# ---------------------------------------------------------------------------

class WebFetcher:
    """网页抓取统一入口：纯 Requests 方案 + 重试 + 缓存。

    稳定性优化：
    - UA 池轮换（7 个真实浏览器 UA，每次请求随机选择）
    - 自动重试（最多 3 次，指数退避，403/429/空壳页自动重试）
    - Referer 伪装（百度百科/百度跳转链接模拟从搜索页点击）
    - 并发控制（最多 3 个并发请求，避免触发风控）
    - 内存缓存（1 小时内不重复抓取同一 URL）
    - 彻底屏蔽需登录的工商网站
    """

    def __init__(self, enable_playwright: bool = True, enable_requests: bool = True):
        # enable_playwright 参数保留为兼容，实际已移除 Playwright
        self._req = _RequestsFetcher() if enable_requests else None
        self._cache: dict[str, _CacheEntry] = {}
        self._cache_lock = asyncio.Lock()
        # 并发控制（避免同时发太多请求触发风控）
        self._sem = asyncio.Semaphore(3)

    async def search(self, query: str, max_results: int = 5) -> list[str]:
        """搜索关键词，返回 URL 列表。

        优先用 Requests 搜索百度，百度被限流/无结果时自动切换必应兜底。
        两个搜索引擎都失败时返回空列表，上层用百炼 enable_search 兜底。
        """
        # 第一优先：百度搜索
        try:
            urls = await _search_baidu_requests(query, max_results)
            if urls:
                return urls
        except Exception as e:
            logger.warning(f"Baidu search failed: {e}")

        # 第二优先：必应搜索兜底（百度被限流/无结果时）
        try:
            urls = await _search_bing_requests(query, max_results)
            if urls:
                logger.info(f"Search '{query[:30]}...' -> Baidu failed, Bing fallback succeeded with {len(urls)} urls")
                return urls
        except Exception as e:
            logger.warning(f"Bing search failed: {e}")

        # 都失败时返回空，上层用百炼 enable_search 兜底
        logger.info(f"Search '{query[:30]}...' -> no urls from both Baidu and Bing, fallback to LLM search")
        return []

    async def fetch(self, url: str, use_cache: bool = True) -> Optional[FetchResult]:
        """抓取单个 URL，带重试 + 缓存。"""
        # 缓存检查
        if use_cache:
            async with self._cache_lock:
                entry = self._cache.get(url)
                if entry and time.time() - entry.timestamp < CACHE_TTL:
                    return entry.result

        # 彻底屏蔽需登录的工商网站，不浪费时间
        if any(domain in url.lower() for domain in _LOGIN_WALL_DOMAINS):
            logger.debug(f"Skipping login-wall site: {url[:60]}")
            if use_cache:
                async with self._cache_lock:
                    self._cache[url] = _CacheEntry(result=None, timestamp=time.time())
            return None

        async with self._sem:
            result = None
            if self._req:
                result = await self._req.fetch(url)

            if result is None:
                logger.debug(f"Fetch failed for {url[:60]}")

        # 写入缓存（包括失败结果，避免重复尝试）
        if use_cache:
            async with self._cache_lock:
                self._cache[url] = _CacheEntry(result=result, timestamp=time.time())

        return result

    async def fetch_many(self, urls: list[str], max_concurrent: int = 3) -> list[Optional[FetchResult]]:
        """并发抓取多个 URL。"""
        sem = asyncio.Semaphore(max_concurrent)

        async def _one(u):
            async with sem:
                return await self.fetch(u)

        return await asyncio.gather(*[_one(u) for u in urls], return_exceptions=False)

    async def search_and_fetch(self, query: str, max_results: int = 3,
                                min_text_length: int = 100) -> list[FetchResult]:
        """搜索 + 抓取一条龙：搜索关键词，抓取 Top N 结果的全文。

        返回成功抓取到的结果列表（按文本长度降序）。
        """
        urls = await self.search(query, max_results=max_results)
        if not urls:
            return []
        results = await self.fetch_many(urls)
        # 过滤失败和内容太少的
        valid = [r for r in results if r and len(r.text) >= min_text_length]
        # 按文本长度降序（内容越丰富越优先）
        valid.sort(key=lambda r: len(r.text), reverse=True)
        return valid

    async def close(self):
        if self._req:
            await self._req.close()

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        await self.close()
