"""AI语义分析引擎 — 调用LLM完成6项语义层检查"""
from __future__ import annotations
import json
import logging
import time
from typing import Optional

import httpx

from site_audit.core.models import Status
from site_audit.core.fetcher import PageData

logger = logging.getLogger("site_audit.ai_engine")

# Prompt 版本号（质量计划书v1·确定性标准）：任何 check_* 方法的 system/user prompt
# 有改动必须递增版本号；版本号随报告 JSON 输出，前后对比报告以此确认口径一致。
# geo-v1.1（2026-09-03 阿栈）：check_knowledge_graph 新增「LLM 提炼核心品牌词兜底重试」prompt，
# 修复品牌变体漏核心名导致的百科误判（hcqycs 案例：变体只有全名/简称，漏「和创企赢」）。
PROMPT_VERSION = "geo-v1.1"

# 常见地域前缀（省级+主要城市），用于从公司名提炼核心品牌词；按长度降序匹配，长前缀优先
REGION_PREFIXES = tuple(sorted([
    "北京", "上海", "天津", "重庆", "广东", "广西", "河北", "河南", "山东", "山西",
    "江苏", "浙江", "安徽", "福建", "江西", "湖南", "湖北", "四川", "贵州", "云南",
    "陕西", "甘肃", "青海", "辽宁", "吉林", "黑龙江", "海南", "内蒙古", "新疆", "西藏",
    "宁夏", "广州", "深圳", "杭州", "南京", "苏州", "成都", "武汉", "西安", "长沙",
    "郑州", "青岛", "大连", "宁波", "厦门", "佛山", "东莞", "无锡", "合肥", "福州",
    "济南", "沈阳", "珠海", "惠州", "中山", "温州", "泉州", "石家庄", "太原", "南昌",
    "南宁", "贵阳", "昆明", "兰州", "海口", "长春", "哈尔滨", "呼和浩特", "乌鲁木齐",
], key=len, reverse=True))


def _strip_region_prefix(name: str) -> str:
    """去掉公司名开头的地域前缀（省/市名，兼容带「市/省」后缀），剩余不足2字则不剥，避免误伤品牌主体"""
    for prefix in REGION_PREFIXES:
        for cand in (prefix + "市", prefix + "省", prefix):
            if name.startswith(cand) and len(name) - len(cand) >= 2:
                return name[len(cand):]
    return name


def _extract_content(page: PageData, max_chars: int = 6000) -> dict:
    """从页面提取供AI分析的结构化内容摘要（在副本上操作，不影响原始soup）"""
    from bs4 import BeautifulSoup
    soup = BeautifulSoup(page.html, "lxml")
    title = soup.title.get_text(strip=True) if soup.title else ""
    desc = page.meta("description") or ""
    h1s = [h.get_text(strip=True) for h in soup.find_all("h1")][:5]
    h2s = [h.get_text(strip=True) for h in soup.find_all("h2")][:10]
    h3s = [h.get_text(strip=True) for h in soup.find_all("h3")][:15]

    for tag in soup.find_all(["script", "style", "nav", "footer", "header", "aside"]):
        tag.decompose()
    paragraphs = [p.get_text(strip=True) for p in soup.find_all("p") if p.get_text(strip=True)]
    list_items = [li.get_text(strip=True) for li in soup.find_all("li") if li.get_text(strip=True)][:20]

    body_parts = []
    for p in paragraphs:
        if sum(len(x) for x in body_parts) + len(p) > max_chars:
            break
        body_parts.append(p)
    body_text = "\n".join(body_parts)

    cta_texts = []
    for a in soup.find_all("a"):
        text = a.get_text(strip=True)
        href = a.get("href", "")
        if any(kw in text for kw in ["咨询", "联系", "预约", "演示", "试用", "下载", "报价", "方案", "了解", "申请"]):
            cta_texts.append(f"[{text}]({href})")

    return {
        "url": page.final_url,
        "title": title,
        "description": desc,
        "h1_list": h1s,
        "h2_list": h2s,
        "h3_list": h3s,
        "body_text": body_text,
        "list_items": list_items,
        "cta_texts": cta_texts[:10],
        "word_count": page.word_count(),
    }


class AIEngine:
    """LLM驱动的语义分析引擎"""

    def __init__(self, base_url: str, api_key: str, model: str,
                 temperature: float = 0, timeout: int = 60, max_retries: int = 2):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.model = model
        self.temperature = temperature
        self.timeout = timeout
        self.max_retries = max_retries
        self._cache = {}
        # 原始 LLM 调用留痕：[{system, user, response}]，供快照落盘取证
        self.raw_log = []

    def _cache_key(self, system_prompt: str, user_prompt: str) -> str:
        import hashlib
        content = system_prompt + "\x00" + user_prompt
        return hashlib.md5(content.encode("utf-8")).hexdigest()

    def _call_llm(self, system_prompt: str, user_prompt: str) -> Optional[dict]:
        """调用LLM并解析JSON响应（带缓存，相同输入只调用一次）"""
        key = self._cache_key(system_prompt, user_prompt)
        if key in self._cache:
            return self._cache[key]

        result = self._call_llm_raw(system_prompt, user_prompt)
        self._cache[key] = result
        return result

    def _call_llm_raw(self, system_prompt: str, user_prompt: str) -> Optional[dict]:
        """实际调用LLM"""
        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ]
        payload = {
            "model": self.model,
            "messages": messages,
            "temperature": self.temperature,
            "max_tokens": 2000,
            "response_format": {"type": "json_object"},
        }
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        url = f"{self.base_url}/chat/completions"

        for attempt in range(1, self.max_retries + 1):
            try:
                with httpx.Client(timeout=self.timeout) as client:
                    resp = client.post(url, json=payload, headers=headers)
                    resp.raise_for_status()
                    data = resp.json()
                    text = data["choices"][0]["message"]["content"]
                    result = json.loads(text)
                    self.raw_log.append({
                        "system": system_prompt,
                        "user": user_prompt,
                        "response": result,
                    })
                    return result
            except Exception as e:
                logger.warning(f"AI engine attempt {attempt}/{self.max_retries} failed: {e}")
                if attempt < self.max_retries:
                    time.sleep(2 ** attempt)
        self.raw_log.append({
            "system": system_prompt,
            "user": user_prompt,
            "response": None,
            "error": "LLM调用失败（重试耗尽）",
        })
        return None

    @staticmethod
    def _parse_status(status_str: str) -> Status:
        s = status_str.lower().strip()
        if s in ("pass", "good", "ok", "通过", "优秀"):
            return Status.PASS
        if s in ("warn", "warning", "normal", "一般", "注意"):
            return Status.WARN
        return Status.FAIL

    def check_nlp_friendly(self, page: PageData) -> tuple[Status, str, str]:
        """ai-09: NLP/大模型语义友好度"""
        content = _extract_content(page)
        if content["word_count"] < 50:
            return Status.SKIP, "页面内容过少，无法进行语义分析", "正文字数不足50"

        system = '你是大模型语义分析专家。分析网页正文对大模型的理解与摘取友好度。输出JSON：{"status":"pass|warn|fail","summary":"一句话结论","issues":["具体问题1"]}'
        user = f"""分析以下网页内容的NLP友好度：
标题: {content['title']}
描述: {content['description']}

正文:
{content['body_text'][:3000]}

判断标准：
- pass: 概念先定义后展开，实体名称明确无模糊指代，句子简洁，事实陈述完整
- warn: 整体可读但部分段落冗长或指代不清
- fail: 大段绕弯、概念无定义、指代混乱、因果不清

只输出JSON。"""

        result = self._call_llm(system, user)
        if not result:
            return Status.SKIP, "AI分析超时，跳过", "LLM调用失败"
        status = self._parse_status(result.get("status", "warn"))
        summary = result.get("summary", "语义分析完成")
        issues = result.get("issues", [])
        detail = summary + ("。问题: " + "；".join(issues) if issues else "")
        return status, summary, detail

    def check_content_chunking(self, page: PageData) -> tuple[Status, str, str]:
        """ai-10: 内容分块与逻辑组织"""
        content = _extract_content(page)
        if not content["h2_list"] and not content["h3_list"]:
            return Status.WARN, "页面缺少小标题分块，内容组织结构不清晰", "未检测到H2/H3标题，内容可能为大段文字堆砌"

        system = '你是内容结构分析专家。分析网页内容的分块质量与逻辑组织。输出JSON：{"status":"pass|warn|fail","summary":"一句话结论","issues":["具体问题"]}'
        user = f"""分析以下网页的内容分块与逻辑组织：

标题结构:
H1: {content['h1_list']}
H2: {content['h2_list']}
H3: {content['h3_list']}

正文片段:
{content['body_text'][:3000]}

判断标准：
- pass: 每块聚焦单一要点，块间逻辑递进清晰，单块长度适中
- warn: 分块基本合理但少数板块偏离主题或过长
- fail: 标题与内容不对应、逻辑跳跃或无有效分块

只输出JSON。"""

        result = self._call_llm(system, user)
        if not result:
            return Status.SKIP, "AI分析超时，跳过", "LLM调用失败"
        status = self._parse_status(result.get("status", "warn"))
        summary = result.get("summary", "分块分析完成")
        issues = result.get("issues", [])
        detail = summary + ("。问题: " + "；".join(issues) if issues else "")
        return status, summary, detail

    def check_search_intent(self, page: PageData) -> tuple[Status, str, str]:
        """content-07: 搜索意图匹配"""
        content = _extract_content(page)
        if content["word_count"] < 50:
            return Status.SKIP, "页面内容过少，无法判断搜索意图匹配", "正文字数不足50"

        system = '你是搜索引擎意图分析专家。判断页面内容类型与用户搜索意图的匹配度。输出JSON：{"status":"pass|warn|fail","summary":"一句话结论","intent_type":"信息型|比较型|交易型","issues":["问题"]}'
        user = f"""分析以下网页的搜索意图匹配度：

标题: {content['title']}
描述: {content['description']}
H1/H2标题: {content['h1_list'] + content['h2_list']}

正文片段:
{content['body_text'][:3000]}

判断标准：
- pass: 内容类型与核心关键词的搜索意图高度匹配
- warn: 意图基本相关但覆盖不全，或一页混合多种意图不够聚焦
- fail: 内容类型与目标意图明显错位，或无明确主题

只输出JSON。"""

        result = self._call_llm(system, user)
        if not result:
            return Status.SKIP, "AI分析超时，跳过", "LLM调用失败"
        status = self._parse_status(result.get("status", "warn"))
        summary = result.get("summary", "意图分析完成")
        intent = result.get("intent_type", "")
        issues = result.get("issues", [])
        detail = summary + (f"（意图类型: {intent}）" if intent else "") + ("。问题: " + "；".join(issues) if issues else "")
        return status, summary, detail

    def check_originality(self, page: PageData) -> tuple[Status, str, str]:
        """content-02原创性部分: 内容原创性AI判断"""
        content = _extract_content(page)
        if content["word_count"] < 50:
            return Status.SKIP, "页面内容过少，无法判断原创性", "正文字数不足50"

        system = '你是内容质量评估专家。判断网页内容的原创价值。输出JSON：{"status":"pass|warn|fail","summary":"一句话结论","issues":["问题"]}'
        user = f"""分析以下网页内容的原创性：

标题: {content['title']}

正文片段:
{content['body_text'][:3000]}

判断标准：
- pass: 含独到观点、一手数据或原创案例，无明显拼凑洗稿
- warn: 内容原创性一般，部分为通用资料整理，缺少鲜明的一手信息
- fail: 全网通用套话/AI水文，无原创观点和一手数据

只输出JSON。"""

        result = self._call_llm(system, user)
        if not result:
            return Status.SKIP, "AI分析超时，跳过", "LLM调用失败"
        status = self._parse_status(result.get("status", "warn"))
        summary = result.get("summary", "原创性分析完成")
        issues = result.get("issues", [])
        detail = summary + ("。问题: " + "；".join(issues) if issues else "")
        return status, summary, detail

    def check_eeat(self, page: PageData) -> tuple[Status, str, str]:
        """auth-07: E-E-A-T内容可信度信号"""
        content = _extract_content(page)
        soup = page.soup
        author = page.meta("author") or ""
        author_tags = soup.find_all(attrs={"itemprop": "author"})
        author_names = [a.get_text(strip=True) for a in author_tags if a.get_text(strip=True)]
        author_signals = []
        if author:
            author_signals.append(author)
        author_signals.extend(author_names)

        system = '你是E-E-A-T内容可信度评估专家。输出JSON：{"status":"pass|warn|fail","summary":"一句话结论","signals":{"author":"有/无","experience":"有/无","source":"有/无"},"issues":["问题"]}'
        user = f"""分析以下网页的E-E-A-T信号：

标题: {content['title']}
HTML中的作者标签: {author_signals or '未检测到'}

正文片段:
{content['body_text'][:3000]}

判断标准：
- pass: 明确标注作者+资质，有第一手经验/实测描述，关键数据标注来源
- warn: 有作者署名但无资质说明，或部分体现经验但不系统
- fail: 无作者信息、无经验描述、无来源标注，匿名通用文案

只输出JSON。"""

        result = self._call_llm(system, user)
        if not result:
            return Status.SKIP, "AI分析超时，跳过", "LLM调用失败"
        status = self._parse_status(result.get("status", "warn"))
        summary = result.get("summary", "E-E-A-T分析完成")
        signals = result.get("signals", {})
        issues = result.get("issues", [])
        signal_str = "、".join(f"{k}:{v}" for k, v in signals.items()) if signals else ""
        detail = summary + (f"（{signal_str}）" if signal_str else "") + ("。问题: " + "；".join(issues) if issues else "")
        return status, summary, detail

    def check_conversion_path(self, page: PageData) -> tuple[Status, str, str]:
        """b2b-07: 转化引导与决策路径"""
        content = _extract_content(page)
        cta_list = content.get("cta_texts", [])

        system = '你是B端转化路径分析专家。输出JSON：{"status":"pass|warn|fail","summary":"一句话结论","issues":["问题"]}'
        user = f"""分析以下网页的转化引导与决策路径：

标题: {content['title']}

检测到的CTA链接:
{chr(10).join(cta_list) if cta_list else '未检测到明显的转化引导链接'}

正文片段:
{content['body_text'][:2000]}

判断标准：
- pass: CTA匹配内容决策阶段，提供分层转化路径，入口清晰
- warn: 有CTA但形式单一，或位置/话术与决策阶段不完全匹配
- fail: 无任何转化引导，或全程硬推销，用户找不到下一步联系路径

只输出JSON。"""

        result = self._call_llm(system, user)
        if not result:
            return Status.SKIP, "AI分析超时，跳过", "LLM调用失败"
        status = self._parse_status(result.get("status", "warn"))
        summary = result.get("summary", "转化路径分析完成")
        issues = result.get("issues", [])
        cta_count = len(cta_list)
        detail = summary + (f"（检测到{cta_count}个CTA入口）" if cta_count else "（未检测到CTA入口）") + ("。问题: " + "；".join(issues) if issues else "")
        return status, summary, detail

    # ── 品牌名提取 ──

    def extract_brand_name(self, page: PageData) -> str:
        """从页面提取品牌名（简称，用于展示）"""
        names = self.extract_brand_names(page)
        return names[0] if names else ""

    def extract_brand_names(self, page: PageData) -> list[str]:
        """从页面提取品牌名候选列表：公司全名、简称、品牌名"""
        page_text = page.get_text()
        import re
        candidates = []

        # 从copyright提取公司全名（兼容 Copyright © / Copyright & © / © 等格式）
        m = re.search(r"(?:Copyright[^©]{0,5}©\s*\d{4}[-\d]*\s+)([\u4e00-\u9fa5][\u4e00-\u9fa5A-Za-z0-9]{2,30}?)(?:\s+(?:All Rights Reserved|版权所有|粤ICP|备案|\d)|$)", page_text, re.I)
        if not m:
            m = re.search(r"©\s*\d{4}[-\d]*\s+([\u4e00-\u9fa5][\u4e00-\u9fa5A-Za-z0-9]{2,30}?)(?:\s+(?:All Rights Reserved|版权所有|粤ICP|备案|\d)|$)", page_text, re.I)
        if m:
            full_name = m.group(1).strip()
            candidates.append(full_name)
            # 生成简称：去公司类型后缀
            for suffix in ["集团有限公司", "科技股份有限公司", "技术有限公司", "有限公司"]:
                if full_name.endswith(suffix):
                    short = full_name[:-len(suffix)]
                    if short and short not in candidates:
                        candidates.append(short)
                    # 再去地域前缀，得到核心品牌词（如「广东和创企赢企服」→「和创企赢企服」）
                    core = _strip_region_prefix(short)
                    if core and core != short and core not in candidates:
                        candidates.append(core)
                    break

        # 从title提取品牌名
        title = page.soup.title.get_text(strip=True) if page.soup.title else ""
        for sep in [" - ", " _ ", "丨", "｜", "—"]:
            if sep in title:
                brand = title.split(sep)[0].strip()
                if brand and brand not in candidates:
                    candidates.append(brand)
                break

        # 从logo/alt提取
        logo = page.soup.find("img", class_=re.compile(r"logo", re.I))
        if logo:
            alt = (logo.get("alt") or "").strip()
            if alt and alt not in candidates:
                candidates.append(alt)

        # LLM提取补充
        if not candidates:
            system = '从网页信息中提取品牌/公司名称。输出JSON：{"brand":"品牌名"}'
            user = f"标题: {title}\n描述: {page.meta('description') or ''}\n\n正文前200字: {page_text[:200]}\n\n只输出JSON。"
            result = self._call_llm(system, user)
            if result and result.get("brand"):
                candidates.append(result["brand"].strip())

        if not candidates and title:
            candidates.append(title.split()[0] if title else title)

        return candidates

    # ── auth-05: 知识图谱品牌实体 ──

    def _extract_core_brand(self, names: list[str], page: PageData) -> str:
        """LLM 从公司全名提炼核心品牌词：去地域前缀、行业通用后缀、公司类型后缀，
        只保留最独特的品牌识别部分（如「广东和创企赢企服集团有限公司」→「和创企赢」）。
        用于名称变体全部未命中百科时的兜底重试。"""
        title = page.soup.title.get_text(strip=True) if page.soup.title else ""
        system = ('你是品牌名称分析专家。从公司全名提炼核心品牌词：去掉地域前缀（省/市名）、'
                  '行业通用后缀（如企服/科技/网络/实业/咨询/管理）和公司类型后缀（有限公司/集团/股份有限公司等），'
                  '只保留最独特的品牌识别部分，通常2-4个字。输出JSON：{"core_brand":"核心品牌词"}')
        user = (f"公司名称候选: {', '.join(names[:4])}\n网页标题: {title}\n"
                f"网站: {page.final_url}\n\n只输出JSON。")
        result = self._call_llm(system, user)
        if result and result.get("core_brand"):
            core = str(result["core_brand"]).strip()
            if 2 <= len(core) <= 10:
                return core
        return ""

    @staticmethod
    def _query_encyclopedia(name: str) -> tuple:
        """用单个名称查询百科平台，返回 (命中平台列表, 有效通道数)。
        有效通道数=真正拿到可解析响应的查询通道数量（反爬/网络不通不计）。
        调用方据此区分「查证无实体」与「通道全灭没查成」——后者绝不能判无实体。
        通道清单（2026-09-03 阿栈实测本机网络）：百度百科搜索API/item页均遭反爬（安全验证页）、
        维基API超时不可达、必应 site: 搜索可达、头条百科词条直连语义干净（404=无词条）。"""
        import re
        import urllib.parse
        encoded = urllib.parse.quote(name)
        platforms = []
        channels_ok = 0
        ua = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"}

        # 1. 百度百科：搜索API
        try:
            search_url = f"https://baike.baidu.com/api/search?word={encoded}&pn=0&rn=3"
            with httpx.Client(timeout=10, follow_redirects=True,
                              headers={**ua, "Accept": "application/json"}) as client:
                resp = client.get(search_url)
                if resp.status_code == 200 and resp.text.strip():
                    try:
                        data = resp.json()
                        channels_ok += 1
                        if data.get("data", {}).get("result", []):
                            platforms.append("百度百科")
                    except Exception:
                        pass  # 返回非JSON（安全验证页）= 通道被反爬，不计有效
        except Exception:
            pass

        # 1b. 百度百科直接item URL（搜索API失败时回退）；安全验证页/未创建词条不计有效通道
        if "百度百科" not in platforms:
            try:
                baike_url = f"https://baike.baidu.com/item/{encoded}"
                with httpx.Client(timeout=10, follow_redirects=True, headers=ua) as client:
                    resp = client.get(baike_url)
                    if resp.status_code == 200:
                        text = resp.text
                        blocked = ("安全验证" in text[:2000] or "尚未被创建" in text[:5000]
                                   or "还未被创建" in text[:5000])
                        if not blocked and "百度百科" in text and "消歧义" not in text[:200]:
                            channels_ok += 1
                            platforms.append("百度百科")
            except Exception:
                pass

        # 2. 维基百科 (中文)
        try:
            wiki_api = f"https://zh.wikipedia.org/w/api.php?action=query&titles={encoded}&format=json&redirects=1"
            with httpx.Client(timeout=10) as client:
                resp = client.get(wiki_api)
                if resp.status_code == 200:
                    data = resp.json()
                    channels_ok += 1
                    pages = data.get("query", {}).get("pages", {})
                    for page_id, page_info in pages.items():
                        if page_id != "-1" and "missing" not in page_info:
                            platforms.append("维基百科")
                            break
        except Exception:
            pass

        # 3. 头条百科：词条直连（404=无词条，语义干净；200 且标题含名称才算命中）
        if not platforms:
            try:
                with httpx.Client(timeout=10, follow_redirects=True, headers=ua) as client:
                    resp = client.get(f"https://www.baike.com/wiki/{encoded}")
                    if resp.status_code == 200:
                        channels_ok += 1
                        m = re.search(r"<title>([^<]*)</title>", resp.text)
                        if m and name in m.group(1):
                            platforms.append("头条百科")
                    elif resp.status_code == 404:
                        channels_ok += 1  # 404 是有效响应：头条百科确认无此词条
            except Exception:
                pass

        # 4. 必应 site: 搜索兜底（百度百科直连被反爬时的可达通道）；
        # 仅当结果标题包含所查名称才算命中，防止「和（汉语文字）」类误命中
        if "百度百科" not in platforms:
            try:
                q = urllib.parse.quote(f'"{name}" site:baike.baidu.com')
                with httpx.Client(timeout=10, follow_redirects=True, headers=ua) as client:
                    resp = client.get(f"https://cn.bing.com/search?q={q}")
                    if resp.status_code == 200:
                        channels_ok += 1
                        hits = re.findall(
                            r'<h2[^>]*><a[^>]*href="https?://(?:www\.)?baike\.baidu\.com/item/[^"]+"[^>]*>(.*?)</a>',
                            resp.text)
                        for h in hits:
                            if name in re.sub(r"<[^>]+>", "", h):
                                platforms.append("百度百科(via必应)")
                                break
            except Exception:
                pass

        return platforms, channels_ok

    def check_knowledge_graph(self, page: PageData) -> tuple[Status, str, str]:
        """检查品牌在百度百科/维基百科/知识面板中是否有实体条目"""
        names = self.extract_brand_names(page)
        if not names:
            return Status.SKIP, "无法识别品牌名，跳过知识图谱检查", "品牌名提取失败"

        brand_display = names[0]
        found_platforms = []
        found_name = ""
        checked_names = []
        channels_ok = 0

        for name in names[:4]:  # 最多尝试4个名称变体
            checked_names.append(name)
            platforms, ok = self._query_encyclopedia(name)
            channels_ok += ok
            if platforms:
                found_platforms = platforms
                found_name = name
                break

        # 名称变体全部未命中时，LLM 提炼核心品牌词兜底重试一轮
        # （修复 hcqycs 案例：变体只有全名+简称，漏了核心品牌「和创企赢」导致误判无实体）
        if not found_platforms:
            core_brand = self._extract_core_brand(names, page)
            if core_brand and core_brand not in checked_names:
                checked_names.append(core_brand)
                platforms, ok = self._query_encyclopedia(core_brand)
                channels_ok += ok
                if platforms:
                    found_platforms = platforms
                    found_name = core_brand

        # 3. LLM判断品牌知名度
        system = '你是品牌知识图谱分析专家。判断品牌是否在AI知识图谱中有实体条目。输出JSON：{"known":true/false,"confidence":"high/medium/low","note":"说明"}'
        user = f"""品牌名: {brand_display}
名称变体: {", ".join(names[:4])}
网站: {page.final_url}

判断该品牌在AI大模型知识库中是否为已知品牌实体。
- known=true: 品牌在行业内有知名度，AI能识别
- known=false: 品牌过于小众或新成立，AI知识库中可能没有

只输出JSON。"""

        llm_result = self._call_llm(system, user)
        llm_known = False
        llm_note = ""
        if llm_result:
            llm_known = llm_result.get("known", False)
            llm_note = llm_result.get("note", "")

        # 综合判断
        if found_platforms:
            platforms_str = "、".join(found_platforms)
            match_info = f"匹配名称: {found_name}。尝试: {', '.join(checked_names)}" if found_name != brand_display else ""
            if llm_known:
                return (Status.PASS, f"品牌[{found_name}]在{platforms_str}有词条，且AI识别为已知品牌",
                        f"百科平台: {platforms_str}。{match_info}。LLM判断: {llm_note}")
            return (Status.PASS, f"品牌[{found_name}]在{platforms_str}有词条",
                    f"百科平台: {platforms_str}。{match_info}")
        else:
            # 通道全灭（反爬/网络不通）时绝不判无实体：降级为「本次未检」，与业务口径一致不计分
            if channels_ok == 0:
                return (Status.SKIP, "百科查询通道不可用，本次未能核实品牌实体",
                        f"尝试名称: {', '.join(checked_names)}。百度百科(反爬)/维基/必应/头条百科通道均无有效响应，建议人工搜索品牌名核实。LLM判断: {llm_note}")
            if llm_known:
                return (Status.WARN, f"品牌[{brand_display}]无百科词条，但AI识别为已知品牌",
                        f"尝试名称: {', '.join(checked_names)}。LLM判断: {llm_note}")
            return (Status.FAIL, f"品牌[{brand_display}]在百科平台和AI知识图谱中均未检测到实体",
                    f"百科平台均未找到词条（有效通道{channels_ok}个）。尝试名称: {', '.join(checked_names)}。LLM判断: {llm_note}")

    # ── auth-06: 跨平台内容分发 ──

    def check_cross_platform(self, page: PageData) -> tuple[Status, str, str]:
        """检查品牌内容是否跨平台分发（知乎/公众号/行业媒体等）"""
        brand = self.extract_brand_name(page)
        if not brand:
            return Status.SKIP, "无法识别品牌名，跳过跨平台检查", "品牌名提取失败"

        # 1. 从页面检测社交平台链接（已有auth-03检测，这里复用逻辑）
        social_found = []
        soup = page.soup
        for a in soup.find_all("a", href=True):
            href = a["href"].lower()
            text = a.text.strip().lower()
            checks = {
                "微信公众号": ["weixin", "wechat", "公众号", "微信"],
                "知乎": ["zhihu", "知乎"],
                "微博": ["weibo", "微博"],
                "抖音": ["douyin", "抖音"],
                "B站": ["bilibili", "b站"],
                "领英": ["linkedin", "领英"],
            }
            for platform, keywords in checks.items():
                if any(kw in href or kw in text for kw in keywords):
                    if platform not in social_found:
                        social_found.append(platform)

        # 2. 检查知乎是否有品牌内容
        zhihu_found = False
        try:
            import urllib.parse
            zhihu_search = f"https://www.zhihu.com/search?type=content&q={urllib.parse.quote(brand)}"
            with httpx.Client(timeout=10, headers={
                "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"
            }) as client:
                resp = client.get(zhihu_search)
                if resp.status_code == 200 and brand in resp.text:
                    zhihu_found = True
                    if "知乎" not in social_found:
                        social_found.append("知乎(搜索)")
        except Exception:
            pass

        # 3. LLM判断品牌跨平台分发情况
        system = '你是品牌内容分发分析专家。判断品牌的跨平台内容分发情况。输出JSON：{"platforms":["平台1","平台2"],"status":"pass|warn|fail","summary":"结论","issues":["问题"]}'
        user = f"""品牌名: {brand}
网站: {page.final_url}

页面检测到的社交平台链接: {social_found or "无"}

判断标准：
- pass: 核心内容同步到≥3个平台（知乎+公众号+行业媒体），有外链回官网
- warn: 有1-2个平台的内容分发，但部分内容无外链或更新不规律
- fail: 无任何跨平台内容分发，内容仅存在于官网

根据你的知识判断该品牌的跨平台分发情况。只输出JSON。"""

        result = self._call_llm(system, user)
        if not result:
            # 回退到规则判断
            if len(social_found) >= 3:
                return (Status.PASS, f"检测到{len(social_found)}个社交平台: {'、'.join(social_found)}",
                        f"页面社交链接: {', '.join(social_found)}")
            if social_found:
                return (Status.WARN, f"跨平台分发不足，仅有: {'、'.join(social_found)}",
                        f"页面检测到: {', '.join(social_found)}")
            return Status.FAIL, "未检测到跨平台内容分发", "无社交平台链接"

        status = self._parse_status(result.get("status", "warn"))
        summary = result.get("summary", "跨平台分析完成")
        platforms = result.get("platforms", [])
        issues = result.get("issues", [])

        # 合并检测结果
        all_platforms = list(set(social_found + platforms))
        detail_parts = []
        if all_platforms:
            detail_parts.append(f"平台: {', '.join(all_platforms)}")
        if issues:
            detail_parts.append("问题: " + "；".join(issues))
        detail = "。".join(detail_parts) if detail_parts else summary

        return status, summary, detail

    # ── b2b-05: 技术文档/API文档AI友好 ──

    def check_tech_docs(self, page: PageData, all_pages: list = None) -> tuple[Status, str, str]:
        """检查技术文档的可抓取性和AI友好度"""
        all_pages = all_pages or [page]
        import re

        # 1. 规则层：扫描所有页面中是否有文档链接
        doc_keywords = ["文档", "API文档", "开发文档", "技术文档", "帮助文档",
                        "docs", "documentation", "developer", "api参考", "开发者中心"]
        doc_links = []
        for p in all_pages:
            for a in p.soup.find_all("a", href=True):
                text = a.get_text(strip=True)
                href = a["href"]
                if any(kw in text.lower() or kw in href.lower() for kw in doc_keywords):
                    doc_links.append((text[:30], href))

        if not doc_links:
            # 也检查URL中是否有/docs/路径
            for p in all_pages:
                if re.search(r"/docs?/|/api/|/developer", p.final_url, re.I):
                    doc_links.append((p.soup.title.get_text(strip=True)[:30] if p.soup.title else p.final_url, p.final_url))

        if not doc_links:
            return (Status.WARN, "未检测到技术文档/API文档入口",
                    "所有页面均未找到文档链接，B端客户和AI无法获取技术信息")

        # 2. 尝试抓取一个文档页面，检查结构
        from site_audit.core.fetcher import Fetcher
        fetcher = Fetcher(timeout=10)
        doc_page = None
        for _, href in doc_links[:3]:
            try:
                from urllib.parse import urljoin
                full_url = urljoin(page.final_url, href)
                dp = fetcher.fetch(full_url)
                if dp and dp.word_count() > 50:
                    doc_page = dp
                    break
            except Exception:
                continue

        # 3. 规则检查文档结构
        structure_signals = []
        if doc_page:
            soup = doc_page.soup
            # 侧边栏导航
            sidebar = soup.find("nav") or soup.find(class_=re.compile(r"sidebar|menu|toc|目录", re.I))
            if sidebar:
                structure_signals.append("侧边栏目录")
            # 代码块
            code_blocks = soup.find_all("pre") or soup.find_all("code")
            if code_blocks:
                has_lang = any(cb.get("class") or cb.find_parent().get("class") for cb in code_blocks)
                structure_signals.append(f"代码块({len(code_blocks)}个，{'有语言标记' if has_lang else '无语言标记'})")
            # 搜索功能
            search_box = soup.find("input", {"type": "search"}) or soup.find(class_=re.compile(r"search", re.I))
            if search_box:
                structure_signals.append("站内搜索")
            # 标题层级
            h_count = len(soup.find_all(["h1", "h2", "h3"]))
            structure_signals.append(f"标题层级({h_count}个)")

        # 4. LLM评估文档AI友好度
        doc_info = f"找到文档链接: {', '.join(t for t, _ in doc_links[:5])}"
        if structure_signals:
            doc_info += f"。文档结构: {', '.join(structure_signals)}"

        system = '你是技术文档AI友好度评估专家。输出JSON：{"status":"pass|warn|fail","summary":"结论","issues":["问题"]}'
        user = f"""评估以下技术文档的AI友好度：

网站: {page.final_url}
{doc_info}

判断标准：
- pass: 文档有完整侧边栏目录+面包屑，代码块有语言标记，静态HTML可抓取，有搜索功能
- warn: 有基本文档结构，但代码块缺语言标记，或部分内容依赖JS渲染
- fail: 无技术文档，或文档结构混乱无目录，或纯JS渲染无法被AI抓取

根据已有信息判断。只输出JSON。"""

        result = self._call_llm(system, user)
        if not result:
            if structure_signals:
                return (Status.PASS, f"检测到技术文档入口，结构包含: {', '.join(structure_signals)}",
                        doc_info)
            return (Status.WARN, "检测到文档入口但无法评估结构", doc_info)

        status = self._parse_status(result.get("status", "warn"))
        summary = result.get("summary", "技术文档分析完成")
        issues = result.get("issues", [])
        detail = doc_info + ("。问题: " + "；".join(issues) if issues else "")
        return status, summary, detail

    # ── b2b-06: 问答型内容覆盖 ──

    def check_qa_content(self, page: PageData, all_pages: list = None) -> tuple[Status, str, str]:
        """检查网站是否覆盖B端客户常见问题"""
        all_pages = all_pages or [page]
        import re

        # 1. 规则层：检测FAQ结构和高频问题
        faq_signals = []
        qa_count = 0

        # FAQPage Schema
        for p in all_pages:
            scripts = p.soup.find_all("script", type="application/ld+json")
            for s in scripts:
                text = s.get_text()
                if "FAQPage" in text or "faqs" in text.lower():
                    faq_signals.append("FAQPage结构化数据")

        # 常见问题DOM结构
        faq_keywords = ["常见问题", "FAQ", "问答", "Q&A", "问题解答", "帮助中心", "help center"]
        faq_sections = []
        for p in all_pages:
            for kw in faq_keywords:
                elements = p.soup.find_all(string=re.compile(kw, re.I))
                for el in elements:
                    parent = el.parent
                    if parent and parent.name in ("h1", "h2", "h3", "h4", "div", "section"):
                        faq_sections.append((kw, parent.name, p.final_url))
                        break

        # 高频问题关键词匹配
        b2b_questions = {
            "价格/费用": ["价格", "多少钱", "费用", "收费", "报价", "cost", "price", "fee"],
            "部署/使用": ["怎么部署", "如何安装", "怎么用", "使用方法", "deploy", "install", "setup"],
            "安全/合规": ["数据安全", "信息安全", "合规", "隐私保护", "加密", "security", "compliance"],
            "售后/支持": ["售后服务", "技术支持", "客服", "服务保障", "support", "service"],
            "试用/体验": ["免费试用", "体验", "demo", "试用", "trial", "免费体验"],
            "对比/选型": ["对比", "比较", "区别", "哪个好", "选型", "vs", "compare"],
        }

        found_topics = set()
        for p in all_pages:
            text = p.get_text()
            for topic, keywords in b2b_questions.items():
                if any(kw in text for kw in keywords):
                    found_topics.add(topic)
                    qa_count += 1

        # 2. LLM评估问答覆盖度
        covered_str = ", ".join(found_topics) if found_topics else "无"
        faq_str = ", ".join(faq_signals) if faq_signals else "无"
        sections_str = f"{len(faq_sections)}处FAQ板块" if faq_sections else "未检测到FAQ板块"

        system = '你是B端问答内容覆盖度评估专家。输出JSON：{"status":"pass|warn|fail","summary":"结论","covered":["已覆盖话题"],"missing":["缺失话题"],"issues":["问题"]}'
        user = f"""评估以下网站的问答型内容覆盖情况：

网站: {page.final_url}
检测页面数: {len(all_pages)}

FAQ结构化标记: {faq_str}
FAQ板块: {sections_str}
已覆盖话题: {covered_str}

B端客户高频问题维度: 价格/费用, 部署/使用, 安全/合规, 售后/支持, 试用/体验, 对比/选型

判断标准：
- pass: 覆盖≥4个话题维度，有FAQ板块或结构化标记，答案完整
- warn: 覆盖2-3个话题维度，有部分FAQ但不够系统
- fail: 覆盖<2个话题维度，无FAQ或问答内容

根据已有信息判断。只输出JSON。"""

        result = self._call_llm(system, user)
        if not result:
            # 回退到规则判断
            if len(found_topics) >= 4:
                return (Status.PASS, f"问答覆盖{len(found_topics)}个话题: {covered_str}",
                        f"FAQ标记: {faq_str}。板块: {sections_str}")
            if found_topics:
                return (Status.WARN, f"问答覆盖不足，仅有: {covered_str}",
                        f"FAQ标记: {faq_str}。板块: {sections_str}")
            return Status.FAIL, "未检测到问答型内容", f"FAQ标记: {faq_str}。板块: {sections_str}"

        status = self._parse_status(result.get("status", "warn"))
        summary = result.get("summary", "问答覆盖分析完成")
        covered = result.get("covered", [])
        missing = result.get("missing", [])
        issues = result.get("issues", [])

        detail_parts = [f"规则检测覆盖: {covered_str}"]
        if faq_str != "无":
            detail_parts.append(f"FAQ标记: {faq_str}")
        detail_parts.append(f"板块: {sections_str}")
        if missing:
            detail_parts.append(f"LLM判断缺失: {', '.join(missing)}")
        if issues:
            detail_parts.append("问题: " + "；".join(issues))
        detail = "。".join(detail_parts)

        return status, summary, detail
