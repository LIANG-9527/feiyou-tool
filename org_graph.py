"""组织架构强化：种子标准化 → BFS 逐层抽取 → 关系归类，产出 org.json。

数据纪律（对应 docs 设计稿）：
- 只提取检索结果中明确出现的信息，禁止推断；没有的字段一律 null
- credit_code 必须过 18 位校验；is_holding 三值：True(全资/控股/≥50%) / False(参股或<50%) / None(不足判断)
- 节点 id 归一：信用代码优先，无码用归一化公司名临时 id
- 剪枝：仅 is_holding=True 的下级继续展开；参股停在叶上
- 止损：种子层对外投资与公司股东都为空 → 返回 None，回落旧组织数据
"""
import asyncio
import json
import re

CREDIT_RE = re.compile(r"^[0-9A-HJ-NPQRTUWXY]{2}\d{6}[0-9A-HJ-NPQRTUWXY]{10}$")
_MAX_NODES = 25  # BFS 节点上限，防成本失控

SYSTEM = ("你是企业工商信息抽取员。你具备联网搜索能力——先实际执行网络检索再作答，"
          "禁止声称无法访问互联网或没有检索工具。"
          "只输出检索结果中明确出现的信息，禁止凭记忆推断；检索不到的字段一律 null/空数组。仅输出 JSON。")

SEED_PROMPT = """## 任务：确认企业主体身份（种子标准化）
目标企业：「{company}」
请联网检索：
1. 「{company} 统一社会信用代码 法定代表人」
2. 「{company} 创始人 董事长 百度百科」
检索结果含百度百科/公司官网/全国企业信用信息公示系统内容时优先采信。输出 JSON（只提取检索结果中明确出现的信息）：
{{
  "name": "标准注册名",
  "credit_code": "18位统一社会信用代码，检索不到则 null",
  "aliases": ["曾用名/别名，仅列出检索结果中出现过的"],
  "legal_person": "法定代表人，检索不到则 null",
  "founder": "创始人/实际控制人，检索结果明确提及才填，否则 null",
  "status": "存续/注销/吊销等，检索不到则 null",
  "match_confidence": "high 或 low",
  "evidence": "关键依据的一句话原文摘录"
}}
判定规则：
- credit_code 必须为 18 位字母数字；多源不一致取出现次数最多的
- 曾用名是必填项：缺少曾用名信息时 match_confidence 最高只能给 low
- 只输出 JSON"""

HOLDERS_PROMPT = """## 任务：抽取企业「{name}」的公司股东
请联网检索「{name} 股东 股权结构 持股比例」等信息后，输出 JSON：
{{
  "corporate_holders": [
    {{"name": "公司股东全称", "credit_code": "检索到则填否则 null", "is_holding": true/false/null,
      "signal": "检索结果中的持股比例或字样原文", "evidence": "原文摘录一句话"}}
  ],
  "has_natural_person_holder": true/false,
  "uncertain": [{{"claim": "疑似但无法确认的表述", "reason": "为什么无法确认"}}]
}}
规则：
1. corporate_holders 只收【公司】股东；自然人一律不进数组，但存在自然人股东时 has_natural_person_holder 必须为 true
2. is_holding 三值：signal 含"100%/全资/控股"或比例≥50% → true；含"参股/投资"且<50%或无比例 → false；信息不足 → null
3. 条目必须能与「{name}」对应（同段/同句出现），无法确认对应关系的写进 uncertain
4. credit_code 检索到就填，禁止猜测。只输出 JSON"""

ENRICH_PROMPT = """## 任务：批量确认企业统一社会信用代码
请逐家联网检索「公司名 统一社会信用代码」，输出 JSON：
{{"results":[{{"name":"公司名（照抄输入）","credit_code":"18位代码，检索不到则 null","evidence":"代码出处一句话摘录"}}]}}
待查公司清单：
{names}
规则：
1. credit_code 必须 18 位字母数字，检索不到或同名企业无法判定时填 null，禁止编造
2. 同名企业按注册地/经营范围挑最可能的一家；判断不了就 null
3. 只输出 JSON"""

INVEST_PROMPT = """## 任务：抽取企业「{name}」的对外投资
请联网检索「{name} 对外投资 子公司 全资 控股」等信息后，输出 JSON：
{{
  "investments": [
    {{"name": "被投资公司全称", "credit_code": "检索到则填否则 null", "is_holding": true/false/null,
      "signal": "检索结果中的持股比例或字样原文", "evidence": "原文摘录一句话"}}
  ],
  "uncertain": [{{"claim": "疑似但无法确认的表述", "reason": "为什么无法确认"}}]
}}
规则：
1. 只收【公司】类型的被投资对象；分支机构/分公司不进数组（可进 uncertain 说明）
2. is_holding 三值：含"100%/全资/控股"或比例≥50% → true；"参股"或<50% → false；信息不足 → null
3. 无法确认与「{name}」对应关系的写进 uncertain。credit_code 检索到就填，禁止猜测。只输出 JSON"""


def _norm(name: str) -> str:
    return re.sub(r"[\s（）()]", "", str(name or ""))


def _entity_key(item: dict) -> str:
    code = str(item.get("credit_code") or "").strip().upper()
    if CREDIT_RE.match(code):
        return "c:" + code
    return "n:" + _norm(item.get("name"))


async def _chat_json(llm, prompt: str) -> dict:
    try:
        res = await llm.chat([{"role": "system", "content": SYSTEM},
                              {"role": "user", "content": prompt}], expect_json=True)
        return res if isinstance(res, dict) else {}
    except Exception:
        return {}


class OrgGraphBuilder:
    def __init__(self, llm, max_depth: int = 2):
        # max_depth：股权展开深度，可按需增加——
        #   2（当前默认）= 目标企业 + 一层控股子公司（实测约 40~130 次联网调用、1~2.5 分钟）；
        #   3 = 继续展开孙公司，调用次数与每层实体数成正比，受 _MAX_NODES=25 上限与 540s 超时双重保护；
        #   调低到 1 只保留母公司层，适合小客户快速出报告。
        self.llm = llm
        self.max_depth = max_depth
        self.sem = asyncio.Semaphore(4)

    async def _extract(self, name: str, credit_code) -> dict:
        holders_q = HOLDERS_PROMPT.format(name=name)
        invest_q = INVEST_PROMPT.format(name=name)
        h, inv = await asyncio.gather(self._one(holders_q), self._one(invest_q))
        ext = {"self": {"name": name, "credit_code": credit_code},
               "corporate_holders": h.get("corporate_holders") or [],
               "investments": inv.get("investments") or [],
               "has_natural_person_holder": bool(h.get("has_natural_person_holder")),
               "uncertain": (h.get("uncertain") or []) + (inv.get("uncertain") or [])}
        if not ext["corporate_holders"] and not ext["investments"]:
            # 内容级重试：内置搜索单次波动大，两路全空再试一轮
            h2, inv2 = await asyncio.gather(self._one(holders_q), self._one(invest_q))
            ext["corporate_holders"] = h2.get("corporate_holders") or []
            ext["investments"] = inv2.get("investments") or []
            ext["has_natural_person_holder"] = ext["has_natural_person_holder"] or bool(h2.get("has_natural_person_holder"))
            ext["uncertain"] += (h2.get("uncertain") or []) + (inv2.get("uncertain") or [])
        return ext

    async def _one(self, prompt: str) -> dict:
        async with self.sem:
            return await _chat_json(self.llm, prompt)

    async def run(self, company: str) -> dict | None:
        seed_raw = await _chat_json(self.llm, SEED_PROMPT.format(company=company))
        if not seed_raw or not seed_raw.get("name") or not seed_raw.get("credit_code"):
            seed_raw2 = await _chat_json(self.llm, SEED_PROMPT.format(company=company)) or {}
            if not (seed_raw or {}).get("name"):
                seed_raw = seed_raw2
            elif not (seed_raw or {}).get("credit_code") and seed_raw2.get("credit_code"):
                seed_raw = seed_raw2  # 代码/法人等以更完整的一侧为准
        if not seed_raw or not seed_raw.get("name"):
            return None
        seed = {"name": str(seed_raw["name"]), "credit_code": seed_raw.get("credit_code") or None,
                "aliases": seed_raw.get("aliases") or [], "legal_person": seed_raw.get("legal_person"),
                "founder": seed_raw.get("founder"), "status": seed_raw.get("status"),
                "match_confidence": seed_raw.get("match_confidence") or "low",
                "evidence": seed_raw.get("evidence")}
        if seed["credit_code"] and not CREDIT_RE.match(str(seed["credit_code"]).strip().upper()):
            seed["credit_code"] = None  # 18 位校验不过按无码处理
        key = _entity_key(seed)
        layers = {key: {"self": seed, "corporate_holders": [], "investments": [],
                        "has_natural_person_holder": False, "uncertain": []}}
        frontier = [(key, seed["name"], seed["credit_code"], 0)]
        while frontier:
            nxt = []
            results = await asyncio.gather(*[self._expand(k, n, c) for k, n, c, _ in frontier])
            for (k, _n, _c, depth), ext in zip(frontier, results):
                layers[k] = ext
                if depth >= self.max_depth or len(layers) >= _MAX_NODES:
                    continue
                for inv in ext["investments"]:
                    if inv.get("is_holding") is True:  # 剪枝：只有控股下级继续展开
                        ck = _entity_key(inv)
                        if ck not in layers and len(layers) < _MAX_NODES:
                            nxt.append((ck, inv["name"], inv.get("credit_code"), depth + 1))
            frontier = nxt
        await self._enrich(layers)
        org = self._build(seed, key, layers)
        org["depth2_empty"] = any(
            org["nodes"] and n.get("depth") == 1 and not any(e["parent"] == n["id"] for e in org["tree"])
            for n in org["nodes"])
        return org

    async def _expand(self, key, name, code):
        return await self._extract(name, code)

    @staticmethod
    def _align(key: str, name: str, nodes: dict) -> str:
        nm = _norm(name)
        if len(nm) >= 6:
            for k, n in nodes.items():
                on = _norm(n.get("name"))
                if on and min(len(nm), len(on)) >= 6 and (nm in on or on in nm):
                    return k
        return key

    async def _enrich(self, layers: dict):
        """对无码实体按 6 家一批联网补查统一社会信用代码，回填后边置信升 medium。"""
        need = [(k, v["self"]["name"]) for k, v in layers.items()
                if not (v["self"].get("credit_code") and CREDIT_RE.match(str(v["self"]["credit_code"]).strip().upper()))]
        if not need:
            return

        async def batch(items):
            names = "\n".join(f"{i + 1}. {n}" for i, (_k, n) in enumerate(items))
            res = await self._one(ENRICH_PROMPT.format(names=names))
            by_name = {_norm(r.get("name")): r for r in (res.get("results") or []) if isinstance(r, dict)}
            for k, n in items:
                r = by_name.get(_norm(n))
                code = str((r or {}).get("credit_code") or "").strip().upper()
                if r and CREDIT_RE.match(code):
                    layers[k]["self"]["credit_code"] = code

        await asyncio.gather(*[batch(need[i:i + 6]) for i in range(0, len(need), 6)])

    def _build(self, seed, seed_key, layers) -> dict:
        # 层键合并：名字归一后与种子互相包含的层视为同一实体（不同检索源给出的码可能互相矛盾）
        seed_nm = _norm(seed.get("name"))
        remap = {}
        for k, v in list(layers.items()):
            if k == seed_key:
                continue
            nm = _norm(v["self"].get("name"))
            if seed_nm and nm and min(len(nm), len(seed_nm)) >= 6 and (nm in seed_nm or seed_nm in nm):
                remap[k] = seed_key
                cur = layers[seed_key]["self"].get("credit_code")
                if not (cur and CREDIT_RE.match(str(cur).strip().upper())):
                    layers[seed_key]["self"]["credit_code"] = v["self"].get("credit_code")
        for k, tgt in remap.items():
            mv = layers.pop(k)
            layers[tgt]["corporate_holders"] += mv["corporate_holders"]
            layers[tgt]["investments"] += mv["investments"]
            layers[tgt]["uncertain"] += mv["uncertain"]
            layers[tgt]["has_natural_person_holder"] = (layers[tgt]["has_natural_person_holder"]
                                                        or mv["has_natural_person_holder"])
        nodes, tree, weak, uncertain = {}, [], [], []
        for key, ext in layers.items():
            nodes[key] = {**ext["self"], "id": key}
            for h in ext["corporate_holders"]:
                hk = self._align(_entity_key(h), h.get("name"), nodes)
                if hk not in nodes and hk != key:
                    nodes[hk] = {**h, "id": hk}
                if hk != key:
                    self._edge(h, nodes, tree, weak, hk, key, seed_key)
            for inv in ext["investments"]:
                ik = self._align(_entity_key(inv), inv.get("name"), nodes)
                if ik == key or ik not in nodes and ik != key:
                    if ik != key:
                        nodes[ik] = {**inv, "id": ik}
                    if ik != key:
                        self._edge(inv, nodes, tree, weak, key, ik, seed_key)
            uncertain += ext["uncertain"]
        tree = [e for e in tree if e["parent"] in nodes and e["child"] in nodes]
        # 去重 + 循环/噪音边降级：目标企业不应有上级，指向种子的控股边移入存疑
        seen_pairs, dedup = set(), []
        for e in tree:
            pair = (e["parent"], e["child"])
            if pair in seen_pairs:
                continue
            seen_pairs.add(pair)
            if e["child"] == seed_key:
                uncertain.append({"claim": f"{nodes[e['parent']].get('name')} 被检索为持有目标企业",
                                  "reason": "目标企业不应存在上级，判定为检索噪音，已忽略"})
                continue
            dedup.append(e)
        tree = dedup
        has_parent = {e["child"] for e in tree}
        has_children = {e["parent"] for e in tree}
        # 根 = 控股树上无入边的节点；孤点（仅参股关系）不算根，也不进树图
        roots = [k for k in nodes if k not in has_parent and (k in has_children or k == seed_key)]
        roots.sort(key=lambda k: k != seed_key)
        by_parent = {}
        for e in tree:
            by_parent.setdefault(e["parent"], []).append(e["child"])
        siblings = [v for v in by_parent.values() if len(v) > 1]
        depth = {}
        for r in roots:
            stack = [(r, 0)]
            while stack:
                k, d = stack.pop()
                if depth.get(k, 99) <= d:
                    continue
                depth[k] = d
                stack += [(c, d + 1) for e in tree if e["parent"] == k for c in [e["child"]]]
        for k in nodes:
            nodes[k]["depth"] = depth.get(k, 99)
        return {"roots": roots, "seed_key": seed_key, "nodes": list(nodes.values()),
                "tree": tree, "weak": weak, "siblings": siblings,
                "has_natural_person_holder": any(x.get("has_natural_person_holder") for x in layers.values()),
                "uncertain": uncertain[:20]}

    @staticmethod
    def _edge(rel: dict, nodes, tree, weak, parent, child, seed_key=None):
        conf = "medium" if rel.get("credit_code") else "low"
        if rel.get("is_holding") is False:
            if child != seed_key and parent != child:
                weak.append({"parent": parent, "child": child, "rel": "participation"})
        else:
            tree.append({"parent": parent, "child": child, "rel": "subsidiary",
                         "confidence": conf,
                         "signal": str(rel.get("signal") or "")[:40]})


async def build_org_graph(llm, company: str) -> dict | None:
    """对外入口：返回 org.json 或 None（种子层无果/异常时回落旧数据）。"""
    try:
        return await asyncio.wait_for(OrgGraphBuilder(llm).run(company), timeout=540)
    except Exception:
        return None
