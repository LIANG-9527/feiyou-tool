"""build_org_graph_from_api 离线单测：assert 断言，直接 python3 运行。"""
from org_graph import build_org_graph_from_api

DETAIL = {"id": "x1", "companyName": "广州好莱客创意家居股份有限公司",
          "creditNumber": "91440100732952737X", "juridicalPerson": "沈汉标",
          "businessStatus": "存续", "companyType": "股份有限公司(上市,自然人投资或控股)",
          "staffNumber": "1000~4999"}
SH = {"records": [
    {"name": "沈汉标", "type": "个人", "shareholdingRatio": "41.13%"},
    {"name": "某控股集团有限公司", "type": "企业法人", "shareholdingRatio": "51.00%"},
    {"name": "山东省国有资产投资控股有限公司", "type": "投资公司", "shareholdingRatio": "1.51%"},
    {"name": "广州好莱客创意家居股份有限公司-2023年员工持股计划", "type": "员工持股计划", "shareholdingRatio": "3.57%"},
    {"name": "其他股东", "type": "", "shareholdingRatio": "25.10%"},
]}
BR = {"records": [
    {"id": "b1", "name": "某股份有限公司天河分公司", "judicalPerson": "黄立志",
     "companyStatus": "注销", "regitDate": 1645372800},
    {"id": "b2", "name": "某股份有限公司雄安分公司", "judicalPerson": "刘见亮",
     "companyStatus": "存续", "regitDate": 1712764800},
]}


def main():
    org = build_org_graph_from_api(DETAIL, SH, BR)
    assert org, "org 应构建成功"
    by_id = {n["id"]: n for n in org["nodes"]}
    seed = by_id[org["seed_key"]]
    assert seed["legal_person"] == "沈汉标" and seed["credit_code"] == "91440100732952737X"
    # companyType/staffNumber 透传到种子节点（企业性质单标签数据源）
    assert seed["company_type"] == "股份有限公司(上市,自然人投资或控股)"
    assert seed["staff_number"] == "1000~4999"
    # 控股股东成为母公司根，51% >= 50
    holder_edges = [e for e in org["tree"] if e["child"] == org["seed_key"]]
    assert len(holder_edges) == 1 and holder_edges[0]["parent"] in by_id
    assert org["roots"] == [holder_edges[0]["parent"]]
    # 1.51% 参股投资公司与自然人/员工持股/其他股东不进树
    assert not any("山东" in n["name"] for n in org["nodes"])
    assert org["has_natural_person_holder"] is True
    # 分支机构挂在种子下，存续排前
    branch_edges = [e for e in org["tree"] if e["rel"] == "branch"]
    assert [by_id[e["child"]]["name"] for e in branch_edges][0].endswith("雄安分公司")
    assert by_id[branch_edges[0]["child"]]["legal_person"] == "刘见亮"
    # 深度：根0 种子1 分支2
    assert by_id[branch_edges[0]["child"]]["depth"] == 2 and seed["depth"] == 1
    assert org["depth2_empty"] is True and org["source"] == "business_api"

    # 空数据：无分支无控股股东 → None
    assert build_org_graph_from_api(DETAIL, {"records": []}, {"records": []}) is None
    # 缺名称 → None
    assert build_org_graph_from_api({}, SH, BR) is None
    # 坏信用代码当无码处理（key 落 n: 前缀）
    org2 = build_org_graph_from_api({**DETAIL, "creditNumber": "BAD"}, SH, BR)
    assert org2["seed_key"].startswith("n:")
    print("test_org_graph_api: all assertions passed")


if __name__ == "__main__":
    main()
