"""页面抓取与解析工具"""
import re
import requests
from bs4 import BeautifulSoup
from dataclasses import dataclass
from typing import Optional, Dict, Any
from urllib.parse import urljoin, urlparse


@dataclass
class PageData:
    """抓取到的页面数据"""
    url: str
    final_url: str
    status_code: int
    html: str
    soup: BeautifulSoup
    headers: Dict[str, str]
    response_time: float
    content_size: int  # bytes

    def meta(self, name: str) -> Optional[str]:
        """获取 meta 标签内容（按 name 或 property）"""
        tag = self.soup.find("meta", attrs={"name": name})
        if tag and tag.get("content"):
            return tag["content"].strip()
        tag = self.soup.find("meta", attrs={"property": name})
        if tag and tag.get("content"):
            return tag["content"].strip()
        return None

    def get_text(self) -> str:
        """获取页面纯文本"""
        return self.soup.get_text(separator=" ", strip=True)

    def word_count(self) -> int:
        """粗略统计正文字数（中文按字符，英文按单词）"""
        text = self.get_text()
        # 移除多余空白
        text = re.sub(r"\s+", " ", text)
        return len(text)


class Fetcher:
    """网页抓取器"""

    DEFAULT_HEADERS = {
        "User-Agent": (
            "Mozilla/5.0 (compatible; GEO-AuditBot/1.0; "
            "+https://github.com/geo-audit)"
        ),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
    }

    def __init__(self, timeout: int = 15):
        self.timeout = timeout
        self.session = requests.Session()
        self.session.headers.update(self.DEFAULT_HEADERS)

    def fetch(self, url: str) -> Optional[PageData]:
        """抓取页面，返回 PageData 或 None"""
        if not url.startswith(("http://", "https://")):
            url = "https://" + url
        try:
            import time
            start = time.time()
            resp = self.session.get(url, timeout=self.timeout, allow_redirects=True)
            elapsed = time.time() - start
            resp.encoding = resp.apparent_encoding or "utf-8"
            soup = BeautifulSoup(resp.text, "lxml")
            return PageData(
                url=url,
                final_url=resp.url,
                status_code=resp.status_code,
                html=resp.text,
                soup=soup,
                headers=dict(resp.headers),
                response_time=elapsed,
                content_size=len(resp.content),
            )
        except Exception as e:
            print(f"[抓取失败] {url}: {e}")
            return None

    def fetch_raw(self, url: str) -> Optional[requests.Response]:
        """抓取原始响应（用于 robots.txt、sitemap 等非 HTML）"""
        if not url.startswith(("http://", "https://")):
            url = "https://" + url
        try:
            resp = self.session.get(url, timeout=self.timeout, allow_redirects=True)
            return resp
        except Exception:
            return None

    def exists(self, url: str) -> bool:
        """检查 URL 是否可访问（200）"""
        resp = self.fetch_raw(url)
        return resp is not None and resp.status_code == 200


def get_base_url(url: str) -> str:
    """提取基础 URL（scheme + netloc）"""
    if not url.startswith(("http://", "https://")):
        url = "https://" + url
    parsed = urlparse(url)
    return f"{parsed.scheme}://{parsed.netloc}"
