"""AI语义分析引擎 — 调用LLM完成6项语义层检查"""
from __future__ import annotations
import json
import logging
import time
from datetime import datetime
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

# 阿里云百炼 qwen3.8-flash 官方价格（人民币/百万tokens，2026-09-11 核对百炼控制台价目表，
# 国内华北2·北京，0<Token≤1M 单档）：输入 0.8，输出 2.7（非思考/思考同价），
# 输入·隐式命中缓存 0.1（官方标「上下文缓存享有折扣」）。缓存命中取隐式口径，对应 cached_tokens。
# 百炼无 DeepSeek 那种峰谷分时，故高峰/空闲两表同值、仅保留分时桶结构以兼容旧报告。
# 价格如官方调整，改下面两组常量即可。
PRICE_PEAK = {   # 百炼无峰谷，此表与空闲表同值
    "input_hit": 0.1,      # 输入·隐式命中缓存
    "input_miss": 0.8,     # 输入·缓存未命中
    "output": 2.7,         # 输出（非思考/思考同价）
}
PRICE_OFFPEAK = {  # 与高峰同值：百炼按量计费，不分时段
    "input_hit": 0.1,
    "input_miss": 0.8,
    "output": 2.7,
}


def _is_peak(dt) -> bool:
    """百炼无峰谷分时，恒为 False（保留函数以兼容 estimate_cost_cny 的旧调用路径）。"""
    return False


def _price_for_hour(hour_key: str) -> dict:
    """按调用时刻返回价格表 {input_hit, input_miss, output}（元/百万tokens）。

    百炼为按量计费、不分时段，两表同值；保留 hour_key 结构以兼容历史报告的分时桶数据。
    """
    try:
        datetime.strptime(hour_key, "%Y-%m-%dT%H")
    except (ValueError, TypeError):
        return PRICE_PEAK
    return PRICE_PEAK


def estimate_cost_cny(tu: dict, ts_hint: str = None, search_price_per_call: float = 0.0) -> float:
    """按阿里云百炼官方价格估算 token 费用（元）。

    优先用 usage_by_hour 分时桶逐桶查价（旧报告数据兼容）；
    旧报告无分时数据时，用 ts_hint（报告时间戳，"YYYY-MM-DD HH:MM:SS"）。
    百炼不分峰谷，两表同值。
    无缓存细分按全部未命中计价（上界，不会低估成本）。
    search_price_per_call：联网搜索每次单价（元），乘 tu["search_calls"] 计入；
    0=不计费但用量照常记录（单价以实际账单校准后由调用方传入）。
    """
    if not tu:
        return 0.0
    total = search_price_per_call * tu.get("search_calls", 0)
    by_hour = tu.get("usage_by_hour")
    if by_hour:
        for hour_key, b in by_hour.items():
            p = _price_for_hour(hour_key)
            total += (b.get("miss", 0) / 1_000_000 * p["input_miss"]
                      + b.get("hit", 0) / 1_000_000 * p["input_hit"]
                      + b.get("completion", 0) / 1_000_000 * p["output"])
        return round(total, 4)
    # 旧数据兜底：按报告时间戳判断峰谷
    if ts_hint:
        try:
            dt = datetime.strptime(ts_hint[:19], "%Y-%m-%d %H:%M:%S")
            p = PRICE_PEAK if _is_peak(dt) else PRICE_OFFPEAK
        except ValueError:
            p = PRICE_PEAK
    else:
        p = PRICE_PEAK
    miss = tu.get("prompt_cache_miss_tokens")
    if miss is None:
        miss = tu.get("prompt_tokens", 0)
        hit = 0
    else:
        hit = tu.get("prompt_cache_hit_tokens", 0)
    completion = tu.get("completion_tokens", 0)
    cost = total + (miss / 1_000_000 * p["input_miss"]
                    + hit / 1_000_000 * p["input_hit"]
                    + completion / 1_000_000 * p["output"])
    return round(cost, 4)

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
                 temperature: float = 0, timeout: int = 60, max_retries: int = 2,
                 enable_search: bool = False, enable_thinking: bool = False):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.model = model
        self.temperature = temperature
        self.timeout = timeout
        self.max_retries = max_retries
        # 联网搜索开关：开启后 LLM 调用可实时联网核实品牌/百科等外部信息（百炼支持）
        self.enable_search = enable_search
        # 深度思考开关：开启思维链推理（qwen3.8-flash 思考/非思考同价）
        self.enable_thinking = enable_thinking
        self._cache = {}
        # 缓存开关：consensus 多轮判定时第 2..n 轮关闭缓存，确保真调 LLM 而非复读第 1 轮结果
        self._cache_enabled = True
        # 原始 LLM 调用留痕：[{system, user, response}]，供快照落盘取证
        self.raw_log = []
        # Token 用量累计（每次成功调用 LLM 都累加 usage 字段），用于报告成本展示
        self.token_usage = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0, "calls": 0}
        # AI 深度检查页数预算（A2 决议）：全站爬取后规则类检查覆盖全站，
        # LLM 深度分析最多评估 8 个不同页面，防止成本随站点规模膨胀
        self.max_ai_pages = 8
        self._ai_pages_seen = set()
        # 外部数据通道原始响应留痕：[{check, channel, query, url, status_code, snippet, verdict}]，
        # 随快照落盘为 search_raw.json（对齐老周凭证链要求：外部判定必须可重放取证）
        self.search_raw = []

    def _page_budget_ok(self, page) -> bool:
        """A2 护栏：LLM 深度分析最多评估 max_ai_pages 个不同页面。
        超出预算返回 False，调用方按 SKIP 处理（规则类检查已覆盖该页）。"""
        key = getattr(page, "final_url", None) or str(page)
        if key in self._ai_pages_seen:
            return True
        if len(self._ai_pages_seen) >= self.max_ai_pages:
            return False
        self._ai_pages_seen.add(key)
        return True

    def _cache_key(self, system_prompt: str, user_prompt: str) -> str:
        import hashlib
        content = system_prompt + "\x00" + user_prompt
        return hashlib.md5(content.encode("utf-8")).hexdigest()

    def _accumulate_usage(self, usage: dict):
        """累加一次成功调用的 token 用量。

        总量字段（prompt/completion/total/hit/miss/calls）供展示；
        usage_by_hour 分时桶（本地时间"YYYY-MM-DDTHH"）供未来峰谷/分时定价对账，
        hour_key 含日期，可衍生工作日/周末、节假日等任意定价维度。
        """
        self.token_usage["prompt_tokens"] += usage.get("prompt_tokens", 0)
        self.token_usage["completion_tokens"] += usage.get("completion_tokens", 0)
        self.token_usage["total_tokens"] += usage.get("total_tokens", 0)
        # 缓存命中字段：DeepSeek 用 prompt_cache_hit_tokens；百炼用 prompt_tokens_details.cached_tokens
        hit = (usage.get("prompt_cache_hit_tokens")
               or (usage.get("prompt_tokens_details") or {}).get("cached_tokens", 0) or 0)
        miss = usage.get("prompt_cache_miss_tokens")
        if miss is None:
            miss = max(usage.get("prompt_tokens", 0) - hit, 0)
        self.token_usage["prompt_cache_hit_tokens"] = (
            self.token_usage.get("prompt_cache_hit_tokens", 0) + hit)
        self.token_usage["prompt_cache_miss_tokens"] = (
            self.token_usage.get("prompt_cache_miss_tokens", 0) + miss)
        self.token_usage["calls"] += 1
        hour = datetime.now().strftime("%Y-%m-%dT%H")
        buckets = self.token_usage.setdefault("usage_by_hour", {})
        b = buckets.setdefault(hour, {"hit": 0, "miss": 0, "completion": 0})
        b["hit"] += hit
        b["miss"] += miss
        b["completion"] += usage.get("completion_tokens", 0)

    def _call_llm(self, system_prompt: str, user_prompt: str) -> Optional[dict]:
        """调用LLM并解析JSON响应（带缓存，相同输入只调用一次；consensus 轮次可绕过）"""
        key = self._cache_key(system_prompt, user_prompt)
        if self._cache_enabled and key in self._cache:
            return self._cache[key]

        result = self._call_llm_raw(system_prompt, user_prompt)
        if self._cache_enabled:
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
            **({"enable_search": True} if self.enable_search else {}),
            # 百炼 qwen3.8 系列默认开思考，关闭须显式传 false，不传等于没关
            **({"enable_thinking": bool(self.enable_thinking)} if self.enable_search else {}),
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
                    # 累加 token 用量（总量字段 + 按小时分时桶，供峰谷定价对账）
                    usage_data = data.get("usage") or {}
                    self._accumulate_usage(usage_data)
                    self.raw_log.append({
                        "system": system_prompt,
                        "user": user_prompt,
                        "response": result,
                        "usage": usage_data,
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

    @staticmethod
    def _majority_status(statuses: list) -> Status:
        """软性项多轮取多数结论：fail 一票否决（保守最严）+ 多数档

        规则（按优先级）：
          1) 含 fail → 取 fail（质量保守，宁可判严不放过）
          2) ≥2 一致 → 取该
          3) 全异或平局无 fail → 取 warn（中间档）
        """
        from collections import Counter
        if not statuses:
            return Status.WARN
        if Status.FAIL in statuses:
            return Status.FAIL
        c = Counter(statuses)
        most, n = c.most_common(1)[0]
        if n >= 2:
            return most
        return Status.WARN

    def consensus(self, call_fn, n: int = 3) -> tuple[Status, str, str]:
        """对软性LLM判定项做 n 轮取多数结论，降低 LLM 非确定性抖动

        call_fn: 无参 callable，返回 (Status, summary, detail)
        返回: (多数结论status, 首轮的summary, 首轮的detail)

        注意：第 1 轮走正常缓存；第 2..n 轮关闭缓存真调 LLM（相同 prompt 若命中缓存
        会直接复读第 1 轮答案，"多轮取多数"就退化成单次判定）。
        """
        runs = []
        try:
            for i in range(n):
                if i > 0:
                    self._cache_enabled = False
                try:
                    runs.append(call_fn())
                except Exception as e:
                    logger.warning(f"consensus round {i+1}/{n} 异常: {e}")
                    runs.append((Status.WARN, "", f"本轮判定异常: {e}"))
        finally:
            self._cache_enabled = True
        statuses = [r[0] for r in runs]
        consensus_status = self._majority_status(statuses)
        _, sm, dt = runs[0]
        # 记录多轮分布到 raw_log，便于审计
        try:
            self.raw_log.append({
                "type": "consensus",
                "n": n,
                "rounds": [s.value for s in statuses],
                "consensus": consensus_status.value,
            })
        except Exception:
            pass
        return consensus_status, sm, dt

    def check_nlp_friendly(self, page: PageData) -> tuple[Status, str, str]:
        if not self._page_budget_ok(page):
            return (Status.SKIP, "已达AI深度检查页数预算上限（8页），本页未做深度分析（规则类检查已覆盖）", "")
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
        if not self._page_budget_ok(page):
            return (Status.SKIP, "已达AI深度检查页数预算上限（8页），本页未做深度分析（规则类检查已覆盖）", "")
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
        if not self._page_budget_ok(page):
            return (Status.SKIP, "已达AI深度检查页数预算上限（8页），本页未做深度分析（规则类检查已覆盖）", "")
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

    def check_pair_duplicate(self, page_a: PageData, page_b: PageData) -> tuple[Status, str, str]:
        """跨页比对终审（C 方案核心）：问 AI 两页是不是'同一份内容换标题/换皮'。

        机器粗筛只标"疑似"；此方法对疑似对做终审，判定"真重复 / 不算重复 / 存疑"。
        结果只用于报告提示，绝不静默删页。
        """
        ca = _extract_content(page_a)
        cb = _extract_content(page_b)
        if ca.get("word_count", 0) < 50 or cb.get("word_count", 0) < 50:
            return Status.SKIP, "页面内容过少，无法比对", "两页中至少一页正文不足50字"

        system = ("你是一名内容审核专家。判断两页内容是否是'同一份内容换标题/换皮'（正文核心信息一致，"
                  "只是标题/头尾/排版换了一下）。只输出JSON："
                  '{"verdict":"duplicate|not_duplicate|uncertain","confidence":"high|medium|low","reason":"一句话理由"}')
        user = f"""对比以下两页正文的核心内容是否实质相同：

【页 A】标题: {ca['title']}
正文片段: {ca['body_text'][:1500]}

【页 B】标题: {cb['title']}
正文片段: {cb['body_text'][:1500]}

判定：
- duplicate: 两页正文核心信息一致，明显是同一份内容换标题/换皮（重复）
- not_duplicate: 两页内容主题相关但信息不同，是正常的不同内容页
- uncertain: 信息部分重叠但无法确定，或正文太少/模糊（存疑）

只输出JSON。"""
        result = self._call_llm(system, user)
        if not result:
            return Status.SKIP, "AI比对超时，暂存疑", "LLM调用失败"
        verdict = result.get("verdict", "uncertain")
        if verdict == "duplicate":
            return Status.WARN, f"AI判定两页为重复内容（{result.get('confidence','')}置信度）", result.get("reason", "")
        if verdict == "not_duplicate":
            return Status.PASS, "AI判定两页为不同内容", result.get("reason", "")
        return Status.SKIP, "AI判定存疑，需人工复核", result.get("reason", "")

    def check_originality(self, page: PageData) -> tuple[Status, str, str]:
        if not self._page_budget_ok(page):
            return (Status.SKIP, "已达AI深度检查页数预算上限（8页），本页未做深度分析（规则类检查已覆盖）", "")
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
        if not self._page_budget_ok(page):
            return (Status.SKIP, "已达AI深度检查页数预算上限（8页），本页未做深度分析（规则类检查已覆盖）", "")
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
        if not self._page_budget_ok(page):
            return (Status.SKIP, "已达AI深度检查页数预算上限（8页），本页未做深度分析（规则类检查已覆盖）", "")
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

        # 2026-09-10 V6：英文站品牌在标题尾段（… – ZSTEK），剥英文法律后缀
        _en_legal = re.compile(r"[,，\s]*(co\.)?\s*(ltd|limited|inc|llc|llp|corp|corporation|company|gmbh)\.?$", re.I)

        def _en_strip(nm):
            nm = (nm or "").strip()
            while True:
                new = _en_legal.sub("", nm).strip().rstrip(",，").strip()
                if new == nm or not new:
                    return nm
                nm = new

        title = page.soup.title.get_text(strip=True) if page.soup.title else ""
        for sep in [" - ", " – ", " _ ", "丨", "｜", "—"]:
            if sep in title:
                brand = _en_strip(title.split(sep)[0].strip())
                tail = _en_strip(title.split(sep)[-1].strip())
                for cand in (brand, tail):
                    if cand and cand not in candidates:
                        candidates.append(cand)
                break
        # 2026-09-10 V6：英文站——标题里与域名匹配的词根是品牌候选（zstek.com → ZSTEK），放最前
        from urllib.parse import urlparse as _up
        _host = _up(page.final_url or "").netloc.lower().replace("www.", "")
        if _host:
            for w in re.findall(r"[A-Za-z][A-Za-z0-9]{2,}", title):
                if len(w) >= 3 and w.lower() in _host and w not in candidates:
                    candidates.insert(0, w)

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

    def _record_search_raw(self, channel: str, name: str, url: str,
                           status: int, snippet: str, verdict: str):
        """记录外部数据通道原始响应（百科品牌实体检查），随快照落盘为 search_raw.json。"""
        self.search_raw.append({
            "check": "百科品牌实体",
            "channel": channel,
            "query": name,
            "url": url,
            "status_code": status,
            "snippet": snippet[:2000],
            "verdict": verdict,
        })

    def _query_encyclopedia(self, name: str) -> tuple:
        """用单个名称查询百科平台，返回 (命中平台列表, 有效通道数)。
        有效通道数=真正拿到可解析响应的查询通道数量（反爬/网络不通不计）。
        调用方据此区分「查证无实体」与「通道全灭没查成」——后者绝不能判无实体。
        主通道：百度百科词条页 wap 端直连（2026-09-03 阿栈实测：桌面端 item 页/search API
        均遭反爬 403 安全验证，wap 端 wapbaike.baidu.com 直连可达且语义干净——
        200 且页面含所查名称=命中，404=确认无词条）。
        每通道 10s 超时上限并计入整单时间预算；各通道原始响应记入 search_raw 留痕。"""
        import re
        import urllib.parse
        encoded = urllib.parse.quote(name)
        platforms = []
        channels_ok = 0
        ua = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"}

        def _strip(html: str) -> str:
            return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", html))[:1500]

        # 0. 百度百科词条页 wap 端直连（主通道，命中即通过）
        try:
            wap_url = f"https://wapbaike.baidu.com/item/{encoded}"
            with httpx.Client(timeout=10, follow_redirects=True, headers=ua) as client:
                resp = client.get(wap_url)
                text = resp.text
                if resp.status_code == 200 and "安全验证" not in text[:3000]:
                    channels_ok += 1
                    hit = name in text
                    self._record_search_raw(
                        "百度百科(wap词条页直连)", name, wap_url, resp.status_code,
                        _strip(text), "命中" if hit else "未命中(200但页面不含名称)")
                    if hit:
                        platforms.append("百度百科")
                elif resp.status_code == 404:
                    channels_ok += 1  # 404 是有效响应：百度百科确认无此词条
                    self._record_search_raw(
                        "百度百科(wap词条页直连)", name, wap_url, 404, "", "确认无词条(404)")
                else:
                    self._record_search_raw(
                        "百度百科(wap词条页直连)", name, wap_url, resp.status_code,
                        text[:500], "通道无效(反爬/异常)")
        except Exception as e:
            self._record_search_raw(
                "百度百科(wap词条页直连)", name, f"https://wapbaike.baidu.com/item/{encoded}",
                0, "", f"通道异常: {e}")

        # 1. 百度百科：搜索API（备用，本机实测遭反爬）
        if "百度百科" not in platforms:
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

        # 1b. 百度百科桌面 item URL（备用，本机实测遭反爬）；安全验证页/未创建词条不计有效通道
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
                            if name in text:
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
                return (Status.WARN, f"品牌[{brand_display}]无百科词条，但AI识别为已知品牌（需人工复核）",
                        f"尝试名称: {', '.join(checked_names)}。该结果为机器查证否定，上报告前需人工搜索品牌名复核确认。LLM判断: {llm_note}")
            return (Status.FAIL, f"品牌[{brand_display}]在百科平台和AI知识图谱中均未检测到实体（需人工复核）",
                    f"百科平台均未找到词条（有效通道{channels_ok}个）。尝试名称: {', '.join(checked_names)}。该结果为机器查证否定，上红字前需人工搜索品牌名复核确认。LLM判断: {llm_note}")

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
