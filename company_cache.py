"""公司基本信息缓存（SQLite，data/company_info.db）。

语义（与用户约定一致）：
- 每次画像产出后 upsert 非空字段（最新检索优先，字段级合并）；
- 检索落空时读取缓存保底；缓存补充的值由调用方标注（历史缓存 + 日期）；
- 键：信用代码优先，否则归一化公司名；get 时归一化互相包含即视为同一公司。
"""
import json
import logging
import os
import re
import sqlite3
import time

logger = logging.getLogger("company_cache")

from paths import DATA_DIR as _DATA_DIR
DB_PATH = os.path.join(str(_DATA_DIR), "company_info.db")
# LAYER1 字段 + 其他常用字段
_CACHE_FIELDS = (
    # LAYER1 工商基础字段
    "full_name", "credit_code", "legal_person", "founded", "address",
    "listed", "employees", "industry", "industry_position",
    # 其他常用字段
    "revenue", "qualifications", "patents", "founder", "status",
    # 品牌资料：品牌名与别名均允许数组（多品牌；品牌名是别名的其中一个）
    "brands", "aliases", "nature", "domains",
    # 工商接口全量字段（API 给什么存什么，2026-09-16 与用户确认）
    "license_number", "org_code", "regit_address", "province", "city", "district",
    "industry_code", "staff_number", "social_security_num", "social_security_from",
    "reg_capital", "contributed_capital", "annual_turnover", "report_tax_from",
    "report_tax_total", "approve_date", "revoke_date", "business_period",
    "issuing_authority", "address_last_change", "business_scope", "products",
    "company_profile", "history_names", "api_id",
    # 辅助字段
    "reg_capital", "company_type", "business_scope", "products",
)


def derive_audience_type(persona: dict) -> str:
    """客群 B端/C端/B+C端 唯一判定（关键词规则，命中集合合并）。"""
    import re
    blob = " ".join(str(persona.get(k) or "") for k in
                    ("name", "definition", "core_traits", "typical_motivation",
                     "decision_habit", "roles", "sales_strategy"))
    blob = re.sub(r"[，。；、/（）()\-]", " ", blob)
    b = any(w in blob for w in ("企业", "公司", "工厂", "厂家", "经销商", "代理商", "分销", "采购",
                                "招标", "投标", "项目", "工程", "政企", "集团", "门店", "加盟商",
                                "老板", "产品经理", "技术负责人", "工程师", "采购经理", "供应链",
                                "B端", "政企", "开发商", "集成商", "渠道"))
    c = any(w in blob for w in ("业主", "消费者", "家庭", "家长", "个人", "用户", "车主", "住户",
                                "市民", "学生", "宝妈", "C端", "顾客", "玩家", "散户"))
    return "B+C端" if (b and c) else ("B端" if b else ("C端" if c else "未标注"))


def is_org_name(name) -> bool:
    """是否像工商注册全称（以组织形式后缀结尾且≥5字）；品牌简称（"好莱客"）判否。"""
    s = re.sub(r"[)）]$", "", str(name or "").strip())
    return len(s) >= 5 and bool(re.search(
        r"(公司|集团|厂|中心|研究院|研究所|事务所|合伙|银行|合作社|工作室|服务部|分部"
        r"|店|行|坊|馆|堂|部|档|摊|记|餐厅|饭店|超市|市场)$", s))


def derive_nature(info: dict) -> str:
    r"""企业性质唯一判定（互斥优先级命中即停）。结论值=完整档位名（上市企业/…/未标注）。
    规则规格见 docs/specs/SPEC-company-nature-unify.md；一切消费方只调此函数。"""
    import re
    ct = str(info.get("company_type") or "")
    listed = str(info.get("listed") or "")
    full = str(info.get("full_name") or "")
    if "上市" in ct or ("上市" in listed and "未上市" not in listed):
        return "上市企业"
    if "国有" in ct:
        return "国有企业"
    if "港澳台" in ct:
        return "港澳台投资企业"
    if "外商" in ct or "外资" in ct:
        return "外资企业"
    if "集团" in full:
        return "集团企业"
    # 员工规模：工商原始 staff_number（"0~49" 无单位）优先，长文本兜底；
    # 先剔 "\d{4}年" 式年份再取数，防"2025年年报"的年份冒充人数；区间取下界
    emp = str(info.get("staff_number") or info.get("employees") or "")
    emp = re.sub(r"\d{4}\s*年", "", emp)
    m = (re.search(r"(\d[\d,]*)\s*[~～\-–]\s*\d[\d,]*", emp)
         or re.search(r"(\d[\d,]*)\s*人", emp))
    if not m:
        return "未标注"
    n = int(m.group(1).replace(",", ""))
    return ("大型企业" if n >= 1000 else "中型企业" if n >= 300
            else "小型企业" if n >= 20 else "微型企业")


def validate_layer1_fields(fields: dict) -> tuple[dict, list[str]]:
    """校验 LAYER1 字段的有效性，返回 (有效字段, 错误列表)。

    校验规则：
    - credit_code: 18位，由数字和大写字母组成
    - founded: 年月格式，如 "2022-04" 或 "2022年04月"
    - full_name: 非空
    - legal_person: 非空
    - address: 非空
    - listed: 非空
    - employees: 非空，包含数字或"人"
    - industry: 非空
    - industry_position: 非空（可选，不强制）
    """
    valid = {}
    errors = []

    # full_name: 非空
    v = fields.get("full_name")
    if v and str(v).strip():
        valid["full_name"] = str(v).strip()
    else:
        errors.append("full_name 为空")

    # credit_code: 18位，由数字和大写字母组成
    v = fields.get("credit_code")
    if v:
        code = str(v).strip().upper()
        if len(code) == 18 and all(c.isalnum() for c in code):
            valid["credit_code"] = code
        else:
            errors.append(f"credit_code 格式无效: {v}")

    # legal_person: 非空
    v = fields.get("legal_person")
    if v and str(v).strip():
        valid["legal_person"] = str(v).strip()
    else:
        errors.append("legal_person 为空")

    # founded: 年月格式
    v = fields.get("founded")
    if v:
        founded = str(v).strip()
        # 支持 "2022-04"、"2022年04月"、"2022.04" 等格式
        import re
        m = re.search(r"(\d{4})[-年./](\d{1,2})", founded)
        if m:
            valid["founded"] = f"{m.group(1)}-{m.group(2).zfill(2)}"
        elif len(founded) >= 7 and founded[:4].isdigit():
            valid["founded"] = founded[:7]
        else:
            errors.append(f"founded 格式无效: {v}")

    # address: 非空
    v = fields.get("address")
    if v and str(v).strip():
        valid["address"] = str(v).strip()
    else:
        errors.append("address 为空")

    # listed: 非空
    v = fields.get("listed")
    if v and str(v).strip():
        valid["listed"] = str(v).strip()
    else:
        errors.append("listed 为空")

    # employees: 非空，包含数字或"人"
    v = fields.get("employees")
    if v and str(v).strip():
        emp = str(v).strip()
        if any(c.isdigit() for c in emp) or "人" in emp:
            valid["employees"] = emp
        else:
            errors.append(f"employees 格式无效: {v}")
    else:
        errors.append("employees 为空")

    # industry: 非空
    v = fields.get("industry")
    if v and str(v).strip():
        valid["industry"] = str(v).strip()
    else:
        errors.append("industry 为空")

    # industry_position: 可选，非空就保存
    v = fields.get("industry_position")
    if v and str(v).strip():
        valid["industry_position"] = str(v).strip()

    return valid, errors


def _conn():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    c = sqlite3.connect(DB_PATH)
    c.execute("CREATE TABLE IF NOT EXISTS company(id TEXT PRIMARY KEY, data TEXT, updated TEXT)")
    c.execute("CREATE TABLE IF NOT EXISTS mining(id TEXT PRIMARY KEY, data TEXT, updated TEXT)")
    c.execute("CREATE TABLE IF NOT EXISTS audit_snapshot(id TEXT PRIMARY KEY, data TEXT, updated TEXT)")
    # 快照行企业归属列（存量库补列；绑定成功即认领，未绑定为NULL待认领）
    if "company_id" not in [r[1] for r in c.execute("PRAGMA table_info(audit_snapshot)")]:
        c.execute("ALTER TABLE audit_snapshot ADD COLUMN company_id TEXT")
    # 企业资料页需要"首次创建时间"：补列；存量行用 updated 回填（真实更早时间不可追溯）
    cols = [r[1] for r in c.execute("PRAGMA table_info(company)")]
    if "created" not in cols:
        c.execute("ALTER TABLE company ADD COLUMN created TEXT")
        c.execute("UPDATE company SET created=updated WHERE created IS NULL")
    # 信息触点：首次创建该档案的入口 + 最后一次写入的入口（存量行无法追溯，留空显"-"）
    for oc in ("origin_first", "origin_last"):
        if oc not in cols:
            c.execute(f"ALTER TABLE company ADD COLUMN {oc} TEXT")
    return c


def ensure_company(name: str, credit_code: str | None = None, origin: str = "") -> str | None:
    """确保公司行存在（无资料也可），返回统一身份 id。origin=发起页面/触点。
    身份闸（与 upsert_info 同规则）：无信用代码且名称不像注册组织名（品牌简称）时，
    只允许归一到已有档案（全称/品牌/别名匹配），绝不新建游离假身份行。"""
    code = str(credit_code or "").strip().upper() or None
    rid = f"c:{code}" if code else ("n:" + norm_name(name) if norm_name(name) else None)
    if not rid:
        return None
    try:
        with _conn() as c:
            cur = c.cursor()
            matched = _match_id(name, cur)
            if matched:
                if code:
                    row = cur.execute("SELECT data FROM company WHERE id=?", (matched,)).fetchone()
                    data = json.loads(row[0]) if row else {}
                    if not data.get("credit_code"):
                        data["credit_code"] = code
                        cur.execute("UPDATE company SET data=? WHERE id=?",
                                    (json.dumps(data, ensure_ascii=False), matched))
                return matched
            if not code and not is_org_name(name):
                logger.warning(f"ensure_company 拒建身份行：「{name}」非注册组织名")
                return None
            cur.execute("INSERT OR REPLACE INTO company(id, data, updated, created, origin_first, origin_last) VALUES(?,?,?,?,?,?)",
                        (rid, json.dumps({"full_name": name, "credit_code": code}, ensure_ascii=False),
                         time.strftime("%Y-%m-%d %H:%M"), time.strftime("%Y-%m-%d %H:%M"), origin, origin))
            return rid
    except Exception:
        return None


_LIST_KEYS = ("products", "personas", "journey", "journey_stages", "topics",
              "clusters", "verifications", "sample_queries")


def prefer_brand(name: str, brand: str, origin: str = "", source: str = "user_confirm"):
    """钉主品牌（brand_primary）并记录获取方式（brand_source）：
    user_confirm=人工弹窗确认；web_multichannel=七渠道联网核查。识别层只信任这两种，
    其他来源（离线裁决/历史存量无来源）下次识别会重新触发联网核查更新。"""
    brand = str(brand or "").strip()
    if not name or not brand:
        return
    try:
        add_brands_alias(name, brand, origin=origin)
        with _conn() as c:
            cur = c.cursor()
            rid = _match_id(name, cur)
            if not rid:
                return
            row = cur.execute("SELECT data FROM company WHERE id=?", (rid,)).fetchone()
            d = json.loads(row[0]) if row else {}
            if d.get("brand_primary") == brand and d.get("brand_source") == source:
                return
            d["brand_primary"] = brand
            d["brand_source"] = source
            d["nature"] = derive_nature(d)
            cur.execute("UPDATE company SET data=?, updated=? WHERE id=?",
                        (json.dumps(d, ensure_ascii=False), time.strftime("%Y-%m-%d %H:%M"), rid))
    except Exception:
        pass  # 缓存写失败不影响主流程


def upsert_mining(brand: str, payload: dict, origin: str = "", content_brand: str = ""):
    """问句挖掘板块产出写入 mining 表（键=公司身份 id）。
    每条内容打品牌标（content_brand=用户输入的品牌名，缺省用全称）；
    同品牌旧条目整体替换、跨品牌条目保留追加（一司多品牌可分别过滤）。"""
    rid = ensure_company(brand, origin=origin)
    if not rid:
        return
    cbrand = str(content_brand or brand or "")
    try:
        with _conn() as c:
            cur = c.cursor()
            row = cur.execute("SELECT data FROM mining WHERE id=?", (rid,)).fetchone()
            old = json.loads(row[0]) if row else {}
            merged = dict(old)
            for k in _LIST_KEYS:
                items = payload.get(k)
                if items is None:
                    continue
                kept = [e for e in (old.get(k) or [])
                        if isinstance(e, dict) and e.get("brand") and e.get("brand") != cbrand]
                tagged = []
                for e in items:
                    t = dict(e) if isinstance(e, dict) else {"text": str(e)}
                    t["brand"] = cbrand
                    tagged.append(t)
                merged[k] = kept + tagged
            merged["brand"] = brand
            merged["query_count"] = payload.get("query_count", merged.get("query_count"))
            c.execute("INSERT OR REPLACE INTO mining(id, data, updated) VALUES(?,?,?)",
                      (rid, json.dumps(merged, ensure_ascii=False), time.strftime("%Y-%m-%d %H:%M")))
    except Exception:
        pass


def get_mining(name: str) -> tuple[dict, str] | tuple[None, str]:
    try:
        with _conn() as c:
            cur = c.cursor()
            rid = _match_id(name, cur)
            if not rid:
                return None, ""
            row = cur.execute("SELECT data, updated FROM mining WHERE id=?", (rid,)).fetchone()
            return (json.loads(row[0]), row[1]) if row else (None, "")
    except Exception:
        return None, ""


def get_mining_by_id(rid: str) -> tuple[dict, str] | tuple[None, str]:
    """mining 表主键=企业身份 id，详情页直接按 id 取（免名称归一二次匹配）。"""
    try:
        with _conn() as c:
            row = c.execute("SELECT data, updated FROM mining WHERE id=?", (rid,)).fetchone()
        return (json.loads(row[0]), row[1]) if row else (None, "")
    except Exception:
        return None, ""


def upsert_audit_snapshot(domain: str, ts: str, payload: dict):
    """官网检测快照：每次检测一行（id=domain_ts），存结论不存明细。
    企业归属：写入时按域名反查档案带 company_id；未绑定的先挂空（待认领），
    绑定成功由 claim_audit_snapshots 补认——快照永远归企业身份管。"""
    try:
        cid = find_company_id_by_domain(domain)
        with _conn() as c:
            c.execute("INSERT OR REPLACE INTO audit_snapshot(id, data, updated, company_id) VALUES(?,?,?,?)",
                      (f"{domain}_{ts}", json.dumps({"domain": domain, **payload}, ensure_ascii=False),
                       time.strftime("%Y-%m-%d %H:%M"), cid))
    except Exception:
        pass


def _norm_domain(d) -> str:
    d = str(d or "").strip().lower()
    return d[4:] if d.startswith("www.") else d


def find_company_id_by_domain(domain: str) -> str | None:
    """域名反查企业档案身份 id（档案 domains 字段登记过即命中）。"""
    nd = _norm_domain(domain)
    if not nd:
        return None
    try:
        with _conn() as c:
            for rid, data in c.execute("SELECT id, data FROM company").fetchall():
                doms = json.loads(data).get("domains") or []
                if any(_norm_domain(x) == nd for x in doms):
                    return rid
    except Exception:
        return None
    return None


def claim_audit_snapshots(domain: str, company_id: str):
    """绑定成功时认领：该域名名下所有无主快照行补上企业身份 id。"""
    nd = _norm_domain(domain)
    if not nd or not company_id:
        return
    try:
        with _conn() as c:
            for rid, data in c.execute("SELECT id, data FROM audit_snapshot WHERE company_id IS NULL").fetchall():
                if _norm_domain(json.loads(data).get("domain")) == nd:
                    c.execute("UPDATE audit_snapshot SET company_id=? WHERE id=?", (company_id, rid))
            c.commit()
    except Exception:
        pass


def add_domains(name: str, domain: str, origin: str = ""):
    """企业档案并入官网域名（去重、增量），走 upsert_info 同口径。"""
    domain = str(domain or "").strip().lower()
    while domain.startswith("www."):
        domain = domain[4:]
    if not name or not domain:
        return
    try:
        old, _ = get_info(name)
        cur = [d for d in ((old or {}).get("domains") or []) if d]
        if domain not in cur:
            upsert_info(name, {"domains": cur + [domain]}, origin=origin)
    except Exception:
        pass


def add_brands_alias(name: str, brand: str, origin: str = ""):
    """把用户输入的短名（品牌）增量并入正牌行的 品牌名+别名 两清单（去重）。"""
    brand = str(brand or "").strip()
    if not name or not brand or brand == name:
        return
    try:
        old, _ = get_info(name)
        old = old or {}
        brands = [b for b in (old.get("brands") or []) if b]
        aliases = [a for a in (old.get("aliases") or []) if a]
        ch = False
        if brand not in brands:
            brands.append(brand); ch = True
        if brand not in aliases:
            aliases.append(brand); ch = True
        if ch:
            upsert_info(name, {"brands": brands, "aliases": aliases}, origin=origin)
    except Exception:
        pass


def list_audit_snapshots(company_id: str) -> list[dict]:
    """按企业身份 id 查检测快照行（企业资料页「官网检测」页签数据源），时间倒序。"""
    if not company_id:
        return []
    out = []
    try:
        with _conn() as c:
            for rid, data, updated in c.execute(
                    "SELECT id, data, updated FROM audit_snapshot WHERE company_id=? ORDER BY updated DESC",
                    (company_id,)):
                d = json.loads(data)
                summ = d.get("summary") or {}
                out.append({"sid": rid, "domain": d.get("domain"), "date": updated,
                            "score": d.get("score"), "grade": d.get("grade"),
                            "fail": summ.get("fail"), "warn": summ.get("warn"),
                            "pass": summ.get("pass"), "total": summ.get("total"),
                            "categories": d.get("categories") or [],
                            "core_counts": d.get("core_counts") or {},
                            "bonus_counts": d.get("bonus_counts") or {},
                            "fingerprint": d.get("fingerprint") or {},
                            "backend": d.get("backend") or {},
                            "issues": d.get("issues") or [],
                            "report_file": d.get("report_file") or ""})
    except Exception:
        return []
    return out


def norm_name(name: str) -> str:
    return re.sub(r"[\s（）()：:，,。·]", "", str(name or ""))


def _match_id(name: str, cur: sqlite3.Cursor) -> str | None:
    """身份匹配：候选名与档案的 全称/品牌名/别名 任一归一相等或互相包含(≥4字)即同一主体。
    品牌/别名参与匹配（源头修复）：简称挖掘任务归入正牌行，不再另起游离假行。"""
    nm = norm_name(name)
    if not nm:
        return None
    for rid, data in cur.execute("SELECT id, data FROM company").fetchall():
        d = json.loads(data)
        names = [d.get("full_name") or "", *(str(x) for x in (d.get("brands") or [])),
                 *(str(x) for x in (d.get("aliases") or []))]
        for cand in names:
            on = norm_name(cand)
            if not on:
                continue
            if nm == on or (min(len(nm), len(on)) >= 4 and (nm in on or on in nm)):
                return rid
    return None


def get_info(name: str) -> tuple[dict, str] | tuple[None, str]:
    """按公司名取缓存（归一互相包含即命中）。返回 (字段dict, 更新时间) 或 (None, '')。"""
    try:
        with _conn() as c:
            cur = c.cursor()
            rid = _match_id(name, cur)
            if not rid:
                return None, ""
            row = cur.execute("SELECT data, updated FROM company WHERE id=?", (rid,)).fetchone()
            return json.loads(row[0]), row[1]
    except Exception:
        return None, ""


def _has_value(v):
    if isinstance(v, list):
        return any(x not in (None, "", "None", "null", "待补") for x in v)
    return v not in (None, "", [], "None", "null", "待补")


def upsert_info(name: str, fields: dict, credit_code: str | None = None, origin: str = "",
                created: str | None = None):
    """字段级合并写入：非空字段覆盖，空字段保留旧值。origin=发起写入的页面/触点；
    created 仅供存量回灌指定首次创建时间，正常路径不传。"""
    if not isinstance(fields, dict):
        return
    data = {k: v for k, v in fields.items() if k in _CACHE_FIELDS and _has_value(v)}
    if not data:
        return
    try:
        with _conn() as c:
            cur = c.cursor()
            code = str(credit_code or "").strip().upper() or None
            rid = _match_id(name, cur)
            if rid is None:
                if not code and not is_org_name(name):
                    logger.warning(f"upsert_info 拒建身份行：「{name}」不是注册组织名（品牌简称请走弹窗确认全称后入库）")
                    return
                rid = f"c:{code}" if code else "n:" + norm_name(name)
            row = cur.execute("SELECT data, created, origin_first, origin_last FROM company WHERE id=?", (rid,)).fetchone()
            merged = json.loads(row[0]) if row else {}
            for k, v in data.items():
                if _has_value(v):
                    merged[k] = v
            if credit_code:
                merged["credit_code"] = credit_code
            merged.setdefault("full_name", name)
            merged["nature"] = derive_nature(merged)  # 任何档案写入都同步刷新性质结论
            now = time.strftime("%Y-%m-%d %H:%M")
            # 创建时间与首次来源只写一次：新行=本次，已有行保留原值；
            # 存量旧行 origin_first 为空不回填（那次触点并非创建者，填了反而误导）
            created = (row[1] if row and row[1] else None) or created or now
            origin_first = (row[2] if row else None) or (origin if row is None else "")
            # 触点未知的写入不清掉已记录的最后触点
            origin_last = origin or ((row[3] if row else "") or "")
            cur.execute("INSERT OR REPLACE INTO company(id, data, updated, created, origin_first, origin_last) VALUES(?,?,?,?,?,?)",
                        (rid, json.dumps(merged, ensure_ascii=False), now, created, origin_first, origin_last))
    except Exception:
        pass  # 缓存写失败不影响主流程


def known_facts_text(brand: str) -> str:
    """组装库中已知事实摘要（供生成提示词校准注入）。库空返回空串。"""
    def _txt(x):
        return str(x.get("text") or x.get("query") or x.get("stage_name") or x.get("name") or "") if isinstance(x, dict) else str(x)
    parts = []
    info, ts = get_info(brand)
    if info:
        line = "；".join(f"{k}={v}" for k, v in info.items()
                        if v not in (None, "", [], "None") and k != "credit_code")
        parts.append(f"【基本信息（历史沉淀{ts[:10]}）】{line}")
    mining, mts = get_mining(brand)
    if mining:
        bits = []
        if mining.get("products"):
            bits.append("产品/服务=" + json.dumps(mining["products"], ensure_ascii=False)[:300])
        if mining.get("personas"):
            bits.append("客群画像=" + json.dumps(mining["personas"], ensure_ascii=False)[:300])
        if mining.get("journey_stages"):
            bits.append("决策旅程阶段=" + "、".join(filter(None, map(_txt, mining["journey_stages"]))))
        if mining.get("sample_queries"):
            bits.append("高频问句样例=" + "；".join(filter(None, map(_txt, mining["sample_queries"][:8]))))
        if bits:
            parts.append(f"【问句挖掘沉淀（{mts[:10]}）】" + "；".join(bits))
    return "\n".join(parts)


if __name__ == "__main__":
    # 自检走临时库（生产库只读不写，防测试数据入档）
    import tempfile
    DB_PATH = os.path.join(tempfile.mkdtemp(), "t.db")
    # 源头身份断言：组织名判定 + 非组织名拒建身份行
    assert is_org_name("广州好莱客创意家居股份有限公司") and not is_org_name("好莱客")
    upsert_info("好莱客", {"legal_person": "某"})  # 无信用代码且非组织名 → 应被拒
    assert get_info("好莱客")[0] is None, "非组织名身份行不应建立"
    # 写入→命中→字段级合并
    upsert_info("测试甲科技有限公司", {"founded": "2000年", "revenue": "1亿"})
    upsert_info("测试甲科技有限公司", {"employees": "100人"})  # 二次只更新一个字段
    got, ts = get_info("测试甲科技")  # 品牌/别名参与归一匹配（源头修复）：简称命中全称行
    assert got and got["founded"] == "2000年" and got["employees"] == "100人", got
    assert ts
    # 企业性质五样例（规格验收锚点）
    assert derive_nature({"company_type": "股份有限公司(上市,自然人投资或控股)", "listed": "上市",
                          "full_name": "广州好莱客创意家居股份有限公司", "staff_number": "1000~4999"}) == "上市企业"
    assert derive_nature({"company_type": "有限责任公司(港澳台法人独资)", "staff_number": "1000~4999",
                          "full_name": "深圳依时货拉拉科技有限公司"}) == "港澳台投资企业"
    assert derive_nature({"company_type": "其他有限责任公司", "listed": "未上市", "staff_number": "200~499",
                          "full_name": "浙江锦联国际物流有限公司"}) == "小型企业"
    assert derive_nature({"company_type": "其他有限责任公司", "staff_number": "0~49",
                          "full_name": "山西宜发同诚农业发展有限公司"}) == "微型企业"
    assert derive_nature({"employees": "2025年年报社保21人；工商口径0-49人", "full_name": "江西启帆教育科技有限公司"}) == "微型企业"
    assert derive_nature({}) == "未标注"
    print("company_cache OK", ts)


def list_companies() -> list[dict]:
    """企业资料列表：名称、首次创建、最后更新、首次信息来源、最后更新触点（按更新时间倒序）。"""
    with _conn() as c:
        rows = c.execute("SELECT id, data, updated, created, origin_first, origin_last FROM company ORDER BY updated DESC").fetchall()
    out = []
    for rid, data, updated, created, ofirst, olast in rows:
        d = json.loads(data)
        out.append({"id": rid,
                    "name": d.get("full_name") or (rid[2:] if rid.startswith("n:") else rid),
                    "created": created or updated, "updated": updated,
                    "origin_first": ofirst or "", "origin_last": olast or ""})
    return out


def get_company_by_id(rid: str) -> tuple[dict, str, str] | tuple[None, str, str]:
    """按 id 取档案全量：返回 (字段dict, 创建时间, 更新时间)。"""
    with _conn() as c:
        row = c.execute("SELECT data, updated, created FROM company WHERE id=?", (rid,)).fetchone()
    return (json.loads(row[0]), row[2] or row[1], row[1]) if row else (None, "", "")


def delete_company(rid: str) -> bool:
    """删除企业档案行（连带其 mining 板块）。"""
    with _conn() as c:
        n = c.execute("DELETE FROM company WHERE id=?", (rid,)).rowcount
        c.execute("DELETE FROM mining WHERE id=?", (rid,))
    return bool(n)


def delete_audit_snapshot(sid: str) -> bool:
    """删除单条检测快照行（企业资料页检测记录卡片的删除按钮）。"""
    try:
        with _conn() as c:
            return bool(c.execute("DELETE FROM audit_snapshot WHERE id=?", (sid,)).rowcount)
    except Exception:
        return False
