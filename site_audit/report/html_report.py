"""HTML 报告生成 — 极联股份品牌升级设计"""
import base64
import html as html_mod
import os
import re
from urllib.parse import urlparse
from site_audit.core.models import AuditReport, Status, Category, Priority
from site_audit.core.scorer import grade_from_score

# 引擎版本：检查逻辑或报告模板每次改动必须 +1（日期.当日序号），
# 以隐藏方式写入每份报告（查看源代码可见），排查问题时区分报告由哪个版本生成。
ENGINE_VERSION = "2026.09.09.4"


# 报告出具方：默认本公司品牌「飞柚GEO」，可用环境变量 GEO_REPORT_PROVIDER 覆盖。
# 注意：feiyougeo.cn 是我们自己的官网（常被用作体检测试对象），不要把它误当成客户。
REPORT_PROVIDER = os.environ.get("GEO_REPORT_PROVIDER", "飞柚GEO")


# 从结论文案中兜底抽取页面路径（完整 URL / .html / index.php 路径）
_PATH_RE = re.compile(
    r"https?://[^\s,，;；。、）)】\s]+|/index\.php/[A-Za-z0-9_\-./]+|(?:/[A-Za-z0-9_\-]+)+\.html?")


def _evidence_pages(r, home_url: str) -> list:
    """结论具体涉及哪些页面：优先用结果自带 pages，其次从文案抽取路径，最后按范围回退。"""
    pages = []
    for x in (getattr(r, "pages", None) or []):
        x = str(x).strip()
        if x and x not in pages:
            pages.append(x)
    text = f"{getattr(r, 'message', '') or ''} {getattr(r, 'detail', '') or ''}"
    for m in _PATH_RE.findall(text):
        p = urlparse(m).path if m.startswith("http") else m
        p = p.strip()
        if p and p not in pages and len(p) > 1:
            pages.append(p)
    pages = pages[:12]
    if pages:
        return pages
    scope = getattr(r, "scope", "")
    if scope == "首页":
        return [home_url]
    if scope == "内容页":
        return ["抽样内容页"]
    if scope == "全站":
        # 整站总结型检查本就没有"单一来源页"，这不是故障，措辞按设计口径说明
        return ["全站多个页面（整站总结型检查）"]
    return [home_url]


def _home_root(final_url: str) -> str:
    p = urlparse(final_url)
    return f"{p.scheme}://{p.netloc}" if p.netloc else final_url.rstrip('/')


def _abs_page(x: str, root: str) -> str:
    """相对路径补成完整链接；文字说明（如'抽样内容页'）原样保留。"""
    x = (x or "").strip()
    if x.startswith("http://") or x.startswith("https://"):
        return x
    if x.startswith("/"):
        return root + (x if x != "/" else "/")
    return x


def _overflow_chip(text, chip_cls: str = "", maxw: str = "max-w-[220px]", full_html: str = None) -> str:
    """超长标签：块级单行截断并显示省略号，鼠标移入浮出完整内容（纯 CSS）。full_html 给浮层用。"""
    if not text:
        return ""
    vis = e(text)
    pop = full_html if full_html is not None else vis
    return (
        f'<span class="relative group/ov inline-flex min-w-0 {maxw}">'
        f'<span class="block w-full min-w-0 truncate {chip_cls}">{vis}</span>'
        # 浮层与标签无缝贴合（外层 pt-1 透明桥接，鼠标下移途中不丢失 hover），浮层文字可选中复制
        f'<span class="absolute left-0 top-full z-[80] hidden group-hover/ov:block pt-1">'
        f'<span class="block w-[480px] max-w-[86vw] rounded-lg bg-neutral-800/95 text-white text-xs leading-relaxed p-3 '
        f'shadow-xl whitespace-normal break-all font-normal text-left select-text cursor-text">{pop}</span>'
        f'</span>'
        f'</span>'
    )


# 统一的来源紫色（问题卡与详情列表一致，不再用灰色）；左右内边距对称
_SOURCE_CHIP_CLS = "px-2 py-0.5 rounded-md text-xs font-medium text-[#5C3CF1] bg-[#5C3CF1]/10"


def _source_html(raw_pages: list, final_url: str, maxw: str = "max-w-[220px]") -> str:
    """来源标签：「来源：」+具体页面；完整链接、单行限宽，浮层逐行列出全部完整链接。"""
    root = _home_root(final_url)
    pages = [_abs_page(x, root) for x in (raw_pages or [])]
    if not pages:
        pages = [final_url]
    n = len(pages)
    vis = pages[0] if n == 1 else f"共{n}个页面：{pages[0]}"
    if n == 1:
        pop = f'<div>{e(pages[0])}</div>'
    else:
        rows = "".join(f'<div class="mt-1">· {e(p)}</div>' for p in pages)
        pop = f'<div>共{n}个页面，完整链接如下：</div>{rows}'
    return ('<span class="inline-flex items-center text-xs font-semibold text-[#5C3CF1]">来源：</span>'
            + _overflow_chip(vis, _SOURCE_CHIP_CLS, maxw=maxw, full_html=pop))


def _source_block(r, final_url: str, maxw: str = "max-w-[220px]") -> str:
    return _source_html(_evidence_pages(r, final_url), final_url, maxw=maxw)


def _load_logo_base64() -> str:
    """读取 logo.png 并转为 base64 data URI"""
    logo_path = os.path.join(os.path.dirname(__file__), "logo.png")
    try:
        with open(logo_path, "rb") as f:
            data = base64.b64encode(f.read()).decode("ascii")
        return f"data:image/png;base64,{data}"
    except Exception:
        return ""


def e(text) -> str:
    """HTML 转义，防止内容中的 < > 破坏页面结构"""
    if text is None:
        return ""
    return html_mod.escape(str(text), quote=True)


def _clean_title(s):
    """抽样页标题乱码兜底：西里尔字符/替换符多、像样中文极少时判为编码错乱，不把乱码展示给客户。"""
    if not s:
        return "(无标题)"
    s = str(s)
    bad = sum(1 for ch in s if 0x0400 <= ord(ch) <= 0x04FF or ord(ch) == 0xFFFD)
    cjk = sum(1 for ch in s if 0x4E00 <= ord(ch) <= 0x9FFF)
    if bad > 0 and cjk < max(1, len(s) // 6):
        return "(标题无法识别,页面编码可能有误)"
    return s


def _clean_punct(text) -> str:
    """清洗检测文案中的异常标点：重复句号、句号+分号混拼等（分段 join 与 LLM 输出产生）"""
    if not text:
        return text
    t = str(text)
    # 词级统一命名（与本报告状态/标准口径保持一致）
    t = t.replace("达到通过标准", "达到合格标准")
    t = t.replace("基本达标", "基本合格")
    prev = None
    while prev != t:
        prev = t
        t = (t.replace("。。", "。")
              .replace("；；", "；")
              .replace("，，", "，")
              .replace("。；", "；")
              .replace("；。", "；")
              .replace("，。", "。")
              .replace("。，", "，")
              .replace("，；", "；")
              .replace("；，", "；"))
    return t.strip(" ；;，")


def _strip_page_ref(detail) -> str:
    """从 detail 中剥离「检查页面: xxx」段（已单独展示为「结果溯源页面」，避免路径重复）"""
    if not detail:
        return detail
    return re.sub(r"\s*[；;]?\s*检查页面[:：]\s*[^。]*。?", "", str(detail), count=1).strip(" ；;")


# ── 客户版首屏：红字问题卡文案库 ──
# 「改前」一律取当次检查的真实证据（检查结果里实际抓到的内容），此库只配置
# 问题定性 / 生意影响 / 改后方法论，禁止写具体某次的页面标题或客户名——
# 站点改版后写死示例会变成与事实不符的假证据（2026-09-06 整改教训）。
CARD_COPY = {
    "图片与多媒体优化": {
        "weight_i": 5, "weight_f": 5,
        "problem": "AI 看不懂您网站上的图片",
        "biz": "客户用 AI 搜产品图片时永远搜不到您——产品图、案例图这些最能打动客户的内容，AI 一张都看不见。",
        "after": "给每张重要图片配一句文字说明，描述图片里的真实场景，如「全产品线培训现场实拍」",
    },
    "搜索意图匹配": {
        "weight_i": 5, "weight_f": 4,
        "problem": "页面内容答非所问，AI 判定「不予引用」",
        "biz": "客户搜「方案」想比方案，看到的却是公司内部新闻——AI 不推荐您，等于新店开在没人的街上。",
        "after": "改成客户会搜的标题，如「制造企业全产品线培训方案怎么设计？实操复盘（附课程框架）」",
    },
    "E-E-A-T 内容可信度信号": {
        "weight_i": 5, "weight_f": 4,
        "problem": "文章匿名无署名，AI 不敢引用",
        "biz": "同样的话，竞品有专家署名就被 AI 引用；您的内容写得再好，在 AI 眼里也是来路不明的野广告。",
        "after": "文末补充作者署名与资质，如「作者：张 XX，高级顾问，10 年行业经验，服务客户 120+；数据来自某客户项目复盘」",
    },
    "URL结构与可读性": {
        "weight_i": 3, "weight_f": 2,
        "problem": "网址全是一串数字，看不出页面讲什么",
        "biz": "客户和 AI 都没法从网址判断页面主题，转发出去显得不专业，搜索引擎也会降低评分。（改网址涉及老后台，需先评估再动）",
        "after": "网址用拼音或英文写出主题，如 /news/kehu-anli",
    },
    "跨平台内容分发": {
        "weight_i": 4, "weight_f": 2,
        "problem": "内容只发在官网，知乎、公众号等平台看不到您",
        "biz": "AI 的训练材料里几乎找不到您的品牌，被提及、被推荐的概率自然低。见效慢，需持续投入。",
        "after": "官网文章同步发到知乎、公众号，并链回官网",
    },
}

# ── 非必检项/辅助项 计分口径说明文案（项名下方常显） ──
TIER_REASON = {
    # 非必检项（不计分）
    "tech-04": "llms.txt 属前瞻提案，目前无主流搜索引擎/AI 引擎强制要求、也不影响收录排名，做了更好、不做不罚",
    "tech-05": "GEO 前瞻布局文件，当前主流 AI 搜索引擎尚不依赖它，做了加分、不做不罚",
    "ai-06": "无结构化标记时 AI 仍能从页面文本解析层级，改造收益低于门槛",
    "content-06": "单语言站点无此需求，检测自动跳过",
    "auth-03": "属站外运营范畴，不是网站可信性的硬性门槛",
    "auth-06": "分发渠道在站外，非本站可控",
    "b2b-04": "对采购决策有价值，但受行业惯例与合规限制，非所有企业适合公开对比",
    "b2b-05": "技术型产品的重要参考，非技术型服务企业无此需求，检测自动跳过",
    "b2b-07": "属业务设计决策，机器只能识别表层样式、无法判断转化意图，易误判",
    "tech-19": "安全头主要是防护与站点健康项，对 SEO/GEO 排名影响很轻，只给整改建议、不扣分",
    # 辅助检测项（必检但不计分）
    "tech-14": "识别建站系统与技术栈，判断整改可行性，供接手人参考",
    "tech-15": "安全风险提示，与收录无关，供运维整改参考",
}

# ── SEO/GEO Tab 模块映射 ──
SEO_MODULE_MAP = [
    {"title": "站点抓取基础", "subtitle": "网站是否能被搜索引擎正常收录",
     "items": ["tech-01","tech-02","tech-03","tech-06","tech-07","tech-20","tech-08","tech-09","tech-10","tech-11","tech-12","tech-13","tech-16","tech-17","tech-18","tech-19"]},
    {"title": "内容就绪-搜索意图匹配", "subtitle": "内容是否匹配用户真实搜索需求",
     "items": ["ai-01","ai-02","ai-03","ai-04","ai-07","ai-08","ai-11","content-01","content-02","content-03","content-04","content-05","content-06","content-07","content-08","mp-01","mp-02","mp-03","mp-04","mp-05","mp-06","mp-07"]},
    {"title": "内容就绪-站点可信度", "subtitle": "网站可信度对外传递效果",
     "items": ["auth-01","auth-02","auth-08","auth-03","auth-04","auth-05","auth-06","auth-07"]},
    {"title": "商业完备度-决策信息", "subtitle": "客户决策所需商业信息完整度",
     "items": ["b2b-08","b2b-09","b2b-10","b2b-01","b2b-02","b2b-03","b2b-06","b2b-04","b2b-05","b2b-07"]},
]

GEO_MODULE_MAP = [
    {"title": "前置抓取基础", "subtitle": "网站内容能否被 AI 检索链路有效获取",
     "items": ["tech-01","tech-02","tech-03","tech-04","tech-05","tech-06","tech-07","tech-20","tech-08","tech-09","tech-10","tech-11","tech-12","tech-13","tech-16","tech-17","tech-18","tech-19"]},
    {"title": "AI 搜索适配专项", "subtitle": "是否适合 AI 搜索引用与回答生成（GEO 独有）",
     "items": ["ai-01","ai-02","ai-03","ai-04","ai-05","ai-06","ai-07","ai-08","ai-09","ai-10","ai-11","mp-01","mp-02","mp-03"]},
    {"title": "内容就绪-搜索意图匹配", "subtitle": "内容是否匹配 AI 理解的用户真实搜索需求",
     "items": ["content-01","content-02","content-03","content-04","content-05","content-06","content-07","content-08","mp-04","mp-05","mp-06","mp-07"]},
    {"title": "内容可信度", "subtitle": "影响 AI 是否采信本站作为答案来源",
     "items": ["auth-01","auth-02","auth-08","auth-07"]},
    {"title": "内容信息价值", "subtitle": "影响本站内容的引用价值",
     "items": ["auth-03","auth-04","auth-05","auth-06"]},
    {"title": "商业完备度-决策信息", "subtitle": "支撑 AI 商业答案输出",
     "items": ["b2b-08","b2b-09","b2b-10","b2b-01","b2b-02","b2b-03","b2b-06","b2b-04","b2b-05","b2b-07"]},
]

# ── SEO/GEO 检查集 item_id 集合（从模块映射提取，用于归属标记与分列得分）──
def _extract_item_ids(module_map):
    ids = set()
    for mod in module_map:
        ids.update(mod["items"])
    return ids

SEO_ITEM_IDS = _extract_item_ids(SEO_MODULE_MAP)
GEO_ITEM_IDS = _extract_item_ids(GEO_MODULE_MAP)


def _check_set_tag(item_id):
    """返回检查项归属标签 HTML：SEO独有 / GEO独有 / 两者共有"""
    in_seo = item_id in SEO_ITEM_IDS
    in_geo = item_id in GEO_ITEM_IDS
    if in_seo and in_geo:
        return '<span class="inline-flex items-center px-1.5 py-0.5 rounded text-[9px] font-bold whitespace-nowrap flex-shrink-0 text-[var(--jl-neutral-600)] bg-[var(--jl-muted)] border border-[var(--jl-border)]">SEO+GEO</span>'
    elif in_geo:
        return '<span class="inline-flex items-center px-1.5 py-0.5 rounded text-[9px] font-bold whitespace-nowrap flex-shrink-0 text-white" style="background:#5C3CF1;">GEO独有</span>'
    elif in_seo:
        return '<span class="inline-flex items-center px-1.5 py-0.5 rounded text-[9px] font-bold whitespace-nowrap flex-shrink-0 text-white bg-[var(--jl-blue-500)]">SEO</span>'
    return ""


def _calc_set_category_scores(results, item_ids):
    """按检查集过滤结果后计算维度得分（复用 scorer 逻辑）"""
    from site_audit.core.scorer import calculate_category_scores
    filtered = [r for r in results if r.item_id in item_ids]
    return calculate_category_scores(filtered)


def _pick_problem_cards(report: AuditReport) -> list:
    """从失败项里按「影响大 × 容易改」选最多 3 张客户版问题卡。"""
    fails = [r for r in report.results
             if r.status == Status.FAIL and r.item_id not in ("tech-14", "tech-15")
             and getattr(r, 'tier', 'core') != 'bonus']

    # 优先修复排序：先按问题严重档（P0>P1>P2），同档再按"好不好改"；
    # 对 AI 引用影响很弱的基础体验项（如移动 viewport，浏览器/脚本多已兜底）降为补位，
    # 只有当更要命的问题凑不满 3 个时才会出现在优先修复里。
    _prio_rank = {"P0": 3, "P1": 2, "P2": 1}
    _low_for_card = {"tech-11"}

    def _score(r):
        copy = CARD_COPY.get(r.item_name, {})
        pv = getattr(r.priority, "value", r.priority)
        base = _prio_rank.get(pv, 2)
        if r.item_id in _low_for_card:
            base = min(base, 1)
        return base * 10 + copy.get("weight_f", 3)

    fails.sort(key=_score, reverse=True)
    cards = []
    for r in fails[:3]:
        copy = CARD_COPY.get(r.item_name)
        ev_pages = _evidence_pages(r, report.final_url)
        evidence = r.message
        if r.detail:
            snippet = re.sub(r"检查页面[:：][^。]+。", "", r.detail).strip()
            if snippet and snippet[:30] not in evidence:
                # 卡面只放紧凑摘要（在完整分句边界收尾）；完整内容放进悬浮提示，不截断
                if len(snippet) > 160:
                    cut = snippet.rfind("；", 0, 160)
                    if cut < 60:
                        cut = snippet.rfind("，", 0, 160)
                    snippet = (snippet[:cut if cut > 0 else 160]).rstrip("；，、") + "……"
                evidence += "。" + snippet
        ev_full = re.sub(r"检查页面[:：][^。]+。", "", (r.detail or "")).strip() or evidence
        if copy:
            cards.append({
                "problem": copy["problem"], "evidence": evidence,
                "evidence_full": ev_full,
                "ev_pages": ev_pages, "biz": copy["biz"],
                "before": evidence, "after": copy["after"],
            })
        else:
            cards.append({
                "problem": r.item_name, "evidence": evidence,
                "evidence_full": ev_full,
                "ev_pages": ev_pages,
                "biz": r.impact or "该项不达标会影响 AI 对您网站的评价。",
                "before": "当前实际：" + (r.message or "未达标"),
                "after": r.suggestion or "请联系顾问获取修改方案",
            })
    return cards


def _status_style(status):
    """返回状态对应的 Tailwind 类名"""
    map = {
        Status.PASS: ("合格", "state-success", "state-success-bg", "state-success-text", "check"),
        Status.WARN: ("不足", "state-warning", "state-warning-bg", "state-warning-text", "alert-circle"),
        Status.FAIL: ("不合格", "state-error", "state-error-bg", "state-error-text", "x-circle"),
        Status.SKIP: ("未检查", "state-error", "state-error-bg", "state-error-text", "minus-circle"),
    }
    return map.get(status, map[Status.SKIP])


def _grade_color(grade):
    """等级对应颜色变量名"""
    map = {"A": "state-success", "B": "jl-blue-500", "C": "state-warning", "D": "state-orange", "F": "state-error"}
    return map.get(grade, "jl-neutral-500")


def _score_color(score):
    """分数对应颜色变量名"""
    if score >= 75:
        return "state-success"
    elif score >= 50:
        return "state-warning"
    else:
        return "state-error"


def generate_html_report(report: AuditReport) -> str:
    """生成完整 HTML 报告（品牌升级设计）"""
    summary = report.summary
    grade_color_var = _grade_color(report.grade)
    report_provider = REPORT_PROVIDER

    # ── 品牌名提取：优先首页直取的对外品牌名，其次 auth-05，最后回退域名 ──
    brand_name = (getattr(report, "site_brand", "") or "").strip()
    for r in report.results if not brand_name else []:
        if r.item_id == "auth-05":
            m = re.search(r"品牌[\[：:]\s*([^\]：]+)", r.message or "")
            if m:
                full_name = m.group(1).strip()
                # 生成简称：去掉地域前缀和公司后缀
                short = re.sub(r"^(深圳|北京|上海|广州|杭州|成都|武汉|南京|苏州|西安|重庆|天津|青岛|长沙|郑州|东莞|佛山|福州|合肥|济南|昆明|南宁|石家庄|太原|贵阳|兰州|海口|三亚|拉萨|银川|西宁|乌鲁木齐|呼和浩特|哈尔滨|长春|沈阳|大连|青岛|烟台|威海|济宁|临沂|潍坊|浙江|广东|江苏|山东|四川|福建|湖南|湖北|河南|河北|山西|陕西|安徽|江西|辽宁|吉林|黑龙江|云南|贵州|甘肃|青海|海南|)+", "", full_name)
                short = re.sub(r"(技术有限公司|科技有限公司|股份有限公司|有限责任公司|集团有限公司|实业集团有限公司|企服集团有限公司|营销技术有限公司|数智营销技术有限公司)$", "", short)
                brand_name = short if short else full_name
                break
    if not brand_name:
        domain = re.sub(r"^https?://(www\.)?", "", report.url).split("/")[0]
        parts = domain.split(".")
        if parts:
            brand_name = parts[0]

    # ── 客户版首屏：30秒看懂 + 问题卡（核心项口径：锦上添花项不计分）──
    fail_n = sum(1 for r in report.results
                 if r.status == Status.FAIL and r.item_id not in ("tech-14", "tech-15")
                 and getattr(r, 'tier', 'core') != 'bonus')
    cards = _pick_problem_cards(report)
    card_n = len(cards)
    if card_n > 0:
        headline = (f"本次体检 {summary['core_total']} 项计分检查，{fail_n} 项未达标——"
                    f"这些环节正在影响 AI 搜索引擎对您网站内容的理解与引用。"
                    f"优先修复以下 {card_n} 个关键问题，可快速提升 AI 可见度：")
    else:
        headline = (f"本次体检 {summary['core_total']} 项计分检查，{fail_n} 项未达标，"
                    f"整体表现良好，仍有优化空间。")
    cards_html = ""
    for i, c in enumerate(cards, 1):
        # 悬浮提示展示完整结果（不截断），分号处换行便于阅读
        _ev_full_html = e(c["evidence_full"]).replace("；", "；<br>")
        _ev_chip = _overflow_chip(
            c["evidence"],
            "px-2.5 py-1 rounded-md text-xs font-medium "
            "bg-[var(--state-error-bg)] text-[var(--state-error-text)] border border-[var(--state-error)]/10",
            maxw="max-w-[220px] sm:max-w-[300px]",
            full_html=_ev_full_html)
        _src_chip = _source_html(c["ev_pages"], report.final_url)
        cards_html += f'''
      <article class="relative bg-[var(--jl-card)] rounded-2xl p-5 sm:p-6 shadow-sm border border-[var(--jl-border)] overflow-visible">
        <div class="flex items-center gap-3 mb-4 flex-wrap">
          <h2 class="text-lg font-bold text-[var(--jl-neutral-900)]">{i}. {e(c["problem"])}</h2>
          {_ev_chip}
          <span class="inline-flex items-center">{_src_chip}</span>
        </div>
        <div class="grid grid-cols-1 md:grid-cols-2 gap-4">
          <div class="rounded-xl p-4 bg-transparent border border-[var(--jl-border)] shadow-sm">
            <div class="flex items-center gap-2 text-xs font-bold text-[var(--jl-neutral-700)] mb-2">
              <svg class="w-3.5 h-3.5 flex-shrink-0" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="m21.73 18-8-14a2 2 0 0 0-3.48 0l-8 14A2 2 0 0 0 4 21h16a2 2 0 0 0 1.73-3Z"/><line x1="12" y1="9" x2="12" y2="13"/><line x1="12" y1="17" x2="12.01" y2="17"/></svg>
              <span>对SEO / GEO的影响</span>
            </div>
            <p class="text-sm text-[var(--jl-neutral-700)] leading-relaxed">{e(c["biz"])}</p>
          </div>
          <div class="rounded-xl p-4 bg-transparent border border-[var(--jl-border)] shadow-sm">
            <div class="flex items-center gap-2 text-xs font-bold text-[var(--jl-neutral-700)] mb-2">
              <svg class="w-3.5 h-3.5 flex-shrink-0" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M22 11.08V12a10 10 0 1 1-5.93-9.14"/><polyline points="22 4 12 14.01 9 11.01"/></svg>
              <span>修改示例（改前 → 改后）</span>
            </div>
            <p class="text-sm text-[var(--jl-neutral-700)] leading-relaxed">
              <b>改前：</b>{e(c["before"])}<br><b>改后：</b>{e(c["after"])}
            </p>
          </div>
        </div>
      </article>'''

    if not cards_html:
        cards_html = '''
      <article class="bg-[var(--state-success-bg)] border border-[var(--state-success)]/20 rounded-2xl p-6 text-[var(--state-success-text)] text-base">
        本次体检未发现致命问题，详见下方报告详情。
      </article>'''

    # ── 维度得分卡片（按 SEO/GEO 检查集拆分双列） ──
    seo_cats = _calc_set_category_scores(report.results, SEO_ITEM_IDS)
    geo_cats = _calc_set_category_scores(report.results, GEO_ITEM_IDS)

    def _render_dim_rows(cats):
        rows = ""
        for c in cats:
            bar_color = _score_color(c.score)
            rows += f'''
        <div style="margin-bottom:14px;">
          <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:4px;">
            <span style="font-size:12px;font-weight:600;color:var(--jl-neutral-700);">{e(c.category.value)}</span>
            <span style="font-size:16px;font-weight:700;color:var(--jl-neutral-900);">{c.score}</span>
          </div>
          <div style="height:6px;border-radius:3px;background:var(--jl-neutral-200);overflow:hidden;">
            <div style="height:100%;border-radius:3px;width:{c.score}%;background:var(--{bar_color});"></div>
          </div>
          <div style="margin-top:3px;font-size:10px;color:var(--jl-neutral-500);">合格 {c.passed} · 不足 {c.warned} · 不合格 {c.failed}</div>
        </div>'''
        return rows

    dimension_card_html = f'''
    <div style="background:var(--jl-card);border-radius:16px;padding:24px;box-shadow:0 1px 2px rgba(15,23,42,0.04);border:1px solid var(--jl-border);height:100%;display:flex;flex-direction:column;">
      <h3 style="font-size:15px;font-weight:700;color:var(--jl-neutral-900);margin:0 0 16px 0;display:flex;align-items:center;gap:8px;">
        <span style="width:4px;height:20px;border-radius:4px;background:var(--jl-primary);"></span>
        维度得分
      </h3>
      <div style="flex:1;display:grid;grid-template-columns:1fr 1fr;gap:20px;">
        <div>
          <div style="font-size:12px;font-weight:700;color:var(--jl-blue-500);margin-bottom:10px;">SEO 检查集</div>
          {_render_dim_rows(seo_cats)}
        </div>
        <div>
          <div style="font-size:12px;font-weight:700;color:#5C3CF1;margin-bottom:10px;">GEO 检查集</div>
          {_render_dim_rows(geo_cats)}
        </div>
      </div>
    </div>'''

    # ── 表格行渲染函数（3列 + 手风琴展开） ──
    _row_counter = [0]

    def _render_row(r, show_set_tag=True):
        _row_counter[0] += 1
        row_idx = _row_counter[0]
        label, color_var, bg_var, text_var, icon_name = _status_style(r.status)

        # 图标 SVG
        icons = {
            "check": '<svg class="w-3 h-3 mr-1 flex-shrink-0" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M22 11.08V12a10 10 0 1 1-5.93-9.14"/><polyline points="22 4 12 14.01 9 11.01"/></svg>',
            "alert-circle": '<svg class="w-3 h-3 mr-1 flex-shrink-0" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="10"/><line x1="12" y1="8" x2="12" y2="12"/><line x1="12" y1="16" x2="12.01" y2="16"/></svg>',
            "x-circle": '<svg class="w-3 h-3 mr-1 flex-shrink-0" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="10"/><line x1="15" y1="9" x2="9" y2="15"/><line x1="9" y1="9" x2="15" y2="15"/></svg>',
            "minus-circle": '<svg class="w-3 h-3 mr-1 flex-shrink-0" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="10"/><line x1="8" y1="12" x2="16" y2="12"/></svg>',
        }
        icon_svg = icons.get(icon_name, icons["check"])

        # detail 区（剥离已上行的「检查页面」段 + 标点清洗）；超长单行截断，鼠标悬浮自制浮层显示完整内容
        # （原生 title 提示对长文本会被浏览器截断，改用与来源标签同款浮层，完整不截断、可选中复制）
        _detail_clean = _clean_punct(_strip_page_ref(r.detail))
        if not _detail_clean:
            detail_html = ""
        elif len(_detail_clean) > 70:
            _det_pop = e(_detail_clean).replace("；", "；<br>")
            detail_html = (
                f'<div class="mt-1 relative group/det max-w-[560px]">'
                f'<div class="text-xs text-[var(--jl-neutral-500)] truncate cursor-help">{e(_detail_clean)}</div>'
                f'<div class="absolute left-0 top-full z-[80] hidden group-hover/det:block pt-1">'
                f'<div class="w-[620px] max-w-[86vw] rounded-lg bg-neutral-800/95 text-white text-xs leading-relaxed p-3 '
                f'shadow-xl whitespace-normal break-all select-text cursor-text">{_det_pop}</div>'
                f'</div></div>'
            )
        else:
            detail_html = f'<div class="mt-1 text-xs text-[var(--jl-neutral-500)]">{e(_detail_clean)}</div>'

        # 作用（缺失影响已上行至项名下方常显，此处只保留作用）
        purpose_impact = ""
        if r.purpose:
            purpose_impact = f'<div class="text-xs text-[var(--jl-neutral-700)] leading-relaxed">{e(r.purpose)}</div>'
        else:
            purpose_impact = '<div class="text-xs text-[var(--jl-neutral-400)]">-</div>'

        # 手动检查方法
        manual = f'<div class="text-xs text-[var(--jl-neutral-700)] leading-relaxed">{e(r.manual_check)}</div>' if r.manual_check else '<div class="text-xs text-[var(--jl-neutral-400)]">-</div>'

        # 三级标准（统一命名口径与清洗标点）
        excellent = e(_clean_punct(r.excellent)) or "-"
        normal = e(_clean_punct(r.normal)) or "-"
        failed = e(_clean_punct(r.failed)) or "-"
        if r.excellent or r.normal or r.failed:
            standards = f'''
            <div class="text-[11px] leading-relaxed">
              <div class="text-[var(--state-success)]"><b>合格标准：</b>{excellent}</div>
              <div class="text-[var(--state-warning)]"><b>不足标准：</b>{normal}</div>
              <div class="text-[var(--state-error)]"><b>不合格标准：</b>{failed}</div>
            </div>'''
        else:
            standards = '<div class="text-xs text-[var(--jl-neutral-400)]">-</div>'

        # 范围 + 结果溯源页面（「X适用」tag 与「X级」信息重叠，已移除；内容页级用品牌紫）
        if r.scope == "内容页":
            scope_cls = "text-[#5C3CF1] bg-[#5C3CF1]/10"
        elif r.scope == "全站":
            scope_cls = "text-[var(--state-info)] bg-[var(--state-info-bg)]"
        else:
            scope_cls = "text-[var(--jl-blue-500)] bg-[var(--jl-blue-50)]"

        _src = _source_block(r, report.final_url)

        scope_html = f'''
        <div class="mt-2 flex flex-wrap items-center gap-1.5">
          <span class="inline-flex items-center px-1.5 py-0.5 rounded-md text-[10px] font-semibold whitespace-nowrap flex-shrink-0 {scope_cls}">
            {r.scope}级
          </span>
          {_src}
        </div>'''

        # 修复建议
        suggestion_html = ""
        if r.suggestion and r.status in (Status.FAIL, Status.WARN):
            suggestion_html = f'''
            <div class="mt-2 p-2.5 bg-[var(--state-success-bg)] border-l-2 border-[var(--state-success)] rounded-r-md">
              <div class="text-[10px] font-bold text-[var(--state-success-text)] mb-1">修复建议</div>
              <div class="text-xs text-[var(--state-success-text)] leading-relaxed">{e(r.suggestion)}</div>
            </div>'''

        # 标签：必检项/非必检项 + 参与计分/不计分（不显示内部优先级编码）
        # 辅助检测项（CMS诊断与运营维护评估）：必检但不计分
        if getattr(r, 'tier', 'core') == 'bonus':
            level_tag = '<span class="inline-flex items-center px-2 py-0.5 rounded-md text-[10px] font-semibold whitespace-nowrap flex-shrink-0 text-[var(--jl-neutral-500)] bg-[var(--jl-muted)] border border-[var(--jl-border)]">非必检项</span>'
            score_tag = '<span class="inline-flex items-center px-2 py-0.5 rounded-md text-[10px] font-medium whitespace-nowrap flex-shrink-0 text-[var(--jl-neutral-600)] bg-[var(--jl-muted)] border border-[var(--jl-border)]">不计分</span>'
        elif r.item_id in ("tech-14", "tech-15"):
            level_tag = '<span class="inline-flex items-center px-2 py-0.5 rounded-md text-[10px] font-semibold whitespace-nowrap flex-shrink-0 text-white bg-[var(--jl-blue-500)]">必检项</span>'
            score_tag = '<span class="inline-flex items-center px-2 py-0.5 rounded-md text-[10px] font-medium whitespace-nowrap flex-shrink-0 text-[var(--jl-neutral-600)] bg-[var(--jl-muted)] border border-[var(--jl-border)]">不计分</span>'
        else:
            level_tag = '<span class="inline-flex items-center px-2 py-0.5 rounded-md text-[10px] font-semibold whitespace-nowrap flex-shrink-0 text-white bg-[var(--jl-blue-500)]">必检项</span>'
            score_tag = '<span class="inline-flex items-center px-2 py-0.5 rounded-md text-[10px] font-medium whitespace-nowrap flex-shrink-0 text-[var(--jl-primary)] bg-[var(--jl-blue-50)] border border-[var(--jl-blue-200)]">参与计分</span>'

        # 项名下方常显说明行：必检项=影响（缺失影响上行），非必检项=不扣分原因，辅助项=不计分说明
        if getattr(r, 'tier', 'core') == 'bonus':
            _reason_label, _reason_text = "不扣分原因", TIER_REASON.get(r.item_id, "")
        elif r.item_id in ("tech-14", "tech-15"):
            _reason_label, _reason_text = "不计分", TIER_REASON.get(r.item_id, "")
        else:
            _reason_label, _reason_text = "影响", (r.impact or "")
        reason_html = ""
        if _reason_text:
            reason_html = (
                f'<div class="mt-0.5 pl-6 text-[10px] text-[var(--jl-neutral-500)] leading-snug">'
                f'<b>{_reason_label}：</b>{e(_reason_text)}</div>'
            )

        return f'''
              <tr class="accordion-row cursor-pointer hover:bg-[var(--jl-muted)] transition-colors align-middle" onclick="this.nextElementSibling.classList.toggle('hidden')">
                <td class="text-left px-4 py-3 text-[var(--jl-neutral-700)]">
                  <div class="relative">
                    <svg class="absolute left-0 top-[2px] w-4 h-4 text-[var(--jl-neutral-400)] flex-shrink-0 transition-transform" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><polyline points="6 9 12 15 18 9"/></svg>
                    <div class="flex flex-wrap items-center gap-2 pl-6">
                      {_check_set_tag(r.item_id) if show_set_tag else ""}
                      <span class="font-medium text-[var(--jl-neutral-900)] min-w-0 break-words">{e(r.item_name)}</span>
                      {level_tag}
                      {score_tag}
                    </div>
                  </div>
                  {reason_html}
                </td>
                <td class="text-center px-4 py-3 whitespace-nowrap">
                  <span class="inline-flex items-center px-3 py-1 rounded-full text-xs font-semibold bg-[var(--{bg_var})] text-[var(--{text_var})] border border-[var(--{color_var})]/10 whitespace-nowrap">
                    {icon_svg}{label}
                  </span>
                </td>
                <td class="text-left px-4 py-3 text-[var(--jl-neutral-700)] text-sm">
                  {e(_clean_punct(r.message))}
                  {detail_html}
                  {suggestion_html}
                  {scope_html}
                </td>
              </tr>
              <tr class="hidden">
                <td colspan="3" class="px-4 py-3 bg-[var(--jl-muted)]/30">
                  <div class="divide-y divide-[var(--jl-border)]/40">
                    <div class="flex gap-4 items-center py-3">
                      <div class="w-40 flex-shrink-0 text-xs font-bold text-[var(--jl-neutral-500)]">作用</div>
                      <div class="flex-1">{purpose_impact}</div>
                    </div>
                    <div class="flex gap-4 items-center py-3">
                      <div class="w-40 flex-shrink-0 text-xs font-bold text-[var(--jl-neutral-500)]">验收方式</div>
                      <div class="flex-1">{manual}</div>
                    </div>
                    <div class="flex gap-4 items-center py-3">
                      <div class="w-40 flex-shrink-0 text-xs font-bold text-[var(--jl-neutral-500)]">合格 / 不足 / 不合格 标准</div>
                      <div class="flex-1">{standards}</div>
                    </div>
                  </div>
                </td>
              </tr>'''

    # 表格头
    _table_head = '''
              <thead class="bg-[var(--jl-muted)] text-[var(--jl-neutral-500)]">
                <tr>
                  <th class="text-left px-4 py-3 font-medium whitespace-nowrap w-[35%]">检查项</th>
                  <th class="text-center px-4 py-3 font-medium whitespace-nowrap w-[10%]">状态</th>
                  <th class="text-left px-4 py-3 font-medium whitespace-nowrap">检测结果</th>
                </tr>
              </thead>'''

    # CMS诊断独立板块
    cms_item_ids = {"tech-14", "tech-15"}

    # 按 SEO/GEO Tab 分模块渲染详细结果（排除CMS）
    item_map = {r.item_id: r for r in report.results}

    # ── 检查项计分配置详情（弹窗数据）──
    def _cfg_set(iid):
        s, g = iid in SEO_ITEM_IDS, iid in GEO_ITEM_IDS
        return "SEO+GEO" if (s and g) else ("仅SEO" if s else ("仅GEO" if g else "辅助诊断"))

    def _cfg_level(r):
        if getattr(r, "tier", "core") == "bonus":
            return "非必检项", "不计分"
        if r.item_id in cms_item_ids:
            return "必检项", "不计分"
        return "必检项", "参与计分"

    _cat_order = {Category.TECHNICAL: 0, Category.AI_FRIENDLY: 1, Category.CONTENT: 2,
                  Category.AUTHORITY: 3, Category.B2B: 4}
    _cfg_rows = []
    for r in sorted(item_map.values(), key=lambda x: (_cat_order.get(x.category, 9), x.item_id)):
        level, scoring = _cfg_level(r)
        lvl_cls = ("text-[var(--jl-neutral-500)] bg-[var(--jl-muted)]" if level == "非必检项"
                   else "text-white bg-[var(--jl-blue-500)]")
        sco_cls = ("text-[var(--jl-neutral-600)] bg-[var(--jl-muted)]" if scoring == "不计分"
                   else "text-[var(--jl-primary)] bg-[var(--jl-blue-50)]")
        _cfg_rows.append(
            f'<tr class="align-top">'
            f'<td class="px-3 py-2 text-[var(--jl-neutral-900)] font-medium whitespace-nowrap">{e(r.item_name)}</td>'
            f'<td class="px-3 py-2 text-[var(--jl-neutral-600)] whitespace-nowrap">{e(getattr(r.category, "value", r.category))}</td>'
            f'<td class="px-3 py-2 text-[var(--jl-neutral-600)] whitespace-nowrap">{_cfg_set(r.item_id)}</td>'
            f'<td class="px-3 py-2 text-[var(--jl-neutral-700)]">{e(getattr(r, "description", "") or r.purpose or "-")}</td>'
            f'<td class="px-3 py-2 whitespace-nowrap"><span class="inline-block px-2 py-0.5 rounded text-[11px] font-semibold {lvl_cls}">{level}</span></td>'
            f'<td class="px-3 py-2 whitespace-nowrap"><span class="inline-block px-2 py-0.5 rounded text-[11px] font-medium {sco_cls}">{scoring}</span></td>'
            f'</tr>'
        )
    config_modal_html = f'''
    <div id="cfgModal" style="display:none;position:fixed;inset:0;z-index:9999;background:rgba(15,23,42,.55);" onclick="if(event.target===this)closeCfgModal()">
      <div style="background:var(--jl-card);border-radius:16px;max-width:1080px;width:calc(100% - 48px);max-height:84vh;margin:6vh auto;display:flex;flex-direction:column;box-shadow:0 20px 60px rgba(15,23,42,.3);">
        <div style="display:flex;justify-content:space-between;align-items:center;padding:18px 22px;border-bottom:1px solid var(--jl-border);">
          <div>
            <div style="font-size:16px;font-weight:700;color:var(--jl-neutral-900);">检查项计分配置详情</div>
            <div style="font-size:12px;color:var(--jl-neutral-500);margin-top:2px;">共 {len(_cfg_rows)} 个检查点 · 必检项参与总分，非必检项与辅助诊断只给建议、不计分</div>
          </div>
          <button onclick="closeCfgModal()" style="border:none;background:var(--jl-muted);width:30px;height:30px;border-radius:8px;cursor:pointer;font-size:16px;color:var(--jl-neutral-600);">×</button>
        </div>
        <div style="overflow:auto;padding:16px 22px 22px;">
          <table class="w-full text-xs" style="border-collapse:collapse;min-width:760px;">
            <thead style="position:sticky;top:0;background:var(--jl-neutral-50);">
              <tr>
                <th class="px-3 py-2 text-left font-semibold text-[var(--jl-neutral-500)] whitespace-nowrap">检查点</th>
                <th class="px-3 py-2 text-left font-semibold text-[var(--jl-neutral-500)] whitespace-nowrap">维度</th>
                <th class="px-3 py-2 text-left font-semibold text-[var(--jl-neutral-500)] whitespace-nowrap">所属检查集</th>
                <th class="px-3 py-2 text-left font-semibold text-[var(--jl-neutral-500)]">检查什么</th>
                <th class="px-3 py-2 text-left font-semibold text-[var(--jl-neutral-500)] whitespace-nowrap">是否必检</th>
                <th class="px-3 py-2 text-left font-semibold text-[var(--jl-neutral-500)] whitespace-nowrap">是否计分</th>
              </tr>
            </thead>
            <tbody class="divide-y divide-[var(--jl-border)]">{''.join(_cfg_rows)}</tbody>
          </table>
        </div>
      </div>
    </div>
    <script>
      function openCfgModal(){{document.getElementById('cfgModal').style.display='block';document.body.style.overflow='hidden';}}
      function closeCfgModal(){{document.getElementById('cfgModal').style.display='none';document.body.style.overflow='';}}
      document.addEventListener('keydown',function(e){{if(e.key==='Escape')closeCfgModal();}});
    </script>'''

    def _render_detail_modules(module_map):
        _row_counter[0] = 0
        sections = ""
        for mod in module_map:
            mod_items = [item_map[i] for i in mod["items"] if i in item_map and i not in cms_item_ids]
            if not mod_items:
                continue
            # 必检项（计分）排前、非必检项排后；组内按严重程度：不合格 > 不足 > 合格 > 未检查
            _sev_rank = {Status.FAIL: 0, Status.WARN: 1, Status.PASS: 2, Status.SKIP: 3}
            mod_items.sort(key=lambda r: (getattr(r, 'tier', 'core') == 'bonus',
                                          _sev_rank.get(r.status, 9)))
            rows = "".join(_render_row(r, show_set_tag=False) for r in mod_items)
            sections += f'''
        <section>
          <h3 class="text-base font-bold text-[var(--jl-neutral-900)] mb-2 flex items-center gap-2">
            <span class="w-1 h-5 rounded-full bg-[var(--jl-primary)]"></span>
            {mod["title"]}
          </h3>
          <p class="text-xs text-[var(--jl-neutral-500)] mb-4 ml-3">{mod["subtitle"]} · {len(mod_items)} 项</p>
          <div class="overflow-x-auto rounded-2xl border border-[var(--jl-border)] bg-[var(--jl-card)] shadow-sm">
            <table class="w-full min-w-[600px] text-sm">
              {_table_head}
              <tbody class="divide-y divide-[var(--jl-border)]">{rows}</tbody>
            </table>
          </div>
        </section>'''
        return sections

    seo_detail = _render_detail_modules(SEO_MODULE_MAP)
    geo_detail = _render_detail_modules(GEO_MODULE_MAP)

    detail_sections = f'''
        <div style="display:flex;flex-direction:column;">
          <div style="display:flex;gap:4px;margin-bottom:-1px;position:relative;z-index:1;">
            <div class="tab-btn active" onclick="switchDetailTab(\'seo\')" id="detail-tab-seo">SEO 检查集</div>
            <div class="tab-btn" onclick="switchDetailTab(\'geo\')" id="detail-tab-geo">GEO 检查集</div>
          </div>
          <div id="detail-tab-seo-content" style="background:var(--jl-card);border:1px solid var(--jl-blue-200);border-radius:0 16px 16px 16px;padding:12px;display:flex;flex-direction:column;gap:24px;">
            {seo_detail}
          </div>
          <div id="detail-tab-geo-content" style="background:var(--jl-card);border:1px solid var(--jl-blue-200);border-radius:0 16px 16px 16px;padding:12px;display:flex;flex-direction:column;gap:24px;display:none;">
            {geo_detail}
          </div>
        </div>'''

    # 待优化清单
    non_pass = [r for r in report.results if r.status in (Status.FAIL, Status.WARN) and r.item_id not in cms_item_ids and getattr(r, 'tier', 'core') == 'core']
    status_order = {Status.FAIL: 0, Status.WARN: 1, Status.SKIP: 2}
    priority_order = {"P0": 0, "P1": 1, "P2": 2}
    non_pass.sort(key=lambda r: (status_order.get(r.status, 9), priority_order.get(r.priority.value, 9)))

    todo_list_html = ""
    if non_pass:
        fail_count = sum(1 for r in non_pass if r.status == Status.FAIL)
        warn_count = sum(1 for r in non_pass if r.status == Status.WARN)
        seo_todo = sum(1 for r in non_pass if r.item_id in SEO_ITEM_IDS)
        geo_todo = sum(1 for r in non_pass if r.item_id in GEO_ITEM_IDS)
        geo_only_todo = sum(1 for r in non_pass if r.item_id in GEO_ITEM_IDS and r.item_id not in SEO_ITEM_IDS)
        todo_rows = "".join(_render_row(r) for r in non_pass)
        todo_list_html = f'''
      <section>
        <h3 class="text-base font-bold text-[var(--jl-neutral-900)] mb-2 flex items-center gap-2">
          <span class="w-1 h-5 rounded-full bg-[var(--state-error)]"></span>
          待优化清单
        </h3>
        <p class="text-xs text-[var(--jl-neutral-500)] mb-4">
          必检项有 {len(non_pass)} 项需处理 ·
          <span class="text-[var(--state-error)] font-semibold">不合格 {fail_count}</span> ·
          <span class="text-[var(--state-warning)] font-semibold">不足 {warn_count}</span>
          （按严重程度排序）
        </p>
        <div class="overflow-x-auto rounded-2xl border border-[var(--jl-border)] bg-[var(--jl-card)] shadow-sm">
          <table class="w-full min-w-[600px] text-sm">
            {_table_head}
            <tbody class="divide-y divide-[var(--jl-border)]">{todo_rows}</tbody>
          </table>
        </div>
      </section>'''

    # CMS诊断独立板块
    cms_results = [r for r in report.results if r.item_id in cms_item_ids]
    cms_rows = "".join(_render_row(r) for r in cms_results)
    cms_section_html = f'''
      <section>
        <h3 class="text-base font-bold text-[var(--jl-neutral-900)] mb-2 flex items-center gap-2">
          <span class="w-1 h-5 rounded-full bg-[var(--jl-blue-400)]"></span>
          CMS诊断与运营维护评估
        </h3>
        <p class="text-xs text-[var(--jl-neutral-500)] mb-4">识别建站系统类型，评估技术栈是否适合AI GEO 优化运营，检测后台路径安全性</p>
        <div class="overflow-x-auto rounded-2xl border border-[var(--jl-border)] bg-[var(--jl-card)] shadow-sm">
          <table class="w-full min-w-[600px] text-sm">
            {_table_head}
            <tbody class="divide-y divide-[var(--jl-border)]">{cms_rows}</tbody>
          </table>
        </div>
      </section>''' if cms_rows else ""

    # 打分逻辑说明
    weight_rows = ""
    for c in report.categories:
        weight_pct = int(c.weight * 100)
        score_color = _score_color(c.score)
        weight_rows += (
            f'<tr><td class="px-4 py-2.5 text-[var(--jl-neutral-700)]">{e(c.category.value)}</td>'
            f'<td class="px-4 py-2.5 text-center font-semibold text-[var(--jl-neutral-900)]">{weight_pct}%</td>'
            f'<td class="px-4 py-2.5 text-center text-[var(--jl-neutral-500)]">{c.total} 项</td>'
            f'<td class="px-4 py-2.5 text-center font-bold text-[var(--{score_color})]">{c.score}</td></tr>'
        )

    # 多页面抽样
    if report.sampled_pages:
        sampled_rows = ""
        for idx, p in enumerate(report.sampled_pages, 1):
            status_color = "state-success" if p.get("status") == 200 else "state-error"
            sampled_rows += (
                f'<tr class="align-top">'
                f'<td class="text-left px-4 py-2.5 text-[var(--jl-neutral-500)] whitespace-nowrap">{idx}</td>'
                f'<td class="text-left px-4 py-2.5 text-[var(--jl-neutral-900)] max-w-xs break-words">{e(_clean_title(p.get("title")))}</td>'
                f'<td class="text-left px-4 py-2.5 text-[var(--jl-primary)] max-w-sm break-all text-xs">{e(p.get("url",""))}</td>'
                f'<td class="text-center px-4 py-2.5 font-semibold text-[var(--{status_color})]">{p.get("status")}</td>'
                f'<td class="text-center px-4 py-2.5 text-[var(--jl-neutral-500)] whitespace-nowrap">{p.get("size_kb", 0):.0f}KB</td>'
                f'</tr>'
            )
        sampled_pages_html = f'''
      <section>
        <h3 class="text-base font-bold text-[var(--jl-neutral-900)] mb-2 flex items-center gap-2">
          <span class="w-1 h-5 rounded-full bg-[var(--jl-blue-400)]"></span>
          多页面抽样覆盖（共 {len(report.sampled_pages)} 个内页）
        </h3>
        <p class="text-xs text-[var(--jl-neutral-500)] mb-4 leading-relaxed">抽样规则：除首页外，<b>优先保证必查页各取到</b>——关于我们、产品/服务介绍、联系我们、客户案例、资讯/文章、FAQ常见问题、隐私政策；帮助文档、资质荣誉在站内确实存在时也必查。其余名额在不同栏目间轮转补足、不让同一类页面占满，站点必查页越齐全、抽样数量越多。断链检测、标题/描述唯一性、关键词蚕食、内页质量等均基于这些页面做跨页分析。</p>
        <div class="overflow-x-auto rounded-2xl border border-[var(--jl-border)] bg-[var(--jl-card)] shadow-sm">
          <table class="w-full min-w-[700px] text-sm">
            <thead class="bg-[var(--jl-muted)] text-[var(--jl-neutral-500)]">
              <tr>
                <th class="text-left px-4 py-3 font-medium w-12">#</th>
                <th class="text-left px-4 py-3 font-medium">页面标题</th>
                <th class="text-left px-4 py-3 font-medium">URL</th>
                <th class="text-center px-4 py-3 font-medium">状态</th>
                <th class="text-center px-4 py-3 font-medium">大小</th>
              </tr>
            </thead>
            <tbody class="divide-y divide-[var(--jl-border)]">{sampled_rows}</tbody>
          </table>
        </div>
      </section>'''
    else:
        sampled_pages_html = '''
      <div class="bg-[var(--state-warning-bg)] border border-[var(--state-warning)]/20 rounded-2xl p-4 text-sm text-[var(--state-warning-text)]">
        本次仅检测到首页，未从首页发现可抽样的同域内页（可能首页链接为 JS 动态渲染或内链极少）；断链检测、Title唯一性、关键词蚕食等跨页面检查已跳过。
      </div>'''

    # ── 完整 HTML ──
    html = f'''<!DOCTYPE html>
<html lang="zh-CN">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <meta name="engine-version" content="{ENGINE_VERSION}">
  <title>GEO 诊断报告 - {e(brand_name)}</title>
  <script src="https://cdn.jsdelivr.net/npm/@tailwindcss/browser@4.3.1/dist/index.global.js"></script>
  <style>
    /* 极联股份 · GEO 诊断报告 — 品牌升级设计系统 */
    :root {{
      --jl-blue-50: #eff6ff;
      --jl-blue-100: #dbeafe;
      --jl-blue-200: #bfdbfe;
      --jl-blue-300: #93c5fd;
      --jl-blue-400: #60a5fa;
      --jl-blue-500: #3b82f6;
      --jl-blue-600: #2563eb;
      --jl-blue-700: #1d4ed8;
      --jl-blue-800: #1e3a5f;
      --jl-blue-900: #172554;
      --jl-neutral-0: #ffffff;
      --jl-neutral-50: #f8fafc;
      --jl-neutral-100: #f1f5f9;
      --jl-neutral-200: #e2e8f0;
      --jl-neutral-300: #cbd5e1;
      --jl-neutral-400: #94a3b8;
      --jl-neutral-500: #64748b;
      --jl-neutral-600: #475569;
      --jl-neutral-700: #334155;
      --jl-neutral-800: #1e293b;
      --jl-neutral-900: #0f172a;
      --state-success: #10b981;
      --state-success-bg: #ecfdf5;
      --state-success-text: #065f46;
      --state-warning: #f59e0b;
      --state-warning-bg: #fffbeb;
      --state-warning-text: #92400e;
      --state-error: #ef4444;
      --state-error-bg: #fef2f2;
      --state-error-text: #991b1b;
      --state-info: #0ea5e9;
      --state-info-bg: #f0f9ff;
      --state-info-text: #0c4a6e;
      --state-orange: #f97316;
      --state-orange-bg: #fff7ed;
      --state-orange-text: #9a3412;
      --jl-background: var(--jl-neutral-50);
      --jl-foreground: var(--jl-neutral-900);
      --jl-card: var(--jl-neutral-0);
      --jl-card-foreground: var(--jl-neutral-800);
      --jl-primary: var(--jl-blue-600);
      --jl-primary-foreground: #ffffff;
      --jl-muted: var(--jl-neutral-100);
      --jl-muted-foreground: var(--jl-neutral-500);
      --jl-border: var(--jl-neutral-200);
    }}
    body {{
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", "PingFang SC",
        "Hiragino Sans GB", "Microsoft YaHei", sans-serif;
      -webkit-font-smoothing: antialiased;
      -moz-osx-font-smoothing: grayscale;
    }}
    /* 自定义补充样式 */
    details > summary {{ list-style: none; cursor: pointer; }}
    details > summary::-webkit-details-marker {{ display: none; }}
    details[open] .chev {{ transform: rotate(180deg); }}
    .chev {{ transition: transform 0.2s; }}
    .tab-btn {{ padding:10px 24px;border-radius:12px 12px 0 0;font-size:14px;font-weight:600;cursor:pointer;border:1px solid var(--jl-border);border-bottom:none;transition:all 0.2s; }}
    .tab-btn.active {{ background:var(--jl-card);color:var(--jl-primary);border-color:var(--jl-blue-200);border-bottom:1px solid var(--jl-card);position:relative;z-index:1; }}
    .tab-btn:not(.active) {{ background:var(--jl-muted);color:var(--jl-neutral-500);border-bottom:1px solid var(--jl-border); }}
  </style>
  <style type="text/tailwindcss">
    @theme inline {{
      --color-background: var(--jl-background);
      --color-foreground: var(--jl-foreground);
      --color-card: var(--jl-card);
      --color-card-foreground: var(--jl-card-foreground);
      --color-primary: var(--jl-primary);
      --color-primary-foreground: var(--jl-primary-foreground);
      --color-muted: var(--jl-muted);
      --color-muted-foreground: var(--jl-muted-foreground);
      --color-border: var(--jl-border);
      --color-jl-blue-50: var(--jl-blue-50);
      --color-jl-blue-100: var(--jl-blue-100);
      --color-jl-blue-200: var(--jl-blue-200);
      --color-jl-blue-300: var(--jl-blue-300);
      --color-jl-blue-400: var(--jl-blue-400);
      --color-jl-blue-500: var(--jl-blue-500);
      --color-jl-blue-600: var(--jl-blue-600);
      --color-jl-blue-700: var(--jl-blue-700);
      --color-jl-blue-800: var(--jl-blue-800);
      --color-jl-blue-900: var(--jl-blue-900);
      --color-state-success: var(--state-success);
      --color-state-success-bg: var(--state-success-bg);
      --color-state-success-text: var(--state-success-text);
      --color-state-warning: var(--state-warning);
      --color-state-warning-bg: var(--state-warning-bg);
      --color-state-warning-text: var(--state-warning-text);
      --color-state-error: var(--state-error);
      --color-state-error-bg: var(--state-error-bg);
      --color-state-error-text: var(--state-error-text);
      --color-state-info: var(--state-info);
      --color-state-info-bg: var(--state-info-bg);
      --color-state-info-text: var(--state-info-text);
      --color-state-orange: var(--state-orange);
      --color-state-orange-bg: var(--state-orange-bg);
      --color-state-orange-text: var(--state-orange-text);
      --radius-sm: 4px;
      --radius-md: 8px;
      --radius-lg: 12px;
      --radius-xl: 16px;
      --radius-2xl: 20px;
    }}
    @layer base {{
      body {{ background: var(--jl-background); color: var(--jl-foreground); }}
      td, th {{ word-break: break-word; }}
      th {{ white-space: nowrap; }}
    }}
  </style>
</head>
<body class="min-h-screen font-sans antialiased bg-background text-foreground">
<!-- 引擎版本: {ENGINE_VERSION} ｜ 生成时间: {report.timestamp} ｜ 隐藏标识：仅查看源代码可见，用于排查问题区分生成版本 -->
<div class="max-w-[1280px] mx-auto px-4 sm:px-6 lg:px-8 py-6">

  <!-- Brand Header -->
  <header style="display:flex;flex-direction:column;gap:16px;margin-bottom:24px;">
    <div style="display:flex;align-items:center;gap:12px;">
      <img src="{_load_logo_base64()}" alt="飞柚GEO LOGO" style="height:40px;width:auto;flex-shrink:0;" />
      <div style="flex:1;">
        <div style="font-size:16px;font-weight:700;color:var(--jl-neutral-900);">GEO 诊断中心</div>
        <div style="font-size:12px;color:var(--jl-neutral-500);">让 AI 搜索优先推荐你的品牌</div>
      </div>
      <button type="button" onclick="downloadReport()" title="把本报告保存为本地 HTML 文件（离线可看，双击即开）"
              style="display:flex;align-items:center;gap:6px;padding:8px 14px;font-size:13px;font-weight:600;color:var(--jl-neutral-700);background:var(--jl-card);border:1px solid var(--jl-border);border-radius:10px;cursor:pointer;transition:all .15s;"
              onmouseover="this.style.borderColor='var(--jl-primary)';this.style.color='var(--jl-primary)'"
              onmouseout="this.style.borderColor='var(--jl-border)';this.style.color='var(--jl-neutral-700)'">
        <svg style="width:14px;height:14px;" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><polyline points="7 10 12 15 17 10"/><line x1="12" y1="15" x2="12" y2="3"/></svg>
        导出报告
      </button>
    </div>
  </header>

  <!-- Hero -->
  <section style="position:relative;overflow:hidden;border-radius:16px;background:linear-gradient(135deg,var(--jl-blue-800) 0%,var(--jl-blue-600) 100%);padding:32px 28px;color:#fff;margin-bottom:24px;box-shadow:0 4px 6px -1px rgba(15,23,42,0.05);">
    <div style="position:relative;z-index:10;">
      <div style="display:flex;align-items:center;gap:8px;font-size:14px;color:rgba(255,255,255,0.8);margin-bottom:8px;">
        <svg style="width:16px;height:16px;flex-shrink:0;" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M3 7V5a2 2 0 0 1 2-2h2"/><path d="M17 3h2a2 2 0 0 1 2 2v2"/><path d="M21 17v2a2 2 0 0 1-2 2h-2"/><path d="M7 21H5a2 2 0 0 1-2-2v-2"/><line x1="7" y1="12" x2="17" y2="12"/><path d="M12 7l5 5-5 5"/></svg>
        <span>GEO 官网诊断报告</span>
        <button type="button" onclick="openCfgModal()" style="margin-left:8px;padding:1px 8px;font-size:11px;font-weight:400;line-height:1.6;color:rgba(255,255,255,.82);background:transparent;border:1px solid rgba(255,255,255,.28);border-radius:999px;cursor:pointer;vertical-align:middle;transition:background .15s;" onmouseover="this.style.background='rgba(255,255,255,.14)'" onmouseout="this.style.background='transparent'">检查项计分配置详情</button>
      </div>
      <div style="display:flex;justify-content:space-between;align-items:flex-end;margin-left:24px;">
        <div>
          <h1 style="font-size:28px;font-weight:700;color:#fff;margin:0 0 4px 0;">{e(brand_name)}</h1>
          <p style="font-size:13px;color:rgba(255,255,255,0.7);margin:0;">结果溯源网站：{e(report.final_url)}</p>
        </div>
        <div style="text-align:right;flex-shrink:0;">
          <div style="font-size:12px;color:rgba(255,255,255,0.6);">检测时间</div>
          <div style="font-size:14px;font-weight:600;color:rgba(255,255,255,0.9);">{e(report.timestamp)}</div>
        </div>
      </div>
    </div>
    <div style="position:absolute;top:0;right:0;width:256px;height:256px;background:rgba(255,255,255,0.05);border-radius:9999px;transform:translateY(-50%) translateX(33%);filter:blur(40px);"></div>
  </section>

  <!-- Executive Summary -->
  <section style="background:var(--jl-card);border-radius:16px;padding:24px 28px;margin-bottom:20px;box-shadow:0 1px 2px rgba(15,23,42,0.04);border:1px solid var(--jl-border);">
    <div style="display:flex;align-items:center;gap:8px;font-size:14px;font-weight:600;color:var(--jl-primary);margin-bottom:12px;">
      <svg style="width:16px;height:16px;" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M12 3l1.912 5.813h6.113l-4.943 3.587 1.888 5.813L12 14.626l-4.97 3.587 1.888-5.813L3.975 8.813h6.113z"/></svg>
      <span>30 秒看懂这份报告</span>
    </div>
    <p style="font-size:18px;font-weight:700;color:var(--jl-neutral-900);line-height:1.7;">{e(headline)}</p>
  </section>

  <!-- Problem Cards -->
  <section style="display:flex;flex-direction:column;gap:16px;margin-bottom:24px;">
    {cards_html}
  </section>

  <!-- Technical Details -->
  <details open style="margin-top:8px;">
    <summary style="background:var(--jl-card);border-radius:16px;padding:20px 24px;box-shadow:0 1px 2px rgba(15,23,42,0.04);border:1px solid var(--jl-border);display:flex;align-items:center;justify-content:space-between;gap:16px;">
      <div style="display:flex;align-items:center;gap:12px;">
        <div style="width:40px;height:40px;border-radius:12px;background:var(--jl-blue-50);color:var(--jl-primary);display:flex;align-items:center;justify-content:center;">
          <svg style="width:20px;height:20px;" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M12.22 2h-.44a2 2 0 0 0-2 2v.18a2 2 0 0 1-1 1.73l-.43.25a2 2 0 0 1-2 0l-.15-.08a2 2 0 0 0-2.73.73l-.22.38a2 2 0 0 0 .73 2.73l.15.1a2 2 0 0 1 1 1.72v.51a2 2 0 0 1-1 1.74l-.15.09a2 2 0 0 0-.73 2.73l.22.38a2 2 0 0 0 2.73.73l.15-.08a2 2 0 0 1 2 0l.43.25a2 2 0 0 1 1 1.73V20a2 2 0 0 0 2 2h.44a2 2 0 0 0 2-2v-.18a2 2 0 0 1 1-1.73l.43-.25a2 2 0 0 1 2 0l.15.08a2 2 0 0 0 2.73-.73l.22-.39a2 2 0 0 0-.73-2.73l-.15-.08a2 2 0 0 1-1-1.74v-.5a2 2 0 0 1 1-1.74l.15-.09a2 2 0 0 0 .73-2.73l-.22-.38a2 2 0 0 0-2.73-.73l-.15.08a2 2 0 0 1-2 0l-.43-.25a2 2 0 0 1-1-1.73V4a2 2 0 0 0-2-2z"/><circle cx="12" cy="12" r="3"/></svg>
        </div>
        <div>
          <h2 style="font-size:16px;font-weight:700;color:var(--jl-neutral-900);margin:0;">报告详情</h2>
          <p style="font-size:12px;color:var(--jl-neutral-500);margin:4px 0 0 0;">{summary["total"]} 项全量打分表与检测证据，点击展开</p>
        </div>
      </div>
      <svg class="chev" style="width:20px;height:20px;color:var(--jl-neutral-500);" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><polyline points="6 9 12 15 18 9"/></svg>
    </summary>
    <div style="padding-top:20px;display:flex;flex-direction:column;gap:24px;">

      <!-- Row 1: 检测概览（全宽，4个指标卡，核心项口径） -->
      <section style="background:var(--jl-card);border-radius:16px;padding:24px;box-shadow:0 1px 2px rgba(15,23,42,0.04);border:1px solid var(--jl-border);">
        <div style="display:flex;align-items:baseline;justify-content:space-between;gap:12px;flex-wrap:wrap;margin-bottom:16px;">
          <div style="font-size:14px;font-weight:600;color:var(--jl-neutral-700);">检测概览</div>
          <div style="font-size:12px;color:var(--jl-neutral-500);">计分检查 {summary["core_total"]} 项；另有 {summary["bonus_total"]} 项非必检项不计分，见下方打分表</div>
        </div>
        <div style="display:grid;grid-template-columns:repeat(4,1fr);gap:16px;">
          <div style="border-radius:12px;padding:16px;background:var(--jl-muted);text-align:center;">
            <div style="font-size:28px;font-weight:700;color:var(--jl-neutral-700);">{summary["core_total"]}</div>
            <div style="font-size:12px;color:var(--jl-neutral-500);margin-top:4px;">计分检查项</div>
          </div>
          <div style="border-radius:12px;padding:16px;background:var(--state-success-bg);border:1px solid rgba(16,185,129,0.1);text-align:center;">
            <div style="font-size:28px;font-weight:700;color:var(--state-success);">{summary["core_pass"]}</div>
            <div style="font-size:12px;color:var(--state-success-text);margin-top:4px;">合格</div>
          </div>
          <div style="border-radius:12px;padding:16px;background:var(--state-warning-bg);border:1px solid rgba(245,158,11,0.1);text-align:center;">
            <div style="font-size:28px;font-weight:700;color:var(--state-warning);">{summary["core_warn"]}</div>
            <div style="font-size:12px;color:var(--state-warning-text);margin-top:4px;">不足</div>
          </div>
          <div style="border-radius:12px;padding:16px;background:var(--state-error-bg);border:1px solid rgba(239,68,68,0.1);text-align:center;">
            <div style="font-size:28px;font-weight:700;color:var(--state-error);">{summary["core_fail"]}</div>
            <div style="font-size:12px;color:var(--state-error-text);margin-top:4px;">不合格</div>
          </div>
        </div>
      </section>

      <!-- Row 2: 总得分 + 等级划分标准 (1:3) -->
      <section style="display:grid;grid-template-columns:1fr 3fr;gap:20px;align-items:stretch;">
        <div style="background:var(--jl-card);border-radius:16px;padding:24px;text-align:center;box-shadow:0 1px 2px rgba(15,23,42,0.04);border:1px solid var(--jl-border);display:flex;flex-direction:column;align-items:center;justify-content:center;">
          <div style="display:flex;align-items:baseline;gap:8px;">
            <span style="font-size:48px;font-weight:800;color:var(--{grade_color_var});line-height:1;">{report.total_score}</span>
          </div>
          <div style="font-size:14px;color:var(--jl-neutral-500);margin-top:4px;">总分 (0-100)</div>
        </div>
        <div style="background:var(--jl-card);border-radius:16px;padding:24px;box-shadow:0 1px 2px rgba(15,23,42,0.04);border:1px solid var(--jl-border);">
          <h3 style="font-size:15px;font-weight:700;color:var(--jl-neutral-900);margin:0 0 20px 0;display:flex;align-items:center;gap:8px;">
            <span style="width:4px;height:20px;border-radius:4px;background:var(--jl-primary);"></span>
            等级划分标准
          </h3>
          <div style="display:grid;grid-template-columns:repeat(5,1fr);gap:8px;">
            <div style="border-radius:12px;padding:16px 8px;text-align:center;background:var(--state-success-bg);border:1px solid rgba(16,185,129,0.1);">
              <div style="font-size:24px;font-weight:800;color:var(--state-success);">A</div>
              <div style="font-size:12px;color:var(--state-success-text);margin-top:4px;">90-100</div>
            </div>
            <div style="border-radius:12px;padding:16px 8px;text-align:center;background:var(--jl-blue-50);border:1px solid var(--jl-blue-200);">
              <div style="font-size:24px;font-weight:800;color:var(--jl-blue-500);">B</div>
              <div style="font-size:12px;color:var(--jl-blue-700);margin-top:4px;">75-89</div>
            </div>
            <div style="border-radius:12px;padding:16px 8px;text-align:center;background:var(--state-warning-bg);border:1px solid rgba(245,158,11,0.1);">
              <div style="font-size:24px;font-weight:800;color:var(--state-warning);">C</div>
              <div style="font-size:12px;color:var(--state-warning-text);margin-top:4px;">60-74</div>
            </div>
            <div style="border-radius:12px;padding:16px 8px;text-align:center;background:var(--state-orange-bg);border:1px solid rgba(249,115,22,0.1);">
              <div style="font-size:24px;font-weight:800;color:var(--state-orange);">D</div>
              <div style="font-size:12px;color:var(--state-orange-text);margin-top:4px;">40-59</div>
            </div>
            <div style="border-radius:12px;padding:16px 8px;text-align:center;background:var(--state-error-bg);border:1px solid rgba(239,68,68,0.1);">
              <div style="font-size:24px;font-weight:800;color:var(--state-error);">F</div>
              <div style="font-size:12px;color:var(--state-error-text);margin-top:4px;">0-39</div>
            </div>
          </div>
        </div>
      </section>

      <!-- Row 3: 各维度得分 + 维度权重规则 (45%:55%) -->
      <section style="display:grid;grid-template-columns:45% 55%;gap:20px;align-items:stretch;">
        {dimension_card_html}
        <div style="background:var(--jl-card);border-radius:16px;padding:24px;box-shadow:0 1px 2px rgba(15,23,42,0.04);border:1px solid var(--jl-border);">
          <h3 style="font-size:15px;font-weight:700;color:var(--jl-neutral-900);margin:0 0 20px 0;display:flex;align-items:center;gap:8px;">
            <span style="width:4px;height:20px;border-radius:4px;background:var(--jl-primary);"></span>
            维度权重规则
          </h3>
          <div style="overflow-x:auto;border-radius:12px;border:1px solid var(--jl-border);">
            <table style="width:100%;font-size:13px;border-collapse:collapse;">
              <thead style="background:var(--jl-muted);color:var(--jl-neutral-500);">
                <tr>
                  <th style="padding:10px 16px;text-align:left;font-weight:500;">维度</th>
                  <th style="padding:10px 16px;text-align:center;font-weight:500;">权重</th>
                  <th style="padding:10px 16px;text-align:center;font-weight:500;">检查项</th>
                  <th style="padding:10px 16px;text-align:center;font-weight:500;">得分</th>
                </tr>
              </thead>
              <tbody>{weight_rows}</tbody>
            </table>
          </div>
          <div style="font-size:13px;color:var(--jl-neutral-600);line-height:1.8;margin-top:16px;">
            <div style="font-weight:600;color:var(--jl-neutral-700);margin-bottom:8px;">单项维度示例（技术可抓取性得分）</div>
            <div>1. 10项合格 × 1分/项 = 10分</div>
            <div>2. 2项不足 × 0.5分/项 = 1分</div>
            <div>3. 1项不合格 × 0分/项 = 0分</div>
            <div>4. (10+1+0) / 13个检测项 × 100 = 84.6分</div>
            <div style="font-weight:600;color:var(--jl-neutral-700);margin-top:12px;margin-bottom:8px;">总分示例</div>
            <div>1. 单项积分：30% × 84.6分 = 25.3</div>
            <div>2. 总分：各单项积分累计总和</div>
          </div>
        </div>
      </section>

      <!-- CMS诊断 -->
      {cms_section_html}

      <!-- 多页面抽样 -->
      {sampled_pages_html}

      <!-- 检查项计分配置详情弹窗 -->
      {config_modal_html}

      <!-- 待优化清单 -->
      {todo_list_html}

      <!-- 详细诊断结果 -->
      <section>
        <h3 style="font-size:15px;font-weight:700;color:var(--jl-neutral-900);margin:0 0 4px 0;display:flex;align-items:center;gap:8px;">
          <span style="width:4px;height:20px;border-radius:4px;background:var(--jl-neutral-700);"></span>
          详细诊断结果
        </h3>
        <p style="font-size:12px;color:var(--jl-neutral-500);margin:0 0 16px 12px;">每项包含：手动检查方法、作用说明、三级标准（合格/不足/不合格）</p>
        <div style="display:flex;flex-direction:column;gap:24px;">
          {detail_sections}
        </div>
      </section>

    </div>
  </details>

  <!-- Footer -->
  <footer style="margin-top:32px;padding-top:24px;text-align:center;font-size:12px;color:var(--jl-neutral-400);">
    本报告由{report_provider}提供
  </footer>

</div>
<script>
function downloadReport(){{
  // 导出当前报告为本地 HTML：logo 已内嵌 base64、样式内联，单文件离线可看
  const html = '<!DOCTYPE html>\\n' + document.documentElement.outerHTML;
  const blob = new Blob([html], {{type: 'text/html;charset=utf-8'}});
  const a = document.createElement('a');
  a.href = URL.createObjectURL(blob);
  const title = (document.querySelector('title') || {{textContent: 'GEO诊断报告'}}).textContent.trim().replace(/[\\\\/:*?"<>|]/g, '_');
  a.download = title + '.html';
  document.body.appendChild(a);
  a.click();
  a.remove();
  setTimeout(() => URL.revokeObjectURL(a.href), 3000);
}}
function switchDetailTab(tab){{
  document.getElementById('detail-tab-seo').classList.toggle('active',tab==='seo');
  document.getElementById('detail-tab-geo').classList.toggle('active',tab==='geo');
  document.getElementById('detail-tab-seo-content').style.display=tab==='seo'?'flex':'none';
  document.getElementById('detail-tab-geo-content').style.display=tab==='geo'?'flex':'none';
}}
</script>
</body>
</html>'''
    return html


def save_html_report(report: AuditReport, output_path: str):
    """保存 HTML 报告"""
    html = generate_html_report(report)
    with open(output_path, "w", encoding="utf-8") as f:
        f.write(html)
    return output_path
