"""企业画像三层数据管线：查询分解 → 证据池 → 汇聚抽取 → 字段机检 → 合批补查 → 终止规则。

设计（docs/工作计划-企业信息获取流程重构.md）：
- 第一层 工商与组织：必须验证；2 轮补查后核心字段（全称/代码/法人）仍缺 → ProfileError（烧主链前终止）
- 第二层 拓展属性：尽力获取，缺失标待补
- 采信顺序：百度百科摘要 > 权威媒体 > 聚合平台；第一层禁用模型记忆填充，每个值必须带证据编号
- 全部检索走百度源（实测稳定）；证据与结果写入公司缓存库

溯源机制（Data Provenance）：
- 每个字段在 out["_provenance"] 中记录来源和获取方法，便于审计和排查
- 不改变 out[字段名]=值 的现有结构，下游代码无感知
- 报告生成时把溯源信息嵌入 HTML data 属性，不展示但可检查
"""
import asyncio
import time
import json
import re
import logging
from datetime import datetime, timezone

logger = logging.getLogger("geo-agent.profile")

# ---------------------------------------------------------------------------
# 溯源机制常量
# ---------------------------------------------------------------------------

# 数据来源类型
SOURCE_BAIKE = "baike"                    # 百度百科
SOURCE_WEB_FETCH = "web_fetch"            # 其他网站全文抓取
SOURCE_SEARCH_ENGINE = "search_engine"    # 搜索引擎摘要（百炼enable_search）
SOURCE_PRETRAINED = "pretrained"          # LLM预训练参数化知识
SOURCE_INFERRED = "inferred"              # LLM基于多源数据的推断
SOURCE_BUSINESS_API = "business_api"      # 工商数据API（预留，待接入）
SOURCE_UNKNOWN = "unknown"                # 来源未知

# 获取方法类型
METHOD_DIRECT_URL_FETCH = "direct_url_fetch"      # 直接构造URL抓取
METHOD_REQUESTS_UA_POOL = "requests_ua_pool"      # Requests+UA池抓取
METHOD_LLM_SEARCH_SNIPPET = "llm_search_snippet"  # LLM搜索摘要
METHOD_LLM_PARAMETRIC_KNOWLEDGE = "llm_parametric_knowledge"  # LLM参数化知识
METHOD_LLM_REASONING = "llm_reasoning"            # LLM推理
METHOD_API_CALL = "api_call"                        # API调用
METHOD_UNKNOWN = "unknown"                          # 方法未知


def _now_iso() -> str:
    """当前时间的ISO格式字符串。"""
    return datetime.now(timezone.utc).isoformat()


def _set_provenance(out: dict, field: str, source: str, method: str,
                     detail: dict = None, confidence: float = None):
    """设置字段的溯源信息。

    Args:
        out: 企业画像数据dict
        field: 字段名
        source: 数据来源（SOURCE_* 常量）
        method: 获取方法（METHOD_* 常量）
        detail: 方法细节（如URL、查询词、文本长度等）
        confidence: 置信度 0.0-1.0，None表示根据source默认
    """
    if "_provenance" not in out:
        out["_provenance"] = {}

    # 默认置信度
    if confidence is None:
        confidence = {
            SOURCE_BUSINESS_API: 1.0,
            SOURCE_BAIKE: 0.85,
            SOURCE_WEB_FETCH: 0.7,
            SOURCE_SEARCH_ENGINE: 0.5,
            SOURCE_PRETRAINED: 0.3,
            SOURCE_INFERRED: 0.6,
            SOURCE_UNKNOWN: 0.2,
        }.get(source, 0.5)

    out["_provenance"][field] = {
        "source": source,
        "method": method,
        "detail": detail or {},
        "confidence": round(confidence, 2),
        "fetched_at": _now_iso(),
    }


def _get_provenance_summary(out: dict) -> dict:
    """获取溯源信息的统计摘要，用于审计和监控。

    Returns:
        {
            "total_fields": 总数,
            "by_source": {source: count},
            "by_method": {method: count},
            "low_confidence_fields": [置信度<0.5的字段列表],
            "unknown_source_fields": [来源未知的字段列表],
        }
    """
    prov = out.get("_provenance") or {}
    by_source = {}
    by_method = {}
    low_conf = []
    unknown_src = []

    for field, info in prov.items():
        src = info.get("source", SOURCE_UNKNOWN)
        mth = info.get("method", METHOD_UNKNOWN)
        conf = info.get("confidence", 0)

        by_source[src] = by_source.get(src, 0) + 1
        by_method[mth] = by_method.get(mth, 0) + 1

        if conf < 0.5:
            low_conf.append(field)
        if src == SOURCE_UNKNOWN:
            unknown_src.append(field)

    return {
        "total_fields": len(prov),
        "by_source": by_source,
        "by_method": by_method,
        "low_confidence_fields": low_conf,
        "unknown_source_fields": unknown_src,
    }

CREDIT_RE = re.compile(r"^[0-9A-HJ-NPQRTUWXY]{2}\d{6}[0-9A-HJ-NPQRTUWXY]{10}$")

LAYER1 = [
    ("full_name", "公司注册全称", "百度百科 公司简介"),
    ("credit_code", "统一社会信用代码(18位)", "统一社会信用代码"),
    ("legal_person", "法定代表人姓名", "法定代表人 创始人 百度百科"),
    ("founded", "成立时间(年月)", "成立时间 百度百科"),
    ("address", "注册地址/总部地址", "总部地址 注册地址"),
    ("listed", "上市状态(交易所+代码,未上市则写'未上市')", "上市 IPO 股票代码"),
    ("employees", "员工人数(带口径年份)", "员工人数 团队规模"),
    ("industry", "所属行业", "主营业务 所属行业"),
    ("industry_position", "行业地位(排名/市场份额依据)", "行业地位 市场份额"),
]
LAYER2 = [
    ("new_business", "转型方向与新业务/第二曲线(如新能源、自动驾驶、无人配送、企业级服务等)", "转型 第二曲线 新业务"),
    ("competitor_landscape", "主要竞争对手名单(含新兴竞品，如无人配送/自动驾驶类公司)", "竞争对手 竞品 无人配送"),
    ("main_products_geo", "主力产品线及其AI搜索可见性判断依据", "主营业务 产品线"),
    ("founder", "创始人/实际控制人姓名(百度百科优先)", "创始人 董事长 百度百科"),
    ("mission", "企业使命(官方表述)", "企业使命"),
    ("vision", "企业愿景(官方表述)", "企业愿景"),
    ("values", "价值观关键词", "企业价值观"),
    ("qualifications", "资质认证列表(JSON数组)", "资质认证 高新技术企业"),
    ("patents", "代表性专利/软著列表(JSON数组,最多8条)", "专利 软件著作权"),
    ("revenue_detail", "年营收(带年份口径)", "年营收 营业收入"),
]
CORE = ("full_name", "credit_code", "legal_person")

SEARCH_PROMPT = """## 任务：企业事实检索
目标企业：「{company}」
查询问题：{question}
请联网检索（结果含百度百科/公司官网/全国企业信用信息公示系统时优先采信）。
输出 JSON：{{"answer":"针对问题的直接答案(检索不到给空串)","quote":"依据的原文摘录一句话","confidence":"high|low"}}
禁止编造；禁止用模型记忆代替检索结果。只输出 JSON。"""

# 带网页全文上下文的检索prompt：当WebFetcher成功抓到全文时使用，
# 把全文作为证据喂给LLM，避免只有搜索摘要导致信息不全。
SEARCH_PROMPT_WITH_CONTEXT = """## 任务：企业事实检索（基于网页全文）
目标企业：「{company}」
查询问题：{question}

以下是通过搜索引擎找到并抓取的网页全文（按相关度排序，可能包含无关内容，请自行甄别）：

===== 网页全文开始 =====
{context}
===== 网页全文结束 =====

请基于上述网页全文回答问题。如果全文中没有答案，可以补充联网检索，但必须以全文内容为主要依据。
输出 JSON：{{"answer":"针对问题的直接答案(检索不到给空串)","quote":"依据的原文摘录一句话(必须来自上述全文)","confidence":"high|low"}}
禁止编造；禁止用模型记忆代替检索结果。只输出 JSON。"""

CONSOLIDATE_PROMPT = """## 任务：从检索证据中抽取企业字段
目标企业：「{company}」
检索证据（编号 E1..En）：
{evidence}
## 待抽取字段
{fields}
## 采信顺序
百度百科摘要 > 权威媒体(招股书/财报/官网) > 聚合平台内容；证据之间冲突取多数一致者并记 conf=low。
## 规则
1. 只依据证据抽取；证据不足的字段 value 给 null
2. 非空值必须带 evid(证据编号)与 conf(high=多证一致/low=单证)
3. 禁止用模型记忆填值——记忆只能用于判断证据合理性
输出 JSON：{{"fields":{{"字段名":{{"value":...,"evid":"E1","conf":"high"}}}},"missing":["字段名"]}}
只输出 JSON。"""

BAIKE_EXTRACT_PROMPT = """## 任务：从百度百科全文中批量抽取企业工商信息
目标企业：「{company}」

## 百度百科全文
{context}

## 待抽取字段
{fields}

## 规则
1. 只依据百科全文抽取；百科中没有的字段 value 给 null
2. 非空值必须带 quote（原文摘录一句话，必须来自上述百科全文）
3. 禁止用模型记忆填值——记忆只能用于判断证据合理性
4. 成立时间格式统一为"YYYY年MM月"或"YYYY年"；员工人数带口径年份
输出 JSON：{{"fields":{{"字段名":{{"value":...,"quote":"原文摘录"}}}},"missing":["字段名"]}}
只输出 JSON。"""


def _extract_baike_json_from_html(html: str) -> str:
    """从wapbaike页面HTML中提取嵌入的结构化JSON数据，转换成文本。

    根据harness的发现，wapbaike页面的<script>标签中包含结构化JSON：
    {"props":{"pageProps":{"pageData":{
      "lemmaId": ..., "lemmaTitle": ..., "abstract": ...,
      "catalog": [...], "structuredContent": [...]
    }}}}

    Returns:
        转换后的文本，空串表示未找到或解析失败。
    """
    import re
    import json

    # 查找所有<script>标签内容
    script_blocks = re.findall(r"<script[^>]*>(.*?)</script>", html, re.DOTALL)

    for script in script_blocks:
        script = script.strip()
        if not script or len(script) < 100:
            continue

        # 尝试找到JSON对象（以{开头，以}结尾）
        # 查找包含 "lemmaTitle" 或 "lemmaId" 或 "structuredContent" 的JSON
        for keyword in ['"lemmaTitle"', '"lemmaId"', '"structuredContent"', '"pageData"']:
            if keyword not in script:
                continue

            # 尝试从{开始解析JSON
            start = script.find("{")
            if start < 0:
                continue

            # 尝试多种方式提取完整JSON
            # 方式1：直接解析整个script（如果是纯JSON）
            try:
                data = json.loads(script[start:])
                return _baike_json_to_text(data)
            except Exception:
                pass

            # 方式2：查找 __NEXT_DATA__ 或类似的赋值
            match = re.search(r'=\s*(\{.*\})\s*;?\s*$', script, re.DOTALL)
            if match:
                try:
                    data = json.loads(match.group(1))
                    return _baike_json_to_text(data)
                except Exception:
                    pass

            # 方式3：逐字符匹配大括号，找到完整的JSON对象
            depth = 0
            end = -1
            for i, ch in enumerate(script[start:], start=start):
                if ch == '{':
                    depth += 1
                elif ch == '}':
                    depth -= 1
                    if depth == 0:
                        end = i + 1
                        break
            if end > start:
                try:
                    data = json.loads(script[start:end])
                    return _baike_json_to_text(data)
                except Exception:
                    pass

            break  # 找到了关键词但解析失败，不再尝试其他关键词

    return ""


def _baike_json_to_text(data: dict) -> str:
    """将百度百科JSON数据转换成文本格式。

    支持的JSON结构：
    - 直接是 pageData: {"lemmaTitle": ..., "abstract": ..., "structuredContent": [...]}
    - 嵌套在 props.pageProps.pageData 中
    """
    import json

    # 递归查找 pageData
    def find_page_data(obj, depth=0):
        if depth > 5 or not isinstance(obj, dict):
            return None
        if "lemmaTitle" in obj or "structuredContent" in obj or "abstract" in obj:
            return obj
        for key in ("props", "pageProps", "pageData"):
            if key in obj:
                result = find_page_data(obj[key], depth + 1)
                if result:
                    return result
        return None

    page_data = find_page_data(data)
    if not page_data:
        return ""

    parts = []

    # 词条标题
    title = page_data.get("lemmaTitle", "")
    if title:
        parts.append(f"词条标题: {title}")

    # 词条描述
    desc = page_data.get("lemmaDesc", "")
    if desc:
        parts.append(f"词条类型: {desc}")

    # 摘要
    abstract = page_data.get("abstract", "")
    if isinstance(abstract, list):
        abstract = " ".join(str(a) for a in abstract if a)
    if abstract and len(str(abstract)) > 10:
        parts.append(f"摘要: {abstract}")

    # 结构化内容（正文）
    structured = page_data.get("structuredContent", [])
    if isinstance(structured, list) and structured:
        parts.append("正文:")
        for section in structured:
            if isinstance(section, dict):
                # 章节标题
                section_title = section.get("title") or section.get("name") or section.get("sectionTitle") or ""
                if section_title:
                    parts.append(f"## {section_title}")
                # 章节内容
                content = section.get("content") or section.get("text") or section.get("summary") or ""
                if isinstance(content, list):
                    content = " ".join(str(c) for c in content if c)
                if content:
                    parts.append(str(content))
            elif isinstance(section, str):
                parts.append(section)

    # 目录
    catalog = page_data.get("catalog", [])
    if isinstance(catalog, list) and catalog and not structured:
        parts.append("目录:")
        for item in catalog:
            if isinstance(item, dict):
                cat_title = item.get("title") or item.get("name") or ""
                if cat_title:
                    parts.append(f"- {cat_title}")
            elif isinstance(item, str):
                parts.append(f"- {item}")

    text = "\n".join(parts)
    # 清理HTML标签
    import re
    text = re.sub(r"<[^>]+>", "", text)
    text = re.sub(r"\s+", " ", text).strip()

    return text if len(text) > 100 else ""


async def _fetch_baike_wap(name: str) -> str:
    """通过百度百科移动端域名获取完整正文。

    域名: https://wapbaike.baidu.com/item/{名称}
    优点: 反爬宽松（不会403）、能获取完整正文、稳定
    优化: 优先从页面嵌入的结构化JSON中提取内容（更准确），失败则回退到HTML标签清理

    Returns:
        清理后的正文文本，空串表示未获取到。
    """
    import httpx
    import re
    from urllib.parse import quote

    url = f"https://wapbaike.baidu.com/item/{quote(name)}"
    headers = {
        "User-Agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 16_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/16.0 Mobile/15E148 Safari/604.1",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "zh-CN,zh;q=0.9",
    }

    try:
        async with httpx.AsyncClient(follow_redirects=True, timeout=20) as client:
            resp = await client.get(url, headers=headers)
            if resp.status_code != 200:
                logger.debug(f"Baike WAP failed for {name}: HTTP {resp.status_code}")
                return ""

            html = resp.text
            final_url = str(resp.url)

            # 检查是否是百科页面（不是404或安全验证页）
            if "baike.baidu.com" not in final_url and "wapbaike.baidu.com" not in final_url:
                logger.debug(f"Baike WAP redirect to non-baike URL: {final_url}")
                return ""

            # 检查是否是404页
            if "error.html" in final_url or "页面不存在" in html or "404" in final_url:
                logger.debug(f"Baike WAP 404 for {name}")
                return ""

            # 检查是否是安全验证页
            if "安全验证" in html or "百度安全验证" in html:
                logger.debug(f"Baike WAP security verification for {name}")
                return ""

            # 优先从嵌入的结构化JSON中提取内容（更准确）
            json_text = _extract_baike_json_from_html(html)
            if json_text and len(json_text) > 200:
                # 提取页面标题
                title_match = re.search(r"<title>([^<]*)</title>", html)
                title = title_match.group(1) if title_match else name
                logger.info(f"Baike WAP JSON: got {name} -> {len(json_text)} chars (title={title[:30]})")
                return f"--- 百度百科移动端(JSON): {title} ({final_url}) ---\n{json_text}"

            # 回退：清理HTML标签，提取正文
            text = re.sub(r"<script[^>]*>.*?</script>", "", html, flags=re.DOTALL)
            text = re.sub(r"<style[^>]*>.*?</style>", "", text, flags=re.DOTALL)
            text = re.sub(r"<[^>]+>", "\n", text)
            text = re.sub(r"\n\s*\n", "\n", text)
            text = re.sub(r"[ \t]+", " ", text)
            text = text.strip()

            if len(text) < 500:
                logger.debug(f"Baike WAP text too short for {name}: {len(text)} chars")
                return ""

            title_match = re.search(r"<title>([^<]*)</title>", html)
            title = title_match.group(1) if title_match else name
            logger.info(f"Baike WAP HTML: got {name} -> {len(text)} chars (title={title[:30]})")
            return f"--- 百度百科移动端(HTML): {title} ({final_url}) ---\n{text}"

    except Exception as e:
        logger.debug(f"Baike WAP error for {name}: {e}")
        return ""


async def _fetch_baike_api(name: str) -> str:
    """通过百度百科开放API获取词条摘要和信息卡片。

    API地址: https://baike.baidu.com/api/openapi/BaikeLemmaCardApi
    优点: 稳定（不会403）、结构化JSON、无频率限制
    缺点: 只有摘要（约200字）和信息卡片，没有完整正文

    Returns:
        拼接后的文本（摘要 + 信息卡片），空串表示未获取到。
    """
    import httpx
    from urllib.parse import quote

    url = f"https://baike.baidu.com/api/openapi/BaikeLemmaCardApi?scope=103&format=json&appid=379020&bk_key={quote(name)}"
    headers = {
        "User-Agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 16_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/16.0 Mobile/15E148 Safari/604.1",
        "Referer": "https://baike.baidu.com/",
    }

    try:
        async with httpx.AsyncClient(follow_redirects=True, timeout=15) as client:
            resp = await client.get(url, headers=headers)
            if resp.status_code != 200:
                logger.debug(f"Baike API failed for {name}: HTTP {resp.status_code}")
                return ""
            data = resp.json()

            # 检查是否有有效内容
            title = data.get("title", "")
            abstract = data.get("abstract", "")
            card = data.get("card", [])

            if not title and not abstract:
                logger.debug(f"Baike API empty for {name}: no title/abstract")
                return ""

            # 拼接文本
            parts = []
            parts.append(f"词条标题: {title}")
            if abstract:
                parts.append(f"摘要: {abstract}")
            if card:
                parts.append("信息卡片:")
                for item in card:
                    if isinstance(item, dict):
                        field_name = item.get("name", "")
                        values = item.get("value", [])
                        if isinstance(values, list):
                            values_str = "、".join(str(v) for v in values if v)
                        else:
                            values_str = str(values)
                        if field_name and values_str:
                            parts.append(f"  {field_name}: {values_str}")

            # 清理HTML标签
            import re
            text = "\n".join(parts)
            text = re.sub(r"<[^>]+>", "", text)  # 去除HTML标签
            text = re.sub(r"\s+", " ", text).strip()  # 合并空白

            logger.info(f"Baike API: got {name} -> {len(text)} chars (title={title})")
            return text

    except Exception as e:
        logger.debug(f"Baike API error for {name}: {e}")
        return ""


async def _fetch_baike(company: str, brand_alias: str, fetcher) -> str:
    """预抓取百度百科全文，返回拼接后的文本。空串表示未抓到百科。

    策略（按优先级）：
    1. 先用百度百科开放API搜索正确词条名（避免用户输入名称与实际词条名不匹配导致404）
    2. 用正确的词条名访问移动端域名（wapbaike.baidu.com）获取完整正文（稳定不403）
    3. 开放API获取的摘要和信息卡片作为补充（结构化）
    4. 最后尝试直接构造URL抓取PC版HTML完整正文（可能403，失败也不影响）
    5. 全部失败才返回空串

    用品牌别名和企业全名分别尝试，因为百科词条名通常是品牌名不是公司全称。
    """
    # 构建候选名称列表：品牌别名 + 企业全名（简化后）
    candidate_names = []
    if brand_alias and brand_alias.strip():
        candidate_names.append(brand_alias.strip())
    brand_full = _short_brand_name(company)
    if not brand_alias or brand_full != brand_alias.strip():
        candidate_names.append(brand_full)

    # 第一步：用开放API搜索正确词条名，获取摘要和卡片
    api_parts = []
    correct_lemma_names = []  # 从API返回的正确词条名
    seen_api_titles = set()
    for name in candidate_names:
        api_text = await _fetch_baike_api(name)
        if api_text and len(api_text) > 50:
            # 从摘要中提取词条标题（格式："词条标题: XXX"）
            import re
            title_match = re.search(r"词条标题:\s*(.+?)(?:\n|$)", api_text)
            if title_match:
                lemma_name = title_match.group(1).strip()
                if lemma_name and lemma_name not in correct_lemma_names:
                    correct_lemma_names.append(lemma_name)

            title_match = api_text.split("\n")[0] if api_text else ""
            if title_match not in seen_api_titles:
                seen_api_titles.add(title_match)
                api_parts.append(f"--- 百度百科API: {name} ---\n{api_text}")
        if len(api_parts) >= 2:
            break

    # 第二步：用正确的词条名（或候选名称）访问wapbaike获取完整正文
    # 优先用API返回的正确词条名，避免404
    wap_names = correct_lemma_names + [n for n in candidate_names if n not in correct_lemma_names]
    wap_parts = []
    seen_wap_titles = set()
    for name in wap_names:
        wap_text = await _fetch_baike_wap(name)
        if wap_text and len(wap_text) > 500:
            # 提取标题去重
            title_match = wap_text.split("\n")[0] if wap_text else ""
            if title_match not in seen_wap_titles:
                seen_wap_titles.add(title_match)
                # 截断到8000字，避免prompt过长
                wap_parts.append(wap_text[:8000])
        if len(wap_parts) >= 2:
            break

    # 第三步：尝试直接构造URL抓取PC版HTML完整正文（可能403）
    html_parts = []
    if fetcher is not None:
        from urllib.parse import quote
        seen_urls = set()
        for name in wap_names:
            baike_url = f"https://baike.baidu.com/item/{quote(name)}"
            try:
                result = await fetcher.fetch(baike_url)
                if result and result.text and len(result.text) > 200:
                    if "baike.baidu.com" in result.final_url and result.final_url not in seen_urls:
                        if name in result.title or "百度百科" in result.title:
                            seen_urls.add(result.final_url)
                            html_parts.append(f"--- 百度百科全文: {result.title} ({result.final_url}) ---\n{result.text[:5000]}")
                            logger.info(f"Baike HTML fetch: got {name} -> {len(result.text)} chars")
            except Exception as e:
                logger.debug(f"Baike HTML fetch failed for {name}: {e}")
            if len(html_parts) >= 2:
                break

    # 合并所有结果（WAP优先，然后API，最后HTML）
    all_parts = wap_parts + api_parts + html_parts
    if not all_parts:
        logger.info(f"Baike pre-fetch: no baike results for {company}")
        return ""

    context = "\n\n".join(all_parts)
    logger.info(f"Baike pre-fetch: got {len(wap_parts)} WAP + {len(api_parts)} API + {len(html_parts)} HTML pages, {len(context)} chars total")
    if correct_lemma_names:
        logger.info(f"Baike correct lemma names: {correct_lemma_names}")
    return context


async def _extract_from_baike(llm, company: str, context: str, fields: list) -> dict:
    """从百度百科全文中批量抽取字段。返回 {字段名: value}。"""
    from org_graph import _chat_json
    field_lines = "\n".join(f"- {k}: {hint}" for k, hint, _q in fields)
    prompt = BAIKE_EXTRACT_PROMPT.format(company=company, context=context, fields=field_lines)
    try:
        res = await _chat_json(llm, prompt, enable_search=False) or {}
        fields_dict = res.get("fields") or {}
        result = {}
        for k, item in fields_dict.items():
            if isinstance(item, dict) and item.get("value"):
                result[k] = item["value"]
        return result
    except Exception as e:
        logger.debug(f"Baike extract failed: {e}")
        return {}


class ProfileError(Exception):
    """第一层核心字段多轮补查后仍缺失——按约定终止流程并向前端报错。"""

    def __init__(self, missing: list):
        self.missing = missing
        # 核心字段（全称/信用代码/法人）全部缺失，说明公司根本查不到
        core_fields = {"full_name", "credit_code", "legal_person"}
        if core_fields.issubset(set(missing)):
            super().__init__(
                "未找到该企业的工商信息（企业全称、统一社会信用代码、法定代表人等核心字段全部缺失）。"
                "请确认企业名称是否准确，或检查是否已注销/更名。"
            )
        else:
            super().__init__("企业基础信息核验失败，缺少：" + "、".join(missing) +
                             "。已按规则终止生成，请确认企业名称是否准确，或补充资料后重试。")


# ---------------------------------------------------------------------------
# 第三方企业信息站兜底（顺企网/7icp/鱼泡网等，无需登录）
# 借鉴 harness 在小微企业和服务型企业案例中的策略：工商网站全封锁时，
# 用第三方企业信息镜像站作为兜底，这些站不需要登录且信息较全。
# ---------------------------------------------------------------------------

THIRD_PARTY_SITES = [
    ("11467.com", "顺企网"),
    ("7icp.com", "7icp企业信息"),
    ("yupao.com", "鱼泡网"),
    ("mingluji.com", "名录集"),
    ("b2b168.com", "1688企业信息"),
    ("zhuangyi.com", "装一网"),
]


async def _fetch_third_party_business_info(llm, company: str, fetcher, brand_alias: str = None) -> tuple:
    """从第三方企业信息站（顺企网/7icp/鱼泡网等）抓取工商信息。

    策略：用"公司全称 + 站点名"搜索，找到详情页后抓取全文，再用LLM批量抽取字段。

    Returns:
        (fields_dict, context_text): 抽取的字段字典 + 抓取的全文上下文
    """
    if fetcher is None:
        return {}, ""

    brand_full = _short_brand_name(company)
    # 构建搜索词：公司全称 + 各站点名
    search_queries = []
    for domain, name in THIRD_PARTY_SITES:
        search_queries.append(f"{brand_full} {name}")
        search_queries.append(f"{brand_full} {domain}")
    # 品牌别名也搜索
    if brand_alias and brand_alias.strip() and brand_alias.strip() != brand_full:
        for domain, name in THIRD_PARTY_SITES[:3]:  # 别名只搜前3个站，避免过多
            search_queries.append(f"{brand_alias.strip()} {name}")

    # 搜索并抓取，只保留第三方企业信息站的结果
    all_results = []
    seen_urls = set()
    for sq in search_queries:
        if len(all_results) >= 6:
            break
        try:
            results = await fetcher.search_and_fetch(sq, max_results=3, min_text_length=200)
            for r in results:
                url = r.final_url.lower()
                # 只保留第三方企业信息站的结果
                if any(domain in url for domain, _ in THIRD_PARTY_SITES):
                    if r.final_url not in seen_urls and len(r.text) > 300:
                        seen_urls.add(r.final_url)
                        all_results.append(r)
        except Exception as e:
            logger.debug(f"Third-party search failed for '{sq}': {e}")

    if not all_results:
        logger.info(f"Third-party business info: no results for {company}")
        return {}, ""

    # 拼接Top结果的全文
    parts = []
    for i, r in enumerate(all_results[:5]):
        parts.append(f"--- 第三方企业信息站{i+1}: {r.title} ({r.final_url}) ---\n{r.text[:4000]}")
    context_text = "\n\n".join(parts)
    logger.info(f"Third-party business info: got {len(all_results)} pages, {len(context_text)} chars")

    # 用LLM批量抽取LAYER1字段
    field_lines = "\n".join(f"- {k}: {hint}" for k, hint, _q in LAYER1)
    prompt = BAIKE_EXTRACT_PROMPT.format(company=company, context=context_text, fields=field_lines)
    try:
        from org_graph import _chat_json
        res = await _chat_json(llm, prompt, enable_search=False) or {}
        fields_dict = res.get("fields") or {}
        result = {}
        for k, item in fields_dict.items():
            if isinstance(item, dict) and item.get("value"):
                result[k] = item["value"]
        logger.info(f"Third-party extract: {len(result)}/{len(LAYER1)} L1 fields hit")
        return result, context_text
    except Exception as e:
        logger.debug(f"Third-party extract failed: {e}")
        return {}, context_text


def _field_ok(name: str, value) -> bool:
    """逐字段机检规则。"""
    if value in (None, "", [], {}, "null", "None"):
        return False
    s = str(value).strip()
    if len(s) < 2 or s.lower() in ("null", "none", "unknown", "待补"):
        return False
    if name == "credit_code":
        return bool(CREDIT_RE.match(s.upper()))
    if name in ("legal_person", "founder"):
        return 2 <= len(re.sub(r"[\s·]", "", s)) <= 6 and "公司" not in s
    return True


def _fmt_evidence(evidence: list) -> str:
    return "\n".join(f"E{i + 1}. [问:{e['question']}] 答:{e.get('answer', '')}｜摘录:{e.get('quote', '')}"
                     for i, e in enumerate(evidence) if isinstance(e, dict))


def _short_brand_name(company: str) -> str:
    """简化公司全称得到品牌名，用于搜索百度百科等。

    百度百科词条名通常是品牌名（如"货拉拉"），不是公司全称（如"货拉拉科技（北京）有限公司"）。
    用全称搜索永远搜不到百度百科，所以需要简化。
    """
    import re
    name = company
    # 去掉括号及内容（如"（北京）""(深圳)"）
    name = re.sub(r"[（(][^）)]*[）)]", "", name)
    # 去掉公司类型后缀
    for suffix in ("股份有限公司", "有限责任公司", "有限公司", "集团有限公司", "集团"):
        if name.endswith(suffix):
            name = name[: -len(suffix)]
            break
    # 去掉常见行业词（科技/信息/网络/技术等），得到更短的品牌名
    for suffix in ("科技", "信息技术", "信息科技", "网络科技", "技术", "信息", "网络"):
        if name.endswith(suffix) and len(name) > len(suffix) + 2:
            name = name[: -len(suffix)]
            break
    return name.strip()


async def _search_one(llm, company: str, question: str, fetcher=None, brand_alias: str = None) -> dict:
    """单条检索：优先用WebFetcher抓全文喂LLM，失败则退回百炼纯搜索模式。

    brand_alias: 品牌别名（如"货拉拉"），与 company（企业全称）分别搜索百度百科。
    百科词条名通常是品牌名不是公司全称，双名称搜索确保能搜到百科。
    """
    from org_graph import _chat_json

    # 尝试用WebFetcher搜索+抓全文
    context_text = ""
    if fetcher is not None:
        try:
            # 构建搜索词列表：企业全名（简化）+ 品牌别名（如果有且不同）
            search_queries = []
            brand_full = _short_brand_name(company)
            search_queries.append(f"{brand_full} {question}")
            # 品牌别名搜索（百科词条名通常是品牌名）
            if brand_alias and brand_alias.strip() and brand_alias.strip() != brand_full:
                search_queries.append(f"{brand_alias.strip()} {question}")

            # 双名称搜索，合并去重
            all_results = []
            seen_urls = set()
            for sq in search_queries:
                results = await fetcher.search_and_fetch(sq, max_results=2, min_text_length=100)
                for r in results:
                    if r.final_url not in seen_urls:
                        seen_urls.add(r.final_url)
                        all_results.append(r)
                if len(all_results) >= 4:
                    break

            if all_results:
                # 拼接Top结果的全文（每篇截断到3000字，避免prompt过长）
                parts = []
                for i, r in enumerate(all_results[:4]):
                    parts.append(f"--- 网页{i+1}: {r.title} ({r.final_url}) ---\n{r.text[:3000]}")
                context_text = "\n\n".join(parts)
                logger.info(f"_search_one '{question[:20]}': got {len(all_results)} pages, {len(context_text)} chars context (queries={search_queries})")
        except Exception as e:
            logger.debug(f"WebFetcher search_and_fetch failed: {e}")
            context_text = ""

    # 有全文上下文：用带上下文的prompt，保持LLM联网搜索开启（全文为主，搜索补充）
    if context_text:
        prompt = SEARCH_PROMPT_WITH_CONTEXT.format(
            company=company, question=question, context=context_text)
        try:
            # 有全文时保持enable_search=True：全文为主，LLM可在全文不足时自行搜索补充
            res = await _chat_json(llm, prompt) or {}
        except Exception:
            res = {}
    else:
        # 无全文：原模式（百炼enable_search）
        prompt = SEARCH_PROMPT.format(company=company, question=question)
        try:
            res = await _chat_json(llm, prompt) or {}
        except Exception:
            res = {}

    # 根据是否有全文抓取，标记来源和方法
    if context_text:
        src = SOURCE_WEB_FETCH
        mth = METHOD_REQUESTS_UA_POOL
    else:
        src = SOURCE_SEARCH_ENGINE
        mth = METHOD_LLM_SEARCH_SNIPPET

    return {"question": question,
            "answer": str(res.get("answer") or "")[:200],
            "quote": str(res.get("quote") or "")[:160],
            "confidence": res.get("confidence") or "low",
            "has_fulltext": bool(context_text),
            "source": src,
            "method": mth}


async def _consolidate(llm, company: str, evidence: list, fields: list) -> dict:
    """从检索证据中抽取企业字段。基于已有证据池，关闭LLM联网搜索避免忽略证据。"""
    from org_graph import _chat_json
    field_lines = "\n".join(f"- {k}: {hint}" for k, hint, _q in fields)
    prompt = CONSOLIDATE_PROMPT.format(company=company, evidence=_fmt_evidence(evidence), fields=field_lines)
    res = await _chat_json(llm, prompt, enable_search=False) or {}
    merged = res.get("fields") if isinstance(res, dict) else None
    if merged is None:
        merged = res if isinstance(res, list) else {}
    return _norm_merged(merged)


def _norm_merged(merged) -> dict:
    """模型偶发把字段输出成数组（[{k,value,…}] 或 [{字段:…}]），归一成 dict 再消费。"""
    if isinstance(merged, list):
        fixed = {}
        for e in merged:
            if not isinstance(e, dict):
                continue
            if e.get("k") or e.get("field"):
                fixed[str(e.get("k") or e.get("field"))] = e
            else:
                fixed.update(e)
        merged = fixed
    return {k: v for k, v in merged.items() if isinstance(v, dict)} if isinstance(merged, dict) else {}


assert _norm_merged([{"k": "revenue", "value": "1亿"}]) == {"revenue": {"k": "revenue", "value": "1亿"}}
assert _norm_merged([{"revenue": {"value": "1亿"}}]) == {"revenue": {"value": "1亿"}}
assert _norm_merged({"revenue": {"value": "1亿"}, "bad": "x"}) == {"revenue": {"value": "1亿"}}
assert _norm_merged("非结构") == {}


async def build_profile(llm, company: str, on_progress=None,
                        use_web_fetcher: bool = True, brand_alias: str = None,
                         force_refresh: bool = False) -> dict:
    """三层数据管线主体。返回 {字段: 值}；第一层核心字段缺失抛 ProfileError。

    use_web_fetcher=True 时启用网页全文抓取（纯 Requests 方案 + UA 池 + 重试），
    解决百炼 enable_search 只有摘要、信息不全的问题。

    brand_alias: 品牌别名/用户原始输入（如"货拉拉"），与 company（企业全称，如"深圳依时货拉拉科技有限公司"）
    分别用于搜索百度百科。百科词条名通常是品牌名不是公司全称，双名称搜索确保能搜到百科。

    force_refresh=True 时绕过本地数据库缓存，强制调用工商API获取最新信息并更新缓存。
    """
    out = {}
    ev_pool = []  # 证据池（跨层共享，含 L1/L2 全部检索）

    # ---- 性能监控：记录每个节点的耗时 ----
    _timing = {}  # 节点耗时记录
    _t_total_start = time.time()

    def _record_timing(node_name: str, start_time: float):
        """记录节点耗时。"""
        elapsed = time.time() - start_time
        _timing[node_name] = elapsed
        logger.info(f"[Timing] {node_name}: {elapsed:.2f}s")

    # 创建 WebFetcher（纯 Requests 方案，轻量稳定）
    _t_fetcher = time.time()
    fetcher = None
    if use_web_fetcher:
        try:
            from web_fetcher import WebFetcher
            fetcher = WebFetcher()
            logger.info(f"WebFetcher enabled for profile of {company}")
        except Exception as e:
            logger.warning(f"WebFetcher init failed, fallback to LLM-only search: {e}")
            fetcher = None
    _record_timing("WebFetcher初始化", _t_fetcher)

    try:
        # ---- 工商API作为LAYER1主力数据源（优先，稳定可靠）----
        # LAYER1的8/9个字段都可以从工商API获取，不需要自己搜索
        # 先从本地数据库缓存读取，命中则直接使用，避免重复调用API
        _t_cache = time.time()
        api_company_id = None  # 保存企业ID，供后续股东/人员等接口使用
        try:
            from company_cache import get_info, validate_layer1_fields
            cached_info, cached_ts = get_info(company)
            if cached_info and not force_refresh:
                # 检查缓存中是否有核心字段（全称、信用代码、法人）
                has_core = all(cached_info.get(k) for k in ("full_name", "credit_code", "legal_person"))
                cached_l1 = {k: cached_info.get(k) for k, _h, _q in LAYER1 if cached_info.get(k)}
                if has_core and len(cached_l1) >= 5:  # 有核心字段且至少5个L1字段，直接使用缓存
                    logger.info(f"Cache hit: {len(cached_l1)}/9 L1 fields from local DB (updated {cached_ts})")
                    for k, v in cached_l1.items():
                        out[k] = v
                        _set_provenance(out, k, SOURCE_BUSINESS_API, METHOD_API_CALL, detail={
                            "api_provider": "jingxiansuo",
                            "source": "local_cache",
                            "cached_at": cached_ts,
                            "confidence": "high",
                            "note": "本地数据库缓存，来自工商API",
                        }, confidence=0.95)
                        ev_pool.append({
                            "question": f"{k}（本地缓存）",
                            "answer": str(v)[:200],
                            "quote": "本地数据库缓存",
                            "confidence": "high",
                            "has_fulltext": True,
                            "source": SOURCE_BUSINESS_API,
                            "method": METHOD_API_CALL,
                        })
        except ImportError:
            logger.warning("company_cache not available, skipping cache read")
        except Exception as e:
            logger.warning(f"Cache read failed: {e}")
        _record_timing("本地数据库缓存读取", _t_cache)

        # 如果缓存中LAYER1字段不足，或 force_refresh=True，才调用工商API
        _t_api = time.time()
        l1_from_cache = sum(1 for k, _h, _q in LAYER1 if _field_ok(k, out.get(k)))
        has_core_cache = all(_field_ok(k, out.get(k)) for k in ("full_name", "credit_code", "legal_person"))
        should_call_api = force_refresh or (not has_core_cache) or (l1_from_cache < 5)

        if should_call_api:
            if force_refresh:
                logger.info("Force refresh: bypassing cache, calling Business API")
                if on_progress:
                    on_progress("企业档案", "强制更新：工商API获取最新企业基础信息", 0)
            else:
                if on_progress:
                    on_progress("企业档案", "工商API获取企业基础信息", 0)
            try:
                from business_api_client import get_business_api_client, BusinessAPIClient, BusinessAPIError
                api_client = get_business_api_client()

                # 优先用企业全称查询，失败则用品牌别名查询
                api_detail = {}
                query_names = [company]
                if brand_alias and brand_alias.strip() and brand_alias.strip() != company:
                    query_names.append(brand_alias.strip())

                for qname in query_names:
                    try:
                        api_detail = await api_client.get_company_detail_by_name(qname)
                        if api_detail:
                            # 源头闸：接口返回的注册名必须像组织全称，否则是同名公众号/个体户
                            # 等杂项主体（实测"好莱客"命中同名个体户），整包拒收不进画像不入库
                            from company_cache import is_org_name
                            ret_name = str(api_detail.get("companyName") or api_detail.get("name") or "")
                            if not is_org_name(ret_name):
                                logger.warning(f"Business API: '{qname}' 返回主体名「{ret_name}」非注册组织名，拒收")
                                api_detail = {}
                                continue
                            logger.info(f"Business API: found company via '{qname}'")
                            api_company_id = api_detail.get("id")
                            break
                    except BusinessAPIError as e:
                        logger.warning(f"Business API query failed for '{qname}': {e}")
                        if e.status_code in (401, 402, 403):
                            break  # 鉴权失败/过期/用量不足，不再重试

                if api_detail:
                    # 从 API 返回中提取 LAYER1 字段（画像/校验用）；入库另带全量原始字段
                    raw_fields = BusinessAPIClient.extract_raw_fields(api_detail)
                    api_fields = BusinessAPIClient.extract_layer1_fields(api_detail)
                    # 校验字段有效性
                    from company_cache import validate_layer1_fields, upsert_info
                    valid_fields, validate_errors = validate_layer1_fields(api_fields)
                    if validate_errors:
                        logger.info(f"Field validation: {len(valid_fields)} valid, {len(validate_errors)} errors: {validate_errors}")

                    api_hit = 0
                    for k, _h, _q in LAYER1:
                        v = valid_fields.get(k)
                        if _field_ok(k, v):
                            # API 是金标准，永远覆盖其他来源
                            out[k] = v
                            _set_provenance(out, k, SOURCE_BUSINESS_API, METHOD_API_CALL, detail={
                                "api_provider": "jingxiansuo",
                                "query_name": qname,
                                "company_id": api_company_id,
                                "confidence": "high",
                                "note": "工商API返回，权威数据源",
                            }, confidence=0.95)
                            ev_pool.append({
                                "question": f"{k}（工商API）",
                                "answer": str(v)[:200],
                                "quote": "工商API返回",
                                "confidence": "high",
                                "has_fulltext": True,
                                "source": SOURCE_BUSINESS_API,
                                "method": METHOD_API_CALL,
                            })
                            api_hit += 1
                    logger.info(f"Business API: {api_hit}/{len(LAYER1)} L1 fields filled")

                    # 全量工商字段保存到本地数据库（原始垫底、LAYER1 加工覆盖在上）
                    merged = {**raw_fields, **valid_fields}
                    if merged:
                        try:
                            upsert_info(company, merged, credit_code=merged.get("credit_code"), origin="客户市场调研")
                            logger.info(f"Saved {len(valid_fields)} L1 fields to local DB")
                        except Exception as e:
                            logger.warning(f"Failed to save to local DB: {e}")
                else:
                    logger.info(f"Business API: company not found for {company}")
            except ImportError:
                logger.warning("Business API client not available, skipping")
            except Exception as e:
                logger.warning(f"Business API integration failed: {e}")
        else:
            logger.info(f"Skipping Business API call: {l1_from_cache}/9 L1 fields from cache")
        _record_timing("工商API调用", _t_api)

        # ---- 预抓取百度百科：L1补缺 + L2拓展字段（使命/愿景/竞品等）的共同来源 ----
        # 任一层有缺就抓；L1批量抽取只在L1缺时执行
        baike_context = ""
        _t_baike = time.time()
        l1_missing_before_baike = [(k, q) for k, _h, q in LAYER1 if not _field_ok(k, out.get(k))]
        l2_missing_before_baike = [k for k, _h, _q in LAYER2 if not _field_ok(k, out.get(k))]
        if l1_missing_before_baike or l2_missing_before_baike:
            if on_progress:
                on_progress("企业档案", f"预抓取百度百科（工商字段缺{len(l1_missing_before_baike)}个、拓展字段缺{len(l2_missing_before_baike)}个）", 0)
            baike_context = await _fetch_baike(company, brand_alias, fetcher)
            if baike_context and l1_missing_before_baike:
                if on_progress:
                    on_progress("企业档案", "从百度百科批量抽取工商字段", 0)
                baike_fields = await _extract_from_baike(llm, company, baike_context, LAYER1)
                baike_hit = 0
                for k, _h, _q in LAYER1:
                    v = baike_fields.get(k)
                    if _field_ok(k, v) and not _field_ok(k, out.get(k)):
                        # 只填充API没有覆盖的字段，不覆盖API的值
                        out[k] = v
                        _set_provenance(out, k, SOURCE_BAIKE, METHOD_DIRECT_URL_FETCH, detail={
                            "url": "https://baike.baidu.com/item/" + _short_brand_name(company),
                            "ua_type": "mobile",
                            "extracted_by": "llm_batch_extract",
                            "context_length": len(baike_context),
                        }, confidence=0.85)
                        ev_pool.append({
                            "question": f"{k}（百度百科批量抽取）",
                            "answer": str(v)[:200],
                            "quote": "百度百科全文",
                            "confidence": "high",
                            "has_fulltext": True,
                            "source": SOURCE_BAIKE,
                            "method": METHOD_DIRECT_URL_FETCH,
                        })
                        baike_hit += 1
                logger.info(f"Baike batch extract: {baike_hit}/{len(l1_missing_before_baike)} missing L1 fields filled")
        _record_timing("百度百科预抓取", _t_baike)

        # ---- 第三方企业信息站兜底（顺企网/7icp/鱼泡网等，无需登录）----
        # 借鉴 harness 策略：工商网站全封锁时，用第三方企业信息镜像站作为兜底。
        # 只在百科未覆盖全部L1字段时触发，避免不必要的搜索。
        _t_third_party = time.time()
        l1_after_baike = [(k, q) for k, _h, q in LAYER1 if not _field_ok(k, out.get(k))]
        third_party_context = ""
        if l1_after_baike and fetcher is not None:
            if on_progress:
                on_progress("企业档案", "第三方企业信息站兜底抓取（顺企网/7icp/鱼泡网）", 0)
            tp_fields, third_party_context = await _fetch_third_party_business_info(
                llm, company, fetcher, brand_alias=brand_alias)
            tp_hit = 0
            for k, _h, _q in LAYER1:
                v = tp_fields.get(k)
                if _field_ok(k, v) and not _field_ok(k, out.get(k)):
                    out[k] = v
                    _set_provenance(out, k, SOURCE_WEB_FETCH, METHOD_REQUESTS_UA_POOL, detail={
                        "source_type": "third_party_business_site",
                        "sites": [name for _, name in THIRD_PARTY_SITES],
                        "context_length": len(third_party_context),
                        "note": "第三方企业信息镜像站兜底抓取",
                    }, confidence=0.65)
                    ev_pool.append({
                        "question": f"{k}（第三方企业信息站兜底）",
                        "answer": str(v)[:200],
                        "quote": "第三方企业信息站全文",
                        "confidence": "medium",
                        "has_fulltext": True,
                        "source": SOURCE_WEB_FETCH,
                        "method": METHOD_REQUESTS_UA_POOL,
                    })
                    tp_hit += 1
            logger.info(f"Third-party business info: {tp_hit}/{len(l1_after_baike)} missing L1 fields filled")
        _record_timing("第三方企业信息站兜底", _t_third_party)

        # ---- 第一层：工商与组织——核心字段机检收尾（API/百科/第三方站已覆盖，不再搜索与补查）----
        missing_core = [k for k in CORE if not _field_ok(k, out.get(k))]
        if missing_core:
            raise ProfileError(missing_core)


        # ---- 第二层：拓展属性（尽力获取，失败标待补）----
        # 百科全文批量抽取 → 仍缺的字段逐条搜索一次 → 汇聚抽取；不做多轮补查
        _t_l2 = time.time()
        if baike_context:
            baike_l2 = await _extract_from_baike(llm, company, baike_context, LAYER2)
            for k, _h, _q in LAYER2:
                v = baike_l2.get(k)
                if _field_ok(k, v):
                    out[k] = v
                    # 标记溯源信息
                    _set_provenance(out, k, SOURCE_BAIKE, METHOD_DIRECT_URL_FETCH, detail={
                        "url": "https://baike.baidu.com/item/" + _short_brand_name(company),
                        "ua_type": "mobile",
                        "extracted_by": "llm_batch_extract",
                        "context_length": len(baike_context),
                    }, confidence=0.85)
                    ev_pool.append({
                        "question": f"{k}（百度百科批量抽取）",
                        "answer": str(v)[:200],
                        "quote": "百度百科全文",
                        "confidence": "high",
                        "has_fulltext": True,
                        "source": SOURCE_BAIKE,
                        "method": METHOD_DIRECT_URL_FETCH,
                    })

        # 只搜索百科未覆盖的缺失字段
        l2_missing = [(k, q) for k, _h, q in LAYER2 if not _field_ok(k, out.get(k))]
        if on_progress:
            on_progress("企业档案", f"第二层：拓展信息核验（百科已覆盖{len(LAYER2)-len(l2_missing)}个，待搜{len(l2_missing)}个）", 0)
        if l2_missing:
            results = await asyncio.gather(*[_search_one(llm, company, q, fetcher=fetcher, brand_alias=brand_alias) for _k, q in l2_missing])
            ev_pool.extend(results)
        merged = await _consolidate(llm, company, ev_pool, LAYER2)
        for k, _h, _q in LAYER2:
            item = merged.get(k) or {}
            if _field_ok(k, item.get("value")):
                out[k] = item["value"]
                # 根据证据来源标记溯源信息（如果该字段还没有溯源信息）
                if k not in (out.get("_provenance") or {}):
                    evid_idx = item.get("evid", "")
                    evid_source = SOURCE_SEARCH_ENGINE  # 默认来源：搜索引擎
                    evid_method = METHOD_LLM_SEARCH_SNIPPET  # 默认方法：搜索摘要
                    evid_detail = {}
                    if evid_idx.startswith("E"):
                        try:
                            idx = int(evid_idx[1:]) - 1
                            if 0 <= idx < len(ev_pool):
                                ev = ev_pool[idx]
                                # 优先使用证据中已标记的 source/method
                                if ev.get("source") and ev.get("source") != SOURCE_UNKNOWN:
                                    evid_source = ev["source"]
                                    evid_method = ev.get("method", METHOD_UNKNOWN)
                                elif ev.get("has_fulltext"):
                                    evid_source = SOURCE_WEB_FETCH
                                    evid_method = METHOD_REQUESTS_UA_POOL
                                else:
                                    evid_source = SOURCE_SEARCH_ENGINE
                                    evid_method = METHOD_LLM_SEARCH_SNIPPET
                                evid_detail = {
                                    "evidence_index": evid_idx,
                                    "evidence_question": ev.get("question", "")[:100],
                                    "confidence_from_llm": item.get("conf", "low"),
                                }
                        except (ValueError, IndexError):
                            pass
                    _set_provenance(out, k, evid_source, evid_method, detail=evid_detail,
                                     confidence=0.7 if evid_source == SOURCE_WEB_FETCH else 0.5)
        _record_timing("LAYER2字段搜索和整合", _t_l2)

        out["_evidence_count"] = len(ev_pool)
        # 统计有多少条证据来自全文抓取
        out["_fulltext_evidence"] = sum(1 for e in ev_pool if isinstance(e, dict) and e.get("has_fulltext"))
        # 抓取器状态标识：success=成功抓到全文 / failed=启动了但没抓到全文 / disabled=未启用
        if use_web_fetcher and fetcher is not None:
            out["_fetcher_status"] = "success" if out["_fulltext_evidence"] > 0 else "failed"
        else:
            out["_fetcher_status"] = "disabled"

        # 溯源信息统计摘要
        out["_provenance_summary"] = _get_provenance_summary(out)
        logger.info(f"Provenance summary: {out['_provenance_summary']}")

        # ---- 性能监控：总耗时和各节点耗时 ----
        _total_elapsed = time.time() - _t_total_start
        _timing["总耗时"] = _total_elapsed
        out["_timing"] = _timing  # 保存到输出中，供后续分析
        logger.info(f"[Timing] 总耗时: {_total_elapsed:.2f}s")
        logger.info(f"[Timing] 各节点耗时: {json.dumps({k: f'{v:.2f}s' for k, v in _timing.items()}, ensure_ascii=False)}")

        return out
    finally:
        # 确保关闭 HTTP 客户端
        if fetcher:
            try:
                await fetcher.close()
            except Exception:
                pass
