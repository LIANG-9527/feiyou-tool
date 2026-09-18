"""官网诊断 → 工商企业精确绑定（规格：docs/specs/SPEC-audit-company-bind.md）。

流程：站内公司全称提取（中文全称 + 英文公司名）→ 工商 API 全称精准核验 →
唯一核验通过才绑定；英文名/多候选/零命中一律落 company_candidates 供人工选定，
绝不自动猜绑。供 main.py 诊断后置钩子与 scripts/backfill_audit_company.py 共用。
"""
from __future__ import annotations

import json
import re
import unicodedata
from pathlib import Path

# 行政区划词典（民政部区划数据，省市两级词条）：截齐与曾用名剥离的唯一依据。
# 名单缺失=禁止任何截齐（宁可漏绑不误切）。
try:
    _AD = set(json.loads((Path(__file__).parent / "adcode_cn.json").read_text(encoding="utf-8")))
except Exception:
    _AD = set()
# 地名基表（去尾缀"市/省"）：曾用名比对时剥离行政区名用
_AD_BASES = sorted({re.sub(r"[市省]$", "", x) for x in _AD if len(x) >= 3}, key=len, reverse=True)
# 分公司分支词典："X市/市名 分公司"并入全称（…有限公司上海分公司）。
# 只有真实地名+分公司才并名；"版权所有"等普通文用负向前瞻排除，防贪婪吞掉。
_BRANCH_NAMES = sorted({re.sub(r"市$", "", x) for x in _AD if x.endswith("市")} | {x for x in _AD if x.endswith("市")}, key=len, reverse=True)
_BRANCH_RE = re.compile("|".join(re.escape(n) for n in _BRANCH_NAMES) + r"分公司") if _BRANCH_NAMES else None

_ZW_RE = re.compile(r"[​-‏⁠﻿]")
_CJK_GAP_RE = re.compile(r"(?<=[\u4e00-\u9fa5])[ \t\r\n\u3000]+(?=[\u4e00-\u9fa5])"
                         r"|(?<=[A-Za-z])[ \t\r\n\u3000]+(?=[\u4e00-\u9fa5])")


def scrub_text(text: str) -> str:
    """匹配前净化：NFKC 归一（全角→半角，３Ｍ→3M）、删零宽字符、删中文之间的空白。
    中文登记名不含空格/零宽，删合字空白不会伤害任何合法全称；页脚断字残名（"…博观 城市…"）
    整类在此消灭。"""
    t = unicodedata.normalize("NFKC", str(text or ""))
    t = _ZW_RE.sub("", t)
    return _CJK_GAP_RE.sub("", t)


# 中文公司全称：…有限公司/有限责任公司/股份有限公司。前缀允许中文/字母/数字/括号，
# 提取后清洗头部粘连的 ASCII 杂质（如 "GEO\n深圳…" 抽出的 "n深圳…"）。
# 尾部分支段（上海分公司）只有词典地名才并入：…有限公司上海分公司 是独立登记主体，
# 截成母公司=绑错；"版权所有"等文由负向前瞻排除（防贪婪回溯把版权文吞进名称）。
_CN_TAIL = (r"(?:(?:" + r"|".join(re.escape(n) for n in _BRANCH_NAMES) + r")分公司)?" if _BRANCH_NAMES else r"(?:[一-龥]{1,6}分公司)?")
_CN_NAME_RE = re.compile(
    r"[\u4e00-\u9fa5A-Za-z0-9（）()]{4,38}?(?:有限责任公司|股份有限公司|有限公司)" + _CN_TAIL +
    r"(?![\u4e00-\u9fa5]{1,6}?(?:分公司|研究院|研究所)|[\u4e00-\u9fa5]{0,2}(?:集团))")
# 英文公司名（国内公司的英文官网）：仅认 ©/(c)/Copyright 版权行锚点后的
# … Co., Ltd. / Limited / Inc. 等句式（后缀区分大小写，防句尾 inc/company 误命中）
_EN_NAME_RE = re.compile(
    r"(?:©|\(c\)|Copyright\b\s*©?)[\s:]*"
    r"((?:\d{4}[\s,–-]*)?[A-Z][A-Za-z0-9&.,'’\- ]{3,60}?"
    r"(?:Co\.,?\s*Ltd\.?|Limited|LLC|Inc\.?|Corp(?:oration)?\.?|Company|Group))")
# 无版权锚点的兜底：全大写词组 + 标准公司后缀（每个词首字母大写，句子里混小写词的
# 散文自动出局），如 "Who we are: Shenzhen ZSTEK Technology Co., Ltd. is a …"
_EN_NAME_LOOSE_RE = re.compile(
    r"\b([A-Z][A-Za-z0-9&'’\-]*(?:\s+[A-Z][A-Za-z0-9&'’\-]*){1,7}"
    r"\s+(?:Co\.,?\s*Ltd\.?|Ltd\.?|Limited|LLC|Inc\.?|Corp\.?|Corporation))\b")
# 明确不是站点主体的模板话术（"由XX技术支持/承建"是第三方，不是站主）
_NOISE_CTX = ("技术支持", "技术维护", "承建", "研发")


def norm_name(name: str) -> str:
    """比对归一：NFKC（全角→半角、全角括号→半角、３Ｍ→3M）、删零宽与空白、统一大写。"""
    s = unicodedata.normalize("NFKC", str(name or ""))
    return re.sub(r"[\s​-‏﻿]", "", s).upper()


def _adcut(v: str) -> str:
    """行政区划词典截齐：仅当 "X省/X市" 段整体命中词典才允许截断（深圳市/黑龙江省）；
    词典不命中的"观城市/超市/市场"一律跳过不截——宁可不截，不可误切。"""
    if not _AD:
        return v  # 词典缺失=禁用一切截齐（宁可漏绑不误切）
    for i in range(len(v)):
        for L in (3, 4, 5):
            seg = v[i:i + L]
            if len(seg) == L and seg[-1] in "省市" and seg in _AD:
                return v if i == 0 else v[i:]
    return v


def _clean_cn(raw: str) -> list[str]:
    """清洗中文全称候选，返回 [原形态, 剥离形态]（去重）。
    拉丁前缀（TCL科技…/GEO深圳…）不再一刀切削掉：原样与剥后两个形态并列送核验，
    由工商精确匹配裁决，避免削掉合法字号（TCL/3M）也保住脏前缀（GEO）路径。"""
    name = raw.strip()
    out = []
    m = re.match(r"^[A-Za-z0-9]+(?=[\u4e00-\u9fa5])", name)
    variants = [name] + ([name[m.end():]] if m else [])
    for v in variants:
        v = _adcut(v)
        # 截齐后仍粘着的口水字/版权词（"版权所有归北京…"）；长词优先只剥一次
        v = re.sub(r"^(?:版权所有归|版权所有|版权|本网站|本站|设计制作|技术支持|由|是|为|与|及|和|属于|我们|其|系)+", "", v)
        if len(v) >= 8 and v not in out:
            out.append(v)
    return out


_FOOTER_CTX = re.compile(r"(©|&copy;|Copyright|版权所有|备案号|ICP备|All Rights Reserved)", re.I)
_CN_WINDOW = 60


def extract_companies(texts: list[str]) -> dict:
    """从页面文本提取公司名候选。返回 {"cn": [...去重], "cn_footer": [...页脚行证据], "en": [...]}。
    页脚证据=名称前后60字内出现 ©/版权/备案/ICP/All Rights Reserved；绑定只认页脚档
    （用户定版：底部写错好歹可解释，提取错页脚不可接受），正文他处提及仅作候选留档。
    文本先过 scrub_text 净化（零宽/中文间空白/NFKC），断字残名整类消灭在匹配前；
    清洗后多形态并列（拉丁前缀原样+剥离），由工商核验裁决。"""
    cn_f, cn_o, en = [], [], []
    for text in (scrub_text(t) for t in (texts or [])):
        for m in _CN_NAME_RE.finditer(text or ""):
            before = text[max(0, m.start() - 8):m.start()]
            after = text[m.end():m.end() + 8]
            if any(n in before or n in after for n in _NOISE_CTX) or before.endswith("由"):
                continue
            win = text[max(0, m.start() - _CN_WINDOW):m.end() + _CN_WINDOW]
            tgt = cn_f if _FOOTER_CTX.search(win) else cn_o
            for n in _clean_cn(m.group(0)):
                if n not in tgt:
                    tgt.append(n)
        for m in list(_EN_NAME_RE.findall(text or "")) + list(_EN_NAME_LOOSE_RE.findall(text or "")):
            n = re.sub(r"^\d{4}[\s,–-]+", "", m.strip(" .,"))
            if 8 <= len(n) <= 80 and n not in en:
                en.append(n)
        if len(cn_f) + len(cn_o) >= 8 and len(en) >= 8:
            break
    cn = cn_f + [x for x in cn_o if x not in cn_f]
    return {"cn": cn[:8], "cn_footer": cn_f[:8], "en": en[:8]}


def snapshot_texts(audit_dir: Path, json_name: str) -> list[str]:
    """读诊断快照 pages.json 的 body_text（诊断时各页实际看到的正文）。无快照返回空。"""
    snap = audit_dir / "snapshots" / Path(json_name).stem / "pages.json"
    if not snap.exists():
        return []
    try:
        pages = json.loads(snap.read_text(encoding="utf-8"))
        # 新格式为 {"note":…, "pages":[…]}（顶层带正文有损摘要说明）；存量快照为裸数组，两种都认
        if isinstance(pages, dict):
            pages = pages.get("pages") or []
        return [str(p.get("body_text") or "") for p in pages if isinstance(p, dict)]
    except (OSError, ValueError):
        return []


_NAME_STOP = ("有限公司", "有限责任", "股份", "公司")


def _name_key(name: str) -> str:
    """曾用名等价键：归一后剥离 词典行政区划名（"深圳市"→""、括号内的"(深圳)"→""）、
    去公司后缀词/括号，**保留字序**做精确比对。
    "深圳中安视达科技 ↔ 中安视达科技（深圳）"这类括号形态差仍可桥接；
    "海中科技 ↔ 中海科技"字序重排不再等价（旧版字符多重集排序会误判同一体）。"""
    s = norm_name(re.sub(r"<[^>]+>", "", str(name or "")))
    for w in _NAME_STOP:
        s = s.replace(w, "")
    s = re.sub(r"[()\\s·,，]", "", s)
    for base in _AD_BASES:  # 长名优先（黑龙江省 先于 林省 类误剥不适用，词典名≥3字且含行政区层级）
        s = s.replace(base.upper(), "")
    return s


# 非正常经营状态：不自动绑，只落人工候选（网站挂注销/吊销主体本身是诊断信号）
_BAD_STATUS = ("注销", "吊销", "撤销", "停业", "清算", "其他")


async def verify_cn(candidates: list[str], api) -> tuple[list[dict], str]:
    """中文全称逐个工商核验，两条路径：
    ① 精准核验：detail(全称) 返回的 companyName 归一后与候选完全相等（现名直查）。
    ② 曾用名核验：①失败时模糊搜，对前 5 条记录查详情，候选出现在其 historyNames
       （词典地名剥离后精确等值）即认定同一主体（站上写的是老注册名，如
       深圳中安视达科技→现名兮元睿视）。曾用名是工商登记的法定名号，不引入字序重排。
    返回 (核验通过列表, api_error)。api_error 非空=工商接口异常（鉴权/欠费/超时），
    与"查无此企业"严格区分——绝不允许把工具不在场说成站内没有。
    命中≥2 即早停（多候选必落人工，继续核验只烧钱不改结论）。
    每项带 status=工商登记经营状态（缺失按存续）。"""
    out = []
    api_err = ""
    for cand in candidates:
        try:
            detail = await api.get_company_detail_by_name(cand, use_cache=True)
        except Exception as e:
            detail, api_err = {}, f"{type(e).__name__}: {e}"
        data = detail or {}
        back = str(data.get("companyName") or data.get("name") or "")
        if back and norm_name(back) == norm_name(cand):
            out.append({"name": cand, "detail": data,
                        "status": str(data.get("businessStatus") or "存续")})
            if len(out) >= 2:
                break
            continue
        try:
            res = await api.search_company(cand, page_size=10)
        except Exception as e:
            res, api_err = {}, f"{type(e).__name__}: {e}"
        for r in (res.get("records") or [])[:5]:
            cname = re.sub(r"<[^>]+>", "", r.get("companyName") or "")
            if len(cname) < 8 or norm_name(cname) == norm_name(back):
                continue
            try:
                det = await api.get_company_detail_by_name(cname, use_cache=True)
            except Exception as e:
                det, api_err = {}, f"{type(e).__name__}: {e}"
            hist = [str(h) for h in ((det or {}).get("historyNames") or [])]
            ckey = _name_key(cand)
            if any(_name_key(h) == ckey for h in hist if len(_name_key(h)) >= 6):
                out.append({"name": cname, "detail": det or {},
                            "status": str((det or {}).get("businessStatus") or "存续")})
                break
        if len(out) >= 2:
            break
    # 有通过者则忽略中间异常（api_error 语义=一次都没成且接口报过错）
    return out, (api_err if not out else "")


def merge_brand_aliases(full_name: str, brand: str):
    """绑定成功后把站点品牌名增量并入档案的品牌名/别名单（去重；品牌名∈别名的约定不变）。"""
    brand = (brand or "").strip()
    if not full_name or not brand:
        return
    try:
        from company_cache import get_info, upsert_info
        old, _ = get_info(full_name)
        old = old or {}
        brands = [b for b in (old.get("brands") or []) if b]
        aliases = [a for a in (old.get("aliases") or []) if a]
        changed = False
        if brand not in brands:
            brands.append(brand)
            changed = True
        if brand not in aliases:
            aliases.append(brand)
            changed = True
        if changed:
            upsert_info(full_name, {"brands": brands, "aliases": aliases}, origin="官网诊断")
    except Exception:
        pass


def match_local_archive(cn_candidates: list[str], brand: str) -> list[str]:
    """本地企业档案库匹配（免工商API）：
    - 中文全称候选：按库内既有归一规则（去符号后相等或互相包含，长度≥4）；
    - 品牌名：匹配各档案的 品牌名(brands)/别名(aliases) 单（归一相等或互相包含）。
    返回命中的档案全称列表（去重）。恰好 1 家=可直接绑定；多家=歧义交人工；0 家=走工商核验。"""
    hits = []
    try:
        from company_cache import _conn, _match_id, norm_name as _norm
        with _conn() as c:
            cur = c.cursor()
            for cand in cn_candidates:
                rid = _match_id(cand, cur)
                if rid:
                    row = cur.execute("SELECT data FROM company WHERE id=?", (rid,)).fetchone()
                    if row:
                        nm = json.loads(row[0]).get("full_name")
                        if nm and nm not in hits:
                            hits.append(nm)
                        continue
                # 全称比不中再比曾用名（站上常印旧注册名）
                ckey = _name_key(cand)
                if len(ckey) < 6:
                    continue
                for rid, data in cur.execute("SELECT id, data FROM company").fetchall():
                    d = json.loads(data)
                    if any(_name_key(str(h)) == ckey for h in (d.get("history_names") or [])):
                        nm = d.get("full_name")
                        if nm and nm not in hits:
                            hits.append(nm)
                        break
            b = _norm(brand)
            if len(b) >= 2:
                for rid, data in cur.execute("SELECT id, data FROM company").fetchall():
                    d = json.loads(data)
                    names = [d.get("full_name") or ""] + [str(x) for x in (d.get("brands") or [])] \
                            + [str(x) for x in (d.get("aliases") or [])] \
                            + [str(x) for x in (d.get("history_names") or [])]  # 曾用名也是法定名号
                    for n in names:
                        n2 = _norm(n)
                        if not n2 or len(min(b, n2)) < 2:
                            continue
                        if b == n2 or (len(min(b, n2)) >= 4 and (b in n2 or n2 in b)):
                            nm = d.get("full_name")
                            if nm and nm not in hits:
                                hits.append(nm)
                            break
    except Exception:
        return []
    return hits


def check_runtime_deps() -> list[str]:
    """绑定现场取页脚的依赖自检：返回缺失清单，空=齐备。
    缺库时必须报错停下，严禁把"工具不在场"误报成"站内没有公司名"。"""
    missing = []
    for mod, use in (("playwright", "渲染页脚"), ("requests", "重抓首页"),
                     ("httpx", "工商核验接口")):
        try:
            __import__(mod)
        except Exception:
            missing.append(f"{mod}({use})")
    return missing


def _rendered_footer_text(url: str) -> tuple[str, str]:
    """无头浏览器渲染首页（与用户所见一致）后取页脚 innerText。
    返回 (页脚文本, 失败原因)：文本空+原因空=成功但页脚无内容；原因非空=渲染没跑成。
    定位顺序：<footer> → class含footer → body末尾可见块。"""
    try:
        from playwright.sync_api import sync_playwright, TimeoutError as _PWTimeout
    except Exception:
        return "", "环境缺playwright库，页脚渲染未执行"
    try:
        with sync_playwright() as pw:
            b = pw.chromium.launch(headless=True)
            try:
                pg = b.new_page()
                pg.goto(url if "//" in url else "https://" + url,
                        wait_until="networkidle", timeout=30000)
                el = (pg.query_selector("footer")
                      or pg.query_selector("[class*='footer']")
                      or pg.query_selector("[id*='footer']")
                      or pg.query_selector("div[class*='Foot']"))
                if el:
                    return el.inner_text() or "", ""
                # 页脚元素定位失败：整页文本中截取公司名所在行作页脚证据
                full = pg.evaluate("() => document.body.innerText")
                for line in (full or "").splitlines():
                    if "有限公司" in line or "版权" in line or "©" in line:
                        return line.strip(), ""
                return pg.evaluate("""() => {
                    const els = [...document.body.children].filter(e => e.offsetParent !== null);
                    return els.length ? els[els.length - 1].innerText : ''; }"""), ""
            finally:
                b.close()
    except _PWTimeout:
        return "", "页脚渲染超时(30s)，页面加载过慢或网络异常"
    except Exception as e:
        return "", f"页脚渲染失败: {type(e).__name__}: {e}"


async def _render_footer(url: str) -> tuple[str, str]:
    try:
        import asyncio
        return await asyncio.to_thread(_rendered_footer_text, url)
    except Exception as e:
        return "", f"页脚渲染失败: {type(e).__name__}: {e}"


async def _fetch_home_text(url: str) -> tuple[str, str]:
    """轻量重抓首页原始HTML去标签（含注释区），供页脚公司名兜底提取。
    返回 (文本, 失败原因)。"""
    try:
        from site_audit.core.fetcher import Fetcher
    except Exception:
        return "", "环境缺requests库（检测抓取器不可用），首页重抓未执行"
    try:
        import asyncio
        p = await asyncio.to_thread(Fetcher().fetch, url if "//" in url else "https://" + url)
        html = re.sub(r"<!--[\s\S]*?-->", " ", p.html or "")  # 注释块=不渲染的历史页脚，必须先剥
        return re.sub(r"<[^>]+>", " ", html), ""
    except Exception as e:
        return "", f"首页重抓失败: {type(e).__name__}: {e}"


# 统一社会信用代码（18位，国标字符集不含 I O Z S V）
_CREDIT_RE = re.compile(r"(?<![0-9A-Za-z])[0-9A-HJ-NPQRTUWXY]{2}[0-9]{6}[0-9A-HJ-NPQRTUWXY]{10}(?![0-9A-Za-z])")


def _merge_domain(full_name: str, url: str):
    """绑定成功即把站点域名并入企业档案（domains 字段），
    并认领该域名名下此前无主的检测快照行（快照归企业身份管）。"""
    try:
        from urllib.parse import urlparse
        from company_cache import add_domains, find_company_id_by_domain, claim_audit_snapshots
        host = urlparse(url if "//" in url else "http://" + url).netloc
        if host:
            add_domains(full_name, host, origin="官网诊断")
            cid = find_company_id_by_domain(host)
            if cid:
                claim_audit_snapshots(host, cid)
    except Exception:
        pass


_NAV_WORDS = ("隐私", "政策", "营业执照", "联系我们", "下载", "关于我们", "首页",
              "登录", "注册", "关注", "扫码", "更多", "合作", "新闻", "加入", "APP")


def _is_dirty(name: str) -> bool:
    """脏候选判定：超长（正规全称≤24字）或名称主体中混入导航菜单词。"""
    return len(name) > 24 or any(w in name[2:] for w in _NAV_WORDS)


async def _llm_clean_candidates(cands: list[str]) -> list[str]:
    """脏候选升级：把已提取到的短串（几十字）交大模型挑出注册全称，
    成本远低于读整页页脚；挑出的名字不豁免——仍走工商精准核验闸门。"""
    dirty = [c for c in cands if _is_dirty(c)]
    if not dirty:
        return cands
    try:
        import yaml
        from llm_client import LLMClient
        lc = yaml.safe_load((Path(__file__).parent / "config.yaml").read_text(encoding="utf-8"))["llm"]
        llm = LLMClient(base_url=lc["base_url"], api_key=lc["api_key"], model=lc["model"],
                        temperature=0, max_tokens=512, timeout=30, max_retries=1,
                        enable_search=False, enable_thinking=False)
        for c in dirty:
            r = await llm.chat([{"role": "user", "content":
                "下面是一串网页页脚文字粘连产生的名称，混有导航菜单词。"
                '请从中提取唯一的工商注册全称（以省/市等行政区划开头、以有限公司/股份有限公司等结尾）。'
                '只输出 JSON {"name": "全称"}；无法确定输出 {"name": ""}。\n名称串：' + c}],
                expect_json=True)
            nm = str((r or {}).get("name") or "").strip() if isinstance(r, dict) else ""
            if nm and not _is_dirty(nm) and nm not in cands:
                cands.append(nm)
    except Exception:
        pass
    return cands


async def bind_report(json_path: Path, api, cache_upsert) -> dict:
    """对一份诊断报告执行绑定：提取→核验→写回 JSON+沉淀档案。
    返回动作摘要（bound / candidates / unchanged / fallback_failed / api_error / skip）。不抛异常。
    unchanged=兜底成功后站内确实没有；fallback_failed=现场取页脚没跑成，绝不能当"没有"；
    api_error=工商接口异常（欠费/鉴权/超时），不得伪装成"查无此企业"。
    绑定前三道闸：①页脚候选字面即所绑名称（现名直查天然满足，残名在提取端消灭）；
    ②工商登记状态非注销/吊销/撤销等才自动绑；③页脚出现信用代码时，核验主体必须与代码登记主体一致。"""
    try:
        d = json.loads(json_path.read_text(encoding="utf-8"))
        if d.get("company_full_name"):
            return {"file": json_path.name, "action": "skip", "why": "已绑定"}
        texts = snapshot_texts(json_path.parent, json_path.name)
        cand = extract_companies(texts)
        foot_evidence = list(texts)  # 信用代码取证池：快照正文 + 后续兜底取到的页脚原文
        # 快照正文常丢页脚（动态渲染站实测）：无页脚证据时先用无头浏览器渲染取页脚
        # （与用户所见一致，V3 主路径），渲染失败再退回原始HTML剥注释提取
        fallback_notes: list[str] = []  # 现场取页脚失败留痕：与"站内确实没有"严格区分
        if not cand["cn_footer"] and d.get("url"):
            _u = str(d.get("final_url") or d.get("url"))
            ft, render_err = await _render_footer(_u)
            if render_err:
                fallback_notes.append(render_err)
            if ft:
                foot_evidence.append(ft)
                rf = extract_companies([ft])["cn_footer"]
                if rf:
                    cand["cn_footer"] = rf
                    cand["cn"] = rf + [x for x in cand["cn"] if x not in rf]
            if not cand["cn_footer"]:
                html, fetch_err = await _fetch_home_text(_u)
                if fetch_err:
                    fallback_notes.append(fetch_err)
                if html:
                    foot_evidence.append(html)
                    cf = extract_companies([html])["cn_footer"]
                    if cf:
                        cand["cn_footer"] = cf
                        cand["cn"] = cf + [x for x in cand["cn"] if x not in cf]
        cn_use = cand["cn_footer"] or cand["cn"]  # 页脚档独占优先
        # 脏候选（菜单词粘连）升级：小输入LLM挑全称，挑出的仍走工商核验
        extra = [x for x in await _llm_clean_candidates(list(cn_use)) if x not in cn_use]
        if extra:
            cn_use += extra
            cand["cn_footer"] = list(dict.fromkeys(cand["cn_footer"] + extra))
        # 第0级：本地档案库先匹配（全称/品牌/别名），命中免调工商API
        local = match_local_archive(cn_use, str(d.get("site_brand") or ""))
        if len(local) == 1:
            d["company_full_name"] = local[0]
            d["company_bind_via"] = "local_archive"
            d["company_candidates"] = cand["en"]
            merge_brand_aliases(local[0], str(d.get("site_brand") or ""))
            _merge_domain(local[0], str(d.get("url") or ""))
            json_path.write_text(json.dumps(d, ensure_ascii=False), encoding="utf-8")
            return {"file": json_path.name, "action": "bound", "company": local[0]}
        if len(local) > 1:
            d["company_candidates"] = local  # 档案多家命中=歧义，人工选定
            json_path.write_text(json.dumps(d, ensure_ascii=False), encoding="utf-8")
            return {"file": json_path.name, "action": "candidates", "candidates": local}
        verified, api_err = (await verify_cn(cn_use, api)) if cn_use else ([], "")
        if api_err and not verified:
            # 工商接口全线异常（欠费/鉴权/超时）：绝不伪装成"查无此企业"落人工
            return {"file": json_path.name, "action": "api_error",
                    "why": f"工商核验接口异常（非站内没有）：{api_err}"}
        vf = [v for v in verified if v["name"] in cand["cn_footer"]]
        if len(verified) != 1 and len(vf) == 1:
            verified = vf  # 多家核验通过但页脚档唯一 → 页脚胜出
        if len(verified) == 1:
            full = verified[0]["name"]
            bad = any(b in verified[0].get("status", "") for b in _BAD_STATUS)
            # 页脚信用代码交叉校验：页脚印有代码时，所绑主体必须就是该代码的登记主体
            codes = [c for t in foot_evidence for c in _CREDIT_RE.findall(t)]
            code_mismatch = False
            if codes:
                credit = str(verified[0]["detail"].get("creditNumber") or
                             verified[0]["detail"].get("creditCode") or "")
                if credit and not any(credit.upper() == c.upper() for c in set(codes)):
                    code_mismatch = True
                elif not credit:
                    try:
                        det2 = await api.get_company_detail_by_credit_code(codes[0], use_cache=True)
                        code_mismatch = bool(det2) and norm_name(str((det2 or {}).get("companyName") or "")) != norm_name(full)
                    except Exception:
                        code_mismatch = False  # 代码查询异常不拦（api_error 已另行分层）
            if bad or code_mismatch:
                d["company_candidates"] = (cand["cn_footer"] or cand["cn"]) + cand["en"]
                json_path.write_text(json.dumps(d, ensure_ascii=False), encoding="utf-8")
                return {"file": json_path.name, "action": "candidates",
                        "candidates": d["company_candidates"],
                        "why": ("工商登记状态异常：" + verified[0]["status"]) if bad
                        else "页脚信用代码与核验主体不一致（署名与登记冲突，需人工）"}
            d["company_full_name"] = full
            d["company_bind_via"] = "site_full_name"
            d["company_candidates"] = cand["en"]  # 英文名留档（若有）
            fields = {}
            try:
                from business_api_client import BusinessAPIClient
                fields = BusinessAPIClient.extract_raw_fields(verified[0]["detail"])
                fields.update(BusinessAPIClient.extract_layer1_fields(verified[0]["detail"]))
            except Exception:
                pass  # 提取器不可用时只绑定不入库（自检环境无 httpx 依赖也能跑通）
            if fields:
                # 与其他分析页面同口径：存在即字段级更新、不存在即创建
                cache_upsert(full, fields, credit_code=fields.get("credit_code"), origin="官网诊断")
            merge_brand_aliases(full, str(d.get("site_brand") or ""))  # 品牌名/别名增量并入档案
            _merge_domain(full, str(d.get("url") or ""))
            action = "bound"
        else:
            if not (cn_use or cand["en"]):
                if fallback_notes:
                    # 现场取页脚没跑成（缺库/超时/抓取失败），绝不允许说成"站内没有"
                    return {"file": json_path.name, "action": "fallback_failed",
                            "why": "现场取页脚失败（并非站内没有）：" + "；".join(fallback_notes)}
                return {"file": json_path.name, "action": "unchanged",
                        "why": "站内未提取到页脚公司名（两级兜底均成功，确实没有可提取的全称）"}
            d["company_candidates"] = (cand["cn_footer"] or cand["cn"]) + cand["en"]
            action = "candidates"
        json_path.write_text(json.dumps(d, ensure_ascii=False), encoding="utf-8")
        return {"file": json_path.name, "action": action,
                "company": d.get("company_full_name", ""),
                "candidates": d.get("company_candidates", [])}
    except Exception as e:
        return {"file": json_path.name, "action": "error", "why": f"{type(e).__name__}: {e}"}


if __name__ == "__main__":
    # 自检：提取与归一（不发请求）
    t1 = "© 2026 深圳飞柚数智营销技术有限公司 版权所有 飞柚GEO是深圳市飞柚数智营销技术有限公司旗下品牌"
    r1 = extract_companies([t1])
    assert "深圳飞柚数智营销技术有限公司" in r1["cn"] and "深圳市飞柚数智营销技术有限公司" in r1["cn"], r1
    assert extract_companies(["GEO\n深圳飞柚数智营销技术有限公司"])["cn"] == \
        ["GEO深圳飞柚数智营销技术有限公司", "深圳飞柚数智营销技术有限公司"], \
        "拉丁前缀双形态并列送核验（由工商裁决，不再一刀切）"
    assert extract_companies(["本站由广州某大数据有限公司技术支持"])["cn"] == [], "技术支持噪声应剔除"
    _r = extract_companies(["Copyright © 2026 广州娇莉芙信息科技服务有限公司版权所有 All Rights Reserved 粤ICP备10021905号-1",
                            "公司全称：广州娇莉芙个人护理用品有限公司 我们服务客户如 国家电网有限公司"])
    assert _r["cn_footer"] == ["广州娇莉芙信息科技服务有限公司"], _r
    assert _r["cn"][0] == "广州娇莉芙信息科技服务有限公司" and "广州娇莉芙个人护理用品有限公司" in _r["cn"]
    # "城市"是普通名词非行政区划：北京天一博观城市… 不得从"市"截齐（历史误截成"一博观…"）
    assert extract_companies(["©2019-2022 北京天一博观城市规划设计院有限公司版权所有 All rights reserved 备案号：11010502041666"])["cn"] \
        == ["北京天一博观城市规划设计院有限公司"]
    r2 = extract_companies(["© 2026 Shenzhen ZSTEK Technology Co., Ltd. All Rights Reserved."])
    assert any("ZSTEK" in e for e in r2["en"]), r2
    assert extract_companies(["欢迎来到我们的网站，提供优质服务。"]) == {"cn": [], "cn_footer": [], "en": []}
    assert _is_dirty("隐私权政策个人信息保护政策营业执照联系我们下载圆领APP圆领上海魔法向量网络有限公司")
    assert not _is_dirty("上海魔法向量网络有限公司") and not _is_dirty("TCL科技集团股份有限公司")
    assert norm_name("深圳 飞柚（数智）营销技术有限公司") == norm_name("深圳飞柚(数智)营销技术有限公司")
    assert norm_name("A公司") != norm_name("B公司")
    print("audit_company_bind OK", r1["cn"], r2["en"])
