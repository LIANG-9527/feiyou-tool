"""Prompt templates for all pipeline steps."""
from __future__ import annotations
import json
from reference_data import COMBINATION_TABLE, COMPARISON_LEVELS, B2B_B2C_EXAMPLES, CORE_PRINCIPLES

SYSTEM = (
    "你是一位 GEO(生成式引擎优化)搜索问句挖掘与意图分析专家。\n"
    "你的任务是根据品牌名称,产出完整的搜索问句与意图分析,用于指导内容投放与差异化销售策略。\n\n"
    "核心原则(所有环节强制遵守):\n"
    + "\n".join(f"{i+1}. {p}" for i, p in enumerate(CORE_PRINCIPLES))
    + "\n\n输出要求: 严格按要求的JSON格式输出,不要添加markdown代码块标记或额外说明文字。"
)


# ── Step 1 · 核心产品/服务清单 ──────────────────────────

def step1(brand: str) -> list[dict]:
    return [
        {"role": "system", "content": SYSTEM},
        {"role": "user", "content": (
            f"## 任务\n调研品牌「{brand}」近半年对外推广销售的核心产品/服务。\n\n"
            "## 要求\n"
            "1. 输出1~3个核心产品/服务\n"
            "2. 每个附: 一句话定位、目标客群、近半年主推依据(基于公开信息)\n"
            "3. 不确定的信息标注'待核验'\n\n"
            "## 输出格式(JSON数组)\n"
            '[\n'
            '  {\n'
            '    "product_name": "产品/服务名称",\n'
            '    "positioning": "一句话定位",\n'
            '    "target_audience": "目标客群简述",\n'
            '    "promotion_basis": "近半年主推依据"\n'
            '  }\n'
            ']'
        )},
    ]


# ── Step 1.5 · 名称变体采集 ──────────────────────────

def step1_5(products: list) -> list[dict]:
    products_text = json.dumps(products, ensure_ascii=False, indent=2)
    return [
        {"role": "system", "content": SYSTEM},
        {"role": "user", "content": (
            "## 任务\n"
            "对以下每个产品/服务输出「名称变体集合」。官方宣传名≠用户真实搜索名,名称是搜索问句命中的基础词库。\n\n"
            f"## 产品清单\n{products_text}\n\n"
            "## 要求\n"
            "每个产品必须覆盖6类变体:\n"
            "1. 官方全称\n2. 简称缩写\n3. 行业术语\n4. 口语俗称\n5. 场景化说法\n6. 英文名\n"
            "每个变体标注'who_searches'(通常谁在搜,即客群信号)。\n"
            "后续所有问句必须覆盖这些变体。\n\n"
            "## 输出格式(JSON对象)\n"
            '{\n'
            '  "产品名1": [\n'
            '    {"type": "官方全称", "variant": "...", "who_searches": "..."},\n'
            '    {"type": "简称缩写", "variant": "...", "who_searches": "..."},\n'
            '    {"type": "行业术语", "variant": "...", "who_searches": "..."},\n'
            '    {"type": "口语俗称", "variant": "...", "who_searches": "..."},\n'
            '    {"type": "场景化说法", "variant": "...", "who_searches": "..."},\n'
            '    {"type": "英文名", "variant": "...", "who_searches": "..."}\n'
            '  ]\n'
            '}'
        )},
    ]


# ── Step 2 · 目标客群: 5线发散 ──────────────────────────

STEP2_LINES = {
    "A": "需求动机线: 用户需要它解决什么问题? 从痛点、需求、渴望出发分析",
    "B": "场景人群线: 在什么场景下、什么人群会用? 从使用场景、人群画像出发分析",
    "C": "决策心理线: 用户做决策时心理想什么? 从恐惧、期待、认知偏差出发分析",
    "D": "价值分层线: 不同预算/价值取向的人怎么选? 从价格敏感度、价值认知出发分析",
    "E": "组织属性线: B端还是C端? 决策角色是谁? 从组织属性、决策链条出发分析",
}

def step2_line(line_key: str, products: list, variants: dict) -> list[dict]:
    line_desc = STEP2_LINES[line_key]
    context = json.dumps({"products": products, "variants": variants}, ensure_ascii=False)
    return [
        {"role": "system", "content": SYSTEM},
        {"role": "user", "content": (
            f"## 任务\n"
            f"你是客群分析的推理线「{line_desc}」。\n"
            f"请从这条线出发,分析品牌目标客群。\n\n"
            f"## 品牌信息\n{context}\n\n"
            "## 要求\n"
            "产出的洞察要具体、有客群信号(谁在问/什么心理)。\n\n"
            "## 输出格式(JSON对象)\n"
            '{\n'
            '  "line": "推理线名称",\n'
            '  "insights": [\n'
            '    {"insight": "洞察描述", "evidence": "依据", "audience_hint": "客群信号"}\n'
            '  ]\n'
            '}'
        )},
    ]

def step2_aggregate(line_results: list, products: list, variants: dict) -> list[dict]:
    lines_text = json.dumps(line_results, ensure_ascii=False, indent=2)
    context = json.dumps({"products": products, "variants": variants}, ensure_ascii=False)
    return [
        {"role": "system", "content": SYSTEM},
        {"role": "user", "content": (
            "## 任务\n"
            "以下是5条并行推理线对目标客群的分析结果。请聚合为3~5类客群画像。\n\n"
            f"## 品牌信息\n{context}\n\n"
            f"## 5条推理线结果\n{lines_text}\n\n"
            "## 要求\n"
            "聚合为3~5类客群画像,每类固定5个字段:\n"
            "1. 一句话定义\n2. 核心特征(2~3条)\n3. 典型动机\n4. 决策习惯\n5. 对应销售策略方向\n\n"
            "## 输出格式(JSON数组)\n"
            '[\n'
            '  {\n'
            '    "name": "客群名称",\n'
            '    "definition": "一句话定义",\n'
            '    "core_traits": ["特征1", "特征2"],\n'
            '    "typical_motivation": "典型动机",\n'
            '    "decision_habit": "决策习惯",\n'
            '    "sales_strategy": "对应销售策略方向"\n'
            '  }\n'
            ']'
        )},
    ]


# ── Step 3 · 决策旅程 ──────────────────────────

def step3(products: list, variants: dict, personas: list) -> list[dict]:
    context = json.dumps({
        "products": products,
        "variants": variants,
        "personas": personas,
    }, ensure_ascii=False)
    return [
        {"role": "system", "content": SYSTEM},
        {"role": "user", "content": (
            "## 任务\n"
            "并行参考以下决策模型,对齐映射为统一阶段框架(默认6阶段,B/C端可微调):\n"
            "- AIDMA (注意→兴趣→欲望→记忆→行动)\n"
            "- AISAS (注意→兴趣→搜索→行动→分享)\n"
            "- 消费者决策5阶段 (认知需求→信息搜索→方案评估→购买决策→购后行为)\n"
            "- Google微时刻 (I-want-to-know / go / do / buy)\n"
            "- JTBD旅程 (任务触发→评估→选择→使用→反馈)\n"
            "- B端模型 (需求识别→供应商评估→采购→交付→运营)\n\n"
            f"## 品牌信息\n{context}\n\n"
            "## 要求\n"
            "输出统一阶段框架,每阶段标注: 用户此刻状态 + 核心关注问题。\n\n"
            "## 输出格式(JSON数组)\n"
            '[\n'
            '  {\n'
            '    "stage_index": 1,\n'
            '    "stage_name": "阶段名称",\n'
            '    "user_state": "用户此刻状态",\n'
            '    "core_concern": "核心关注问题"\n'
            '  }\n'
            ']'
        )},
    ]


# ── Step 3.5 · 双搜索入口 + 入口B生成规范 ──────────────────────────

def step3_5(personas: list, journey: list, variants: dict) -> list[dict]:
    context = json.dumps({
        "personas": personas,
        "journey": journey,
        "variants": variants,
    }, ensure_ascii=False)
    combo_rules = (
        "入口B问句生成规则(信息要素组合表):\n"
        "7个要素: S场景/O对象(必含)/D抉择/C约束/E顾虑/G目标/V验证\n"
        "有效组合: 必须含O + 至少1个其他要素\n"
        "三级标准: 最低(2要素6种)/中(3要素15种,最自然)/最佳(4要素20种)\n"
        "要素只决定'包含什么信息',不决定'怎么说',允许顺序/倒装/省略/碎片/情绪化变体\n"
        "自检判据: '有人能一眼看出用户想解决什么具体问题吗?'"
    )
    return [
        {"role": "system", "content": SYSTEM},
        {"role": "user", "content": (
            "## 任务\n"
            "真实用户有两条搜索路径,每个阶段都要覆盖:\n"
            "- 入口A(产品名入口): 用户已知产品名→直接搜知识、品牌推荐、解决方案、案例\n"
            "- 入口B(问题入口): 用户只知道问题/需求→搜解决方案→得知产品名→再进入案例、知识、推荐\n\n"
            f"## 品牌信息\n{context}\n\n"
            f"## {combo_rules}\n\n"
            "## 要求\n"
            "为每个阶段定义双入口方向,并给出入口B的要素组合提示(哪些要素组合最适合该阶段)。\n\n"
            "## 输出格式(JSON数组,与阶段一一对应)\n"
            '[\n'
            '  {\n'
            '    "stage_index": 1,\n'
            '    "entry_a": "产品名入口方向: 用户已知产品名后搜什么",\n'
            '    "entry_b": "问题入口方向: 用户只知道问题时搜什么",\n'
            '    "entry_b_combination_hints": ["S+O+D", "O+C+E"]\n'
            '  }\n'
            ']'
        )},
    ]


# ── Step 4 · 每阶段话题拆解: 6条推理线并行 ──────────────────────────

STEP4_LINES = {
    "A": {
        "name": "线A 5W2H",
        "instruction": (
            "从5W2H拆解: 是什么/给谁/什么场景/怎么用/多少钱/在哪/何时。\n"
            "每个W/H方向产出一组问句。"
        ),
    },
    "B": {
        "name": "线B 黄金圈(Why/What/How)",
        "instruction": (
            "从黄金圈拆解: 为什么值得(Why)、解决什么问题(What)、怎么做(How)。\n"
            "每个方向产出一组问句。"
        ),
    },
    "C": {
        "name": "线C 因果逻辑链",
        "instruction": (
            "从因果逻辑链拆解: 用了会带来什么收益、代价、风险。\n"
            "从收益、代价、风险三个方向各产出问句。"
        ),
    },
    "D": {
        "name": "线D JTBD",
        "instruction": (
            "从JTBD拆解: 被什么场景或情绪触发、想完成什么任务。\n"
            "问题型问句按信息要素组合表生成(必须含O+至少1个其他要素)。\n"
            "覆盖最低(2要素)/中(3要素)/最佳(4要素)三级。\n"
            "允许顺序/倒装/省略/碎片/情绪化变体。"
        ),
    },
    "E": {
        "name": "线E 搜索意图四分",
        "instruction": (
            "从搜索意图四分拆解: 想知道/想去做/想去买/想去哪。\n"
            "'想去买'扩展覆盖品类品牌推荐。\n"
            "'对比'类覆盖三层级:\n"
            "  第一层·方案形态对比(最上游,如成品vs定制vs木工)\n"
            "  第二层·品牌对比(中间层,如品牌A vs 品牌B)\n"
            "  第三层·套餐/型号对比(最下游,如系列A vs 系列B)\n"
            "三层必须都有问句。"
        ),
    },
    "F": {
        "name": "线F 情绪与风险",
        "instruction": (
            "从情绪与风险拆解: 劝退、后悔、害怕、犹豫、求种草/求骂醒、突发状况。\n"
            "每个方向产出问句,允许负面/情绪词。"
        ),
    },
}

def step4_line(line_key: str, stage: dict, products: list, variants: dict,
               personas: list, dual_entry: dict) -> list[dict]:
    line_info = STEP4_LINES[line_key]
    context = json.dumps({
        "products": products,
        "variants": variants,
        "personas": personas,
        "stage": stage,
        "dual_entry": dual_entry,
    }, ensure_ascii=False)
    return [
        {"role": "system", "content": SYSTEM},
        {"role": "user", "content": (
            f"## 任务\n"
            f"你是阶段「{stage.get('stage_name', '')}」的推理线「{line_info['name']}」。\n"
            f"{line_info['instruction']}\n\n"
            f"## 上下文\n{context}\n\n"
            "## 要求\n"
            "1. 产出候选话题+搜索问句,每话题≥5句\n"
            "2. 产品名问句(入口A)和问题型问句(入口B)都要有\n"
            "3. 问句必须是真人会搜的口语表达,允许负面/情绪词\n"
            "4. 每句标注'audience_signal'(谁在问/什么心理/对应策略)\n"
            "5. 不臆造,不确定标注'待核验'\n\n"
            "## 输出格式(JSON对象)\n"
            '{\n'
            '  "line": "线名",\n'
            '  "stage_index": 1,\n'
            '  "topics": [\n'
            '    {\n'
            '      "topic": "话题名",\n'
            '      "queries": [\n'
            '        {"query": "问句", "entry_type": "产品名问句|问题型问句", "audience_signal": "客群信号"}\n'
            '      ]\n'
            '    }\n'
            '  ]\n'
            '}'
        )},
    ]

def step4_gapcheck(stage: dict, line_results: list) -> list[dict]:
    lines_text = json.dumps(line_results, ensure_ascii=False, indent=2)
    return [
        {"role": "system", "content": SYSTEM},
        {"role": "user", "content": (
            f"## 任务\n"
            f"对阶段「{stage.get('stage_name', '')}」的6条推理线结果做补漏检查。\n\n"
            f"## 6条线结果\n{lines_text}\n\n"
            "## 补漏检查清单(逐条检查,缺则补):\n"
            "① 本阶段用户会被什么负面情绪/突发状况勾着去搜?\n"
            "② 本阶段用户会搜'劝退/后悔/吐槽'类词吗?\n"
            "③ 四类搜索意图(想知道/想去做/想去买/想去哪)都覆盖了吗? 缺哪类补哪类\n"
            "④ 有没有'问题描述型'搜索(只描述问题、不带产品名)? 缺则补\n"
            "⑤ 用户会不会搜'这个品类有哪些品牌/服务商、推荐理由可不可信、怎么验证'? 缺则补\n"
            "⑥ '对比'类问句是否覆盖三层级(方案形态→品牌→套餐/型号)? 缺哪层补哪层\n\n"
            "## 要求\n"
            "只产出补充的问句(已有的不重复)。如果某项已覆盖则跳过。\n"
            "每句标注entry_type和audience_signal。\n\n"
            "## 输出格式(JSON对象)\n"
            '{\n'
            '  "stage_index": 1,\n'
            '  "gap_fills": [\n'
            '    {"check_item": "检查项编号", "query": "补充问句", "entry_type": "产品名问句|问题型问句", "audience_signal": "客群信号"}\n'
            '  ]\n'
            '}'
        )},
    ]


# ── Step 5 · 阶段末汇流(只归组,不删句) ──────────────────────────

def step5(stage: dict, all_queries: list, personas: list) -> list[dict]:
    context = json.dumps({
        "stage": stage,
        "all_queries": all_queries,
        "personas": personas,
    }, ensure_ascii=False)
    return [
        {"role": "system", "content": SYSTEM},
        {"role": "user", "content": (
            "## 任务\n"
            "对以下阶段的全部问句进行汇流归组。\n\n"
            f"## 数据\n{context}\n\n"
            "## 要求(强制)\n"
            "1. 收集该阶段全部问句\n"
            "2. 按'背后核心需求是否相同'归入意图簇(一句只进一簇)\n"
            "3. 禁止删除或合并任何问句,全部保留\n"
            "4. 每簇命名(口语化标签)\n"
            "5. 簇内每个问句标注'audience_signal': 通常谁在问/什么消费心理/对应什么销售策略方向\n\n"
            "## 输出格式(JSON数组)\n"
            '[\n'
            '  {\n'
            '    "cluster_name": "意图簇名称(口语化)",\n'
            '    "queries": [\n'
            '      {"query": "问句", "entry_type": "产品名问句|问题型问句", "audience_signal": "客群信号"}\n'
            '    ]\n'
            '  }\n'
            ']'
        )},
    ]


# ── Step 6 · 意图簇反演验证 ──────────────────────────

def step6(all_clusters: dict) -> list[dict]:
    clusters_text = json.dumps(all_clusters, ensure_ascii=False, indent=2)
    return [
        {"role": "system", "content": SYSTEM},
        {"role": "user", "content": (
            "## 任务\n"
            "对每个意图簇做反演验证:\n"
            "1. 仅凭每个意图簇的标签,反演生成代表性搜索问句\n"
            "2. 核对反演问句能否覆盖该簇全部原始问句的核心需求\n"
            "3. 明显偏离→调整标签或重新归簇\n"
            "4. 输出每簇结论: 一致/需调整\n\n"
            f"## 全部意图簇\n{clusters_text}\n\n"
            "## 输出格式(JSON数组)\n"
            '[\n'
            '  {\n'
            '    "stage_index": 1,\n'
            '    "cluster_name": "意图簇名称",\n'
            '    "reverse_queries": ["反演生成的代表性问句"],\n'
            '    "coverage_check": "覆盖|部分覆盖|偏离",\n'
            '    "adjustment": "无需调整|需调整标签|需重新归簇",\n'
            '    "conclusion": "一致|需调整"\n'
            '  }\n'
            ']'
        )},
    ]


# ── 自校验清单 ──────────────────────────

SELF_CHECK_ITEMS = [
    "名称变体全覆盖(6类变体×每个产品)",
    "双搜索入口齐全(入口A产品名+入口B问题型)",
    "每阶段多话题(每阶段≥3个话题)",
    "6线全覆盖(5W2H/黄金圈/因果链/JTBD/搜索意图四分/情绪风险)",
    "品类品牌推荐与可信度验证问句已覆盖",
    "对比三层级全覆盖(方案形态→品牌→套餐/型号)",
    "问句零删除(所有问句全部保留)",
    "一句一簇(每个问句只归入一个意图簇)",
    "反演一致(意图簇标签反演能覆盖原始问句核心需求)",
]
