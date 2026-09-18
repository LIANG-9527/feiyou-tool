"""audit_company_bind 自检：提取走模块内 __main__ 断言；此处补核验与绑定决策的桩测试。
运行：python3.12 test_audit_company_bind.py（不发真实请求）"""
import asyncio
import json
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from audit_company_bind import verify_cn, bind_report, extract_companies, norm_name  # noqa: E402


class _FakeAPI:
    """桩：只有注册名精准命中，其余一律空。"""

    def __init__(self, registered: dict):
        self.registered = registered  # 归一名 -> 工商返回

    async def get_company_detail_by_name(self, name, use_cache=True):
        return self.registered.get(norm_name(name), {})


def _fake_upsert(*a, **k):
    pass


def test_verify_cn():
    api = _FakeAPI({norm_name("深圳飞柚数智营销技术有限公司"): {"companyName": "深圳飞柚数智营销技术有限公司"}})
    got, err = asyncio.run(verify_cn(["深圳飞柚数智营销技术有限公司", "深圳市飞柚数智营销技术有限公司",
                                      "广州飞柚科技有限公司"], api))
    assert err == "" and [g["name"] for g in got] == ["深圳飞柚数智营销技术有限公司"], (got, err)  # 带市变体/错绑对象均出局


def test_bind_unique():
    api = _FakeAPI({norm_name("深圳飞柚数智营销技术有限公司"): {
        "companyName": "深圳飞柚数智营销技术有限公司", "staffNumber": "88", "creditNumber": "91440300MAxxxx"}})
    with tempfile.TemporaryDirectory() as td:
        jf = Path(td) / "a.cn_20260916_000000.json"
        jf.write_text("{}", "utf-8")
        snap = Path(td) / "snapshots" / jf.stem
        snap.mkdir(parents=True)
        (snap / "pages.json").write_text(json.dumps(
            [{"body_text": "© 2026 深圳飞柚数智营销技术有限公司 粤ICP备xxx号"}], ensure_ascii=False), "utf-8")
        r = asyncio.run(bind_report(jf, api, _fake_upsert))
        assert r["action"] == "bound" and r["company"] == "深圳飞柚数智营销技术有限公司", r
        d = json.loads(jf.read_text("utf-8"))
        # 飞柚已在本机档案库（历史绑定沉淀），第0级本地命中即为正确路径；库不在时走工商核验
        assert d["company_full_name"] == "深圳飞柚数智营销技术有限公司" and d["company_bind_via"] in ("local_archive", "site_full_name")


def test_bind_multi_to_candidates():
    # 用虚构名：确保不命中本地档案库（真实库中恰有的主体会走第0级直绑，属另一用例）
    A, B = "上海测甲网络科技股份有限公司", "上海测乙信息科技股份有限公司"
    api = _FakeAPI({norm_name(A): {"companyName": A}, norm_name(B): {"companyName": B}})
    with tempfile.TemporaryDirectory() as td:
        jf = Path(td) / "b.cn_20260916_000000.json"
        jf.write_text("{}", "utf-8")
        snap = Path(td) / "snapshots" / jf.stem
        snap.mkdir(parents=True)
        (snap / "pages.json").write_text(json.dumps(
            [{"body_text": A + " 与 " + B}], ensure_ascii=False), "utf-8")
        r = asyncio.run(bind_report(jf, api, _fake_upsert))
        assert r["action"] == "candidates" and len(r["candidates"]) == 2, r  # 多主体不自动绑
        assert "company_full_name" not in json.loads(jf.read_text("utf-8"))


def test_bind_english_manual():
    r = extract_companies(["Who we are\nShenzhen ZSTEK Technology Co., Ltd. is a national high-tech enterprise."])
    assert r["en"] == ["Shenzhen ZSTEK Technology Co., Ltd"] and r["cn"] == [], r  # 英文站→人工候选


def test_history_name_bridge():
    """页脚旧注册名 → 档案现名（曾用名桥接，第0级+工商级）"""
    # 第0级：库里档案的 history_names 命中旧名
    import json as _j
    with tempfile.TemporaryDirectory() as td:
        db = Path(td) / "t.db"
        import company_cache as cc
        old_path, cc.DB_PATH = cc.DB_PATH, str(db)
        try:
            cc.upsert_info("兮元睿视科技(深圳)有限公司",
                           {"history_names": ["深圳中安视达科技有限公司"]})
            from audit_company_bind import match_local_archive
            assert match_local_archive(["深圳中安视达科技有限公司"], "") == ["兮元睿视科技(深圳)有限公司"]
        finally:
            cc.DB_PATH = old_path
    # 工商级：曾用名比对用字符集合等价（"（深圳）"↔"深圳市"括号形态差）
    class _A:
        async def get_company_detail_by_name(self, name, use_cache=True):
            from audit_company_bind import norm_name
            if norm_name(name) == norm_name("兮元睿视科技(深圳)有限公司"):
                return {"companyName": "兮元睿视科技(深圳)有限公司",
                        "historyNames": ["深圳中安视达科技有限公司"]}
            return {}
        async def search_company(self, kw, page_size=20, **a):
            return {"records": [{"companyName": "兮元睿视科技(深圳)有限公司"}]}
    got, err = asyncio.run(verify_cn(["中安视达科技（深圳）有限公司"], _A()))
    assert err == "" and [g["name"] for g in got] == ["兮元睿视科技(深圳)有限公司"], got
    # 字序重排不再是同一主体（旧版字符多重集会把 海中/中海 判等）
    got2, _ = asyncio.run(verify_cn(["海中科技有限公司"], _A()))
    assert got2 == [], got2


if __name__ == "__main__":
    test_verify_cn()
    test_bind_unique()
    test_bind_multi_to_candidates()
    test_bind_english_manual()
    # 模块内提取断言（版权/备案/噪声/归一）一并跑
    assert subprocess.run([sys.executable, str(Path(__file__).parent / "audit_company_bind.py")]).returncode == 0
    print("test_audit_company_bind OK")
