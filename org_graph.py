"""组织架构：工商API（详情+股东+分支机构）直接构建 org.json，0次LLM调用。

数据纪律（对应 docs 设计稿）：
- credit_code 必须过 18 位校验
- 持股≥50% 的公司型股东作母公司根；自然人/员工持股计划只置标志不进图
- 分支机构挂种子下级（存续优先），rel="branch"
- API 无果/无树可建 → ensure_org_graph 抛 OrgGraphError 终止任务（不降级：
  生成前已有企业名称API校验 + 画像层校验两层前置，仍失败属真异常）
"""
import re

CREDIT_RE = re.compile(r"^[0-9A-HJ-NPQRTUWXY]{2}\d{6}[0-9A-HJ-NPQRTUWXY]{10}$")
_MAX_NODES = 25  # 分支机构节点上限

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


class OrgGraphError(Exception):
    """组织架构工商API获取失败——按约定终止生成并向用户报错（不降级）。"""


def _norm(name: str) -> str:
    return re.sub(r"[\s（）()]", "", str(name or ""))


async def _chat_json(llm, prompt: str, enable_search: bool | None = None) -> dict:
    """调用LLM并解析JSON返回。enable_search显式传入时覆盖实例默认。"""
    try:
        res = await llm.chat([{"role": "system", "content": SYSTEM},
                              {"role": "user", "content": prompt}],
                             expect_json=True, enable_search=enable_search)
        return res if isinstance(res, dict) else {}
    except Exception:
        return {}


# ---------------------------------------------------------------------------
# 工商API版组织图：详情+股东+分支机构三接口直接构建，0次LLM调用。
# 输出契约：roots/seed_key/nodes/tree/weak/
# siblings/has_natural_person_holder/uncertain/depth2_empty）。
# 局限：API无对外投资接口，二级股权层缺失，depth2_empty 恒 True（提示语仍准确）。
# ---------------------------------------------------------------------------

def _ratio(s) -> float | None:
    m = re.search(r"([\d.]+)\s*%?", str(s or ""))
    try:
        return float(m.group(1)) if m else None
    except ValueError:
        return None


def build_org_graph_from_api(detail: dict, shareholders: dict, branches: dict) -> dict | None:
    """从工商API返回构建 org.json。tree 为空（无分支机构且无控股股东）返回 None。"""
    seed = {"name": str(detail.get("companyName") or detail.get("name") or "").strip(),
            "credit_code": detail.get("creditNumber"),
            "legal_person": detail.get("juridicalPerson"),
            "founder": None, "status": detail.get("businessStatus"),
            "company_type": detail.get("companyType"),
            "staff_number": detail.get("staffNumber")}
    if not seed["name"]:
        return None
    code = str(seed["credit_code"] or "").strip().upper()
    if not CREDIT_RE.match(code):
        code = None
        seed["credit_code"] = None
    seed_key = ("c:" + code) if code else "n:" + _norm(seed["name"])
    nodes = {seed_key: {**seed, "id": seed_key}}
    tree, roots = [], []

    # 控股股东（公司型、≥50%）作为母公司根
    for h in (shareholders or {}).get("records") or []:
        name = str(h.get("name") or "").strip()
        r = _ratio(h.get("shareholdingRatio"))
        if (not name or name == "其他股东" or str(h.get("type") or "") == "个人"
                or "持股计划" in name or not r or r < 50):
            continue
        hk = "n:" + _norm(name)
        if hk == seed_key or hk in nodes:
            continue
        nodes[hk] = {"id": hk, "name": name, "credit_code": None,
                     "legal_person": None, "founder": None, "status": None}
        tree.append({"parent": hk, "child": seed_key, "rel": "subsidiary",
                     "confidence": "high", "signal": str(h.get("shareholdingRatio"))[:40]})
        roots.append(hk)

    # 分支机构作为下级（存续优先，节点上限沿用 _MAX_NODES）
    recs = sorted((branches or {}).get("records") or [],
                  key=lambda x: str(x.get("companyStatus")) != "存续")
    for b in recs[:_MAX_NODES]:
        name = str(b.get("name") or "").strip()
        if not name:
            continue
        bk = "b:" + str(b.get("id") or _norm(name))
        if bk in nodes:
            continue
        nodes[bk] = {"id": bk, "name": name, "credit_code": None,
                     "legal_person": b.get("judicalPerson"), "founder": None,
                     "status": b.get("companyStatus"), "is_branch": True}
        tree.append({"parent": seed_key, "child": bk, "rel": "branch",
                     "confidence": "high", "signal": str(b.get("companyStatus") or "")[:40]})

    if not tree:
        return None
    if not roots:
        roots = [seed_key]
    roots.sort(key=lambda k: k != seed_key)
    by_parent = {}
    for e in tree:
        by_parent.setdefault(e["parent"], []).append(e["child"])
    depth = {}
    for r0 in roots:
        stack = [(r0, 0)]
        while stack:
            k, d = stack.pop()
            if depth.get(k, 99) <= d:
                continue
            depth[k] = d
            stack += [(c, d + 1) for c in by_parent.get(k, [])]
    for k in nodes:
        nodes[k]["depth"] = depth.get(k, 99)
    return {"roots": roots, "seed_key": seed_key, "nodes": list(nodes.values()),
            "tree": tree, "weak": [], "siblings": [v for v in by_parent.values() if len(v) > 1],
            "has_natural_person_holder": any(
                str(h.get("type") or "") == "个人" or "持股计划" in str(h.get("name") or "")
                for h in (shareholders or {}).get("records") or []),
            "uncertain": [], "depth2_empty": True, "source": "business_api"}
