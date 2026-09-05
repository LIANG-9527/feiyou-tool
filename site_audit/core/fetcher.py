"""页面抓取与解析工具"""
import re
import requests
from bs4 import BeautifulSoup
from dataclasses import dataclass
from typing import Optional, Dict, Any
from urllib.parse import urljoin, urlparse


# 现代汉语高频常用字（用于分辨"正确解码的中文"与"错码后形似汉字的乱码"）
_COMMON_CHARS = set(
    "的一是了我不人在他有这个上们来到时大地为子中你说生国年着就那和要她出也得里后自以会家可下而过天去能对小多然于心学么之都好看起发当没成只如事把还用第样道想作种开美总从无情己面最女但现前些所同日手又行意动方期它头经长儿回位分爱老因很给名法间知世什两次使身者被高已亲其进此话常活正感见明问力理点文本公特做外孩相西果走将月十实向声车全信重三机工物气每并别真打太新比才便夫再书部水像眼等体却加电主界门利海受听表德少克代员许先口由死安写性马光白或住难望教命花结乐色更拉东神记处让母父直字友夜雨题司介绍团队规模荣誉质证书品牌数字营销介绍关联系我们产品服务解决方案案例资讯新闻常见问题隐私政策关于址邮箱话备经营许可")


def _cjk_count(text):
    return sum(1 for ch in text if 0x4E00 <= ord(ch) <= 0x9FFF)


def _common_count(text):
    return sum(1 for ch in text if ch in _COMMON_CHARS)


def _garbage_count(text):
    bad = 0
    for ch in text:
        o = ord(ch)
        # 西里尔区间、替换符、C1 控制符都是解码错乱的典型信号
        if (0x0400 <= o <= 0x04FF) or o == 0xFFFD or (0x0080 <= o <= 0x009F):
            bad += 1
    return bad


def _decode_score(text):
    # 常用字命中权重最高（乱码全是生僻字），其次汉字总量，硬扣乱码字
    return 3 * _common_count(text) + _cjk_count(text) - 5 * _garbage_count(text)


def _norm_enc(e):
    if not e:
        return None
    e = str(e).lower().replace("_", "-")
    return {"gb2312": "gb18030", "gbk": "gb18030"}.get(e, e)


def decode_html(raw: bytes) -> str:
    """多候选编码择优解码，避免中文页面被误判编码而乱码。

    候选：页面 meta 声明 -> utf-8 -> gb18030 -> chardet 猜测；
    按"常用汉字命中多、乱码字符少"打分取最佳（错码产生的生僻汉字会被压下去）。"""
    candidates = []
    m = re.search(rb'charset=["\']?([A-Za-z0-9_\-]+)', raw[:4096])
    if m:
        candidates.append(_norm_enc(m.group(1).decode("ascii", "ignore")))
    candidates += ["utf-8", "gb18030"]
    try:
        import chardet
        guess = (chardet.detect(raw) or {}).get("encoding")
        candidates.append(_norm_enc(guess))
    except Exception:
        pass
    best, best_score = None, None
    seen = set()
    for enc in candidates:
        if not enc or enc in seen:
            continue
        seen.add(enc)
        try:
            text = raw.decode(enc)
        except (UnicodeDecodeError, LookupError):
            continue
        score = _decode_score(text)
        if best_score is None or score > best_score:
            best, best_score = text, score
    return best if best is not None else raw.decode("utf-8", "replace")


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
            html = decode_html(resp.content)
            soup = BeautifulSoup(html, "lxml")
            return PageData(
                url=url,
                final_url=resp.url,
                status_code=resp.status_code,
                html=html,
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

    def head_status(self, url: str):
        """只取状态码的 HEAD 请求（用于 HEAD/GET 一致性检查）。失败返回 None。"""
        if not url.startswith(("http://", "https://")):
            url = "https://" + url
        try:
            resp = self.session.head(url, timeout=self.timeout, allow_redirects=True)
            return resp.status_code
        except Exception:
            return None

    def probe(self, url: str):
        """不跟随跳转，返回 (状态码, Location)；用 stream 避免下载正文。失败返回 (None, None)。"""
        if not url.startswith(("http://", "https://")):
            url = "https://" + url
        try:
            resp = self.session.get(url, timeout=self.timeout,
                                    allow_redirects=False, stream=True)
            code = resp.status_code
            loc = resp.headers.get("Location", "") or ""
            resp.close()
            return code, loc
        except Exception:
            return None, None


def get_base_url(url: str) -> str:
    """提取基础 URL（scheme + netloc）"""
    if not url.startswith(("http://", "https://")):
        url = "https://" + url
    parsed = urlparse(url)
    return f"{parsed.scheme}://{parsed.netloc}"
