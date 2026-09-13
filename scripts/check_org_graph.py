"""组织架构图构建逻辑自检：归一/归类/渲染融合纯逻辑断言，不调 LLM。"""
import sys, json
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))
from org_graph import OrgGraphBuilder, _entity_key, CREDIT_RE

assert CREDIT_RE.match("91440300087035600P") and not CREDIT_RE.match("123"), "18位校验"

b = OrgGraphBuilder(llm=None)
seed = {"name": "广州好莱客创意家居股份有限公司", "credit_code": "91440101MA01ABCD00",
        "aliases": [], "legal_person": "沈某", "founder": None, "status": "存续",
        "match_confidence": "high", "evidence": ""}
seed_key = _entity_key(seed)
layers = {
    seed_key: {"self": seed,
               "corporate_holders": [],  # 无母公司
               "investments": [
                   {"name": "广州好莱客智能家居有限公司", "credit_code": "91440101MA01TEST01",
                    "is_holding": True, "signal": "100%", "evidence": ""},
                   {"name": "广东参股科技公司", "credit_code": None,
                    "is_holding": False, "signal": "参股20%", "evidence": ""},
                   {"name": "上海模糊合伙企业", "credit_code": None,
                    "is_holding": None, "signal": "关联", "evidence": ""}],
               "has_natural_person_holder": True, "uncertain": [{"claim": "疑似湖北基地", "reason": "单源"}]},
    "c:91440101MA01TEST01": {"self": {"name": "广州好莱客智能家居有限公司", "credit_code": "91440101MA01TEST01"},
                             "corporate_holders": [{"name": seed["name"], "credit_code": seed["credit_code"],
                                                    "is_holding": True, "signal": "100%", "evidence": ""}],
                             "investments": [], "has_natural_person_holder": False, "uncertain": []},
}
# 变体层合并：不同码的同名公司层应并回种子
layers["n:广州好莱客创意家居股份有限公司"] = {
    "self": {"name": "广州好莱客创意家居股份有限公司", "credit_code": "91440101739758688K"},
    "corporate_holders": [], "investments": [], "has_natural_person_holder": False, "uncertain": []}
org = b._build(seed, seed_key, layers)
assert "n:广州好莱客创意家居股份有限公司" not in [n["id"] for n in org["nodes"]], "变体层已合并"
assert org["roots"] == [seed_key], org["roots"]
assert len(org["tree"]) == 3, org["tree"]      # 2控股+1低置信
assert len(org["weak"]) == 1, org["weak"]      # 参股进 weak
assert org["has_natural_person_holder"] is True
nodes_by_id = {n["id"]: n for n in org["nodes"]}
assert nodes_by_id[seed_key]["depth"] == 0
assert nodes_by_id["c:91440101MA01TEST01"]["depth"] == 1
assert any(u.get("claim") for u in org["uncertain"])

# 渲染融合：org 优先于旧字段，法人/创始人格 + 参股卡出现
from main import _mr_brief_html
brief = {"info": {"full_name": seed["name"]}, "layout": {"branches": ["北京"]},
         "org_tree": {"name": "旧部门架构", "children": [{"name": "旧营销中心"}]},
         "gaps": ["旧待补项"], "timeline": []}
h_old = _mr_brief_html(brief, "好莱客")
h_new = _mr_brief_html(brief, "好莱客", org)
assert "法定代表人" in h_new and "创始人/实控人" in h_new and "沈某" in h_new, "新格"
assert "旧营销中心" in h_old and "旧营销中心" not in h_new, "org 存在时不走旧组织数据"
assert "参股企业" in h_new and "广东参股科技公司" in h_new, "参股卡"
assert "组织检索存疑" in h_new, "uncertain 并入待补"
assert "智能家居" in h_new and "待核实" in h_new, "树含低置信标注"
assert "北京" not in h_new, "org 存在时城市冒充分支不再渲染"
print("OK")
