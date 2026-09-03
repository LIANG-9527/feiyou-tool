"""导出「检测项范围对照表」——从报告 JSON + 代码行为清单生成 docs/ 口径表。

用途（2026-09-03 LIANG 问责后的整改产物）：
- 钉死每项检查的 标称范围(scope) / 适用页(applicable_pages) / 实际执行页（代码行为）/ 证据页字段
- 防止「元数据与实际行为脱节」类数据范围错误复发

用法：
    python3 scripts/export_check_scope_table.py [报告JSON路径]
    默认取 output/audit/ 下最新的报告 JSON（提供检查项花名册/名称/优先级/状态）；
    标称范围与适用页以代码里的 SCOPE_MAP / APPLICABLE_PAGES_MAP 为准（反映当前代码口径）。
    输出 docs/检测项范围对照表-v1.md
"""
import glob
import json
import sys
from datetime import datetime
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from site_audit.audit.checker import SCOPE_MAP, APPLICABLE_PAGES_MAP  # noqa: E402

# ── 实际代码行为清单（2026-09-03 阿栈逐项核对源码，随代码变更必须同步更新）──
# id: (检测方式, 实际执行页, 代码依据)
ACTUAL_BEHAVIOR = {
    # 技术可抓取性（technical.py）
    "tech-01": ("规则", "首页", "technical.py _check_https：解析首页 final_url scheme"),
    "tech-02": ("规则", "全站配置", "technical.py _check_robots_txt：抓 /robots.txt"),
    "tech-03": ("规则", "全站配置", "technical.py _check_sitemap：抓 /sitemap.xml"),
    "tech-04": ("规则", "全站配置", "technical.py _check_llms_txt：抓 /llms.txt"),
    "tech-05": ("规则", "全站配置", "technical.py _check_well_known：抓 /.well-known/"),
    "tech-06": ("规则", "首页", "technical.py _check_indexability：首页 meta robots"),
    "tech-07": ("规则", "首页", "technical.py _check_canonical：首页 canonical 标签"),
    "tech-08": ("规则", "首页", "technical.py _check_status_codes：首页状态码/跳转链"),
    "tech-09": ("规则", "首页", "technical.py _check_content_accessibility：首页正文可抓取性"),
    "tech-10": ("规则", "首页", "technical.py _check_page_size_speed：首页体积/响应时间"),
    "tech-11": ("规则", "首页", "technical.py _check_mobile_friendly：首页 viewport"),
    "tech-12": ("规则", "全部抽样内页", "technical.py:323 遍历 extra_pages 检查每个内页 URL 路径"),
    "tech-13": ("规则", "全部抽样页", "cross_page.py:52 check_internal_link_status(all_pages)"),
    "tech-14": ("规则", "首页", "technical.py _check_cms_fingerprint：首页特征识别（CMS诊断项，不计分）"),
    "tech-15": ("规则", "首页", "technical.py _check_admin_exposure：常见后台路径探测（CMS诊断项，不计分）"),
    # AI 友好度（ai_friendly.py）
    "ai-01": ("规则", "首页", "ai_friendly.py _check_title：首页 title"),
    "ai-02": ("规则", "首页", "ai_friendly.py _check_description：首页 meta description"),
    "ai-03": ("规则", "首页", "ai_friendly.py _check_heading_structure：首页 H1/H2/H3"),
    "ai-04": ("规则", "首页", "ai_friendly.py _check_structured_data：首页 JSON-LD"),
    "ai-05": ("规则", "首页", "ai_friendly.py _check_answer_format：首页列表/表格结构"),
    "ai-06": ("规则", "抽样内容页单页", "ai_friendly.py:215 _pick_content_page 挑单页查面包屑"),
    "ai-07": ("规则", "首页", "ai_friendly.py _check_multi_page：首页站内链接统计"),
    "ai-08": ("规则", "首页", "ai_friendly.py _check_ai_indexing：首页站长验证 meta"),
    "ai-09": ("AI", "抽样内容页单页", "ai_friendly.py:317 _pick_content_page → check_nlp_friendly"),
    "ai-10": ("AI", "抽样内容页单页", "ai_friendly.py:342 _pick_content_page → check_content_chunking"),
    "ai-11": ("规则", "全部抽样页", "cross_page.py:78 遍历 all_pages 对比 Title/Description"),
    # 内容质量（content.py）
    "content-01": ("规则", "首页", "content.py _check_content_depth：首页正文字数"),
    "content-02": ("规则+AI混合", "全部内容页(规则)+抽样单页(AI)", "content.py:62 全内容页时间标注覆盖 + :85 挑单页 check_originality"),
    "content-03": ("规则", "首页", "content.py _check_faq：首页 FAQPage/FAQ DOM"),
    "content-04": ("规则", "首页", "content.py _check_image_optimization：首页图片 alt"),
    "content-05": ("规则", "首页", "content.py _check_internal_link_quality：首页锚文本"),
    "content-06": ("规则", "首页", "content.py _check_multilingual：首页 hreflang"),
    "content-07": ("AI", "抽样内容页单页", "content.py:134 _pick_content_page → check_search_intent"),
    "content-08": ("规则", "全部抽样页", "cross_page.py:127 遍历 all_pages 关键词蚕食对比"),
    # 权威信任（authority.py）
    "auth-01": ("规则", "首页", "authority.py _check_trust_signals：首页页脚备案/联系方式"),
    "auth-02": ("规则", "首页", "authority.py _check_customer_testimonials：首页评价/Logo"),
    "auth-03": ("规则", "首页", "authority.py _check_social_presence：首页社交链接"),
    "auth-04": ("规则", "首页", "authority.py _check_external_links：首页外链域名"),
    "auth-05": ("AI+外部平台", "首页提取品牌名+外部百科平台", "authority.py:173 → check_knowledge_graph：品牌变体×6通道（百度百科wap词条页直连主通道+桌面API/item页备用+维基+头条百科+必应site:兜底），通道健康度追踪，全灭降级SKIP，否定结果标「需人工复核」，原始响应进快照search_raw"),
    "auth-06": ("AI+外部平台", "首页提取品牌名+外部社交平台", "authority.py:192 → check_cross_platform：首页社交链接+知乎搜索+LLM"),
    "auth-07": ("AI", "抽样内容页单页", "authority.py:214 _pick_content_page → check_eeat"),
    # B端增强（b2b.py）
    "b2b-01": ("规则", "首页", "b2b.py _check_product_pages：首页产品/方案链接"),
    "b2b-02": ("规则", "首页", "b2b.py _check_case_studies：首页案例链接"),
    "b2b-03": ("规则", "首页", "b2b.py _check_solution_pages：首页行业方案链接"),
    "b2b-04": ("规则", "首页", "b2b.py _check_comparison_content：首页对比内容"),
    "b2b-05": ("规则+AI混合", "全部抽样页", "b2b.py:163 check_tech_docs(page, all_pages)：规则扫文档链接+LLM评估"),
    "b2b-06": ("规则+AI混合", "全部抽样页", "b2b.py:182 check_qa_content(page, all_pages)：规则扫FAQ+LLM评估"),
    "b2b-07": ("AI", "首页", "b2b.py:201 check_conversion_path(page)：首页CTA提取+LLM"),
}

CMS_ITEMS = {"tech-14", "tech-15"}  # scorer.py CMS_ITEM_IDS：只展示不计分


def latest_report() -> Path:
    files = sorted(glob.glob(str(REPO / "output" / "audit" / "*.json")),
                   key=lambda f: Path(f).stat().st_mtime)
    if not files:
        sys.exit("output/audit/ 下没有报告 JSON，请先跑一次诊断")
    return Path(files[-1])


def main():
    report_path = Path(sys.argv[1]) if len(sys.argv) > 1 else latest_report()
    data = json.loads(report_path.read_text(encoding="utf-8"))
    results = data["results"]

    lines = []
    lines.append("# 检测项范围对照表 v1")
    lines.append("")
    lines.append(f"- 生成时间：{datetime.now():%Y-%m-%d %H:%M}")
    lines.append(f"- 数据来源：{report_path.name}（prompt_version={data.get('prompt_version','')}）")
    lines.append("- 实际执行页口径：逐项核对源码得出（`scripts/export_check_scope_table.py` 内 ACTUAL_BEHAVIOR，随代码变更同步）")
    lines.append("")
    lines.append("## 口径说明（先看这个）")
    lines.append("")
    scored = [r for r in results if r["id"] not in CMS_ITEMS]
    lines.append(f"1. **输出 {len(results)} 项，计分 {len(scored)} 项**：tech-14 CMS指纹识别、tech-15 后台路径暴露为诊断项，只展示不计分（scorer.py CMS_ITEM_IDS）。对外口径「46 项」指计分项。")
    lines.append("2. **标称范围（scope）**：首页=只在首页执行；全站=跨全部抽样页/全站配置；内容页=从抽样内页挑一篇文章/案例/产品页单页执行（checker.py `_pick_content_page`，按 文章→案例→产品 优先级，要求正文>100字）。")
    lines.append("3. **适用页（applicable_pages）**：该检查项针对的页面类型。内页/内容页项在无抽样内页时 SKIP（本次未检，不计分），不判失败。")
    lines.append(f"4. **抽样规模**：首页 + 最多 12 个内页（runner.py max_pages=12），本次实际抽样 {len(data.get('sampled_pages', []))} 页。")
    lines.append("5. **证据页字段**：内容页/跨页项在 detail 里写入「检查页面: URL/页标题」，HTML 报告逐项展示，客户可当场点验。")
    lines.append("6. **命名规则（2026-09-03 LIANG 定）**：对客户/群内沟通一律用业务名（检查项名称），编码只允许出现在代码、commit、配置表三类技术载体。下方「逐项对照」表的 ID↔检查项 即全团队唯一的编码↔业务名字典，新增检查项先起业务名再分配编码。")
    lines.append("")
    lines.append("## 编码 ↔ 业务名 速查（唯一字典）")
    lines.append("")
    lines.append(" | ".join(f"{r['id']}={r['name']}" for r in results[:24]))
    lines.append("")
    lines.append(" | ".join(f"{r['id']}={r['name']}" for r in results[24:]))
    lines.append("")
    lines.append("## 逐项对照")
    lines.append("")
    lines.append("| ID | 检查项 | 优先级 | 检测方式 | 标称范围 | 适用页 | 实际执行页（代码行为） |")
    lines.append("|---|---|---|---|---|---|---|")
    for r in results:
        rid = r["id"]
        method, actual, code_ref = ACTUAL_BEHAVIOR.get(rid, ("?", "?", "未核对"))
        note = "（不计分）" if rid in CMS_ITEMS else ""
        scope = SCOPE_MAP.get(rid, "首页")
        applicable = APPLICABLE_PAGES_MAP.get(rid, "首页")
        lines.append(f"| {rid}{note} | {r['name']} | {r['priority']} | {method} | {scope} | {applicable} | {actual} |")
    lines.append("")
    lines.append("## 代码依据")
    lines.append("")
    for r in results:
        rid = r["id"]
        _, _, code_ref = ACTUAL_BEHAVIOR.get(rid, ("?", "?", "未核对"))
        lines.append(f"- **{rid}**：{code_ref}")
    lines.append("")
    lines.append("## 修正记录")
    lines.append("")
    lines.append("- 2026-09-03（阿栈）：修正 15 项范围/适用页字段与实际行为脱节（tech-02~05/12/13、ai-06/09/10/11、content-07/08、auth-07、b2b-05/06）；auth-05 百科查询从 3 单点扩为 5 通道+健康度追踪，通道全灭降级 SKIP；html_report 检测页面改取 detail 实际证据页。commit 8eccad7 / c30e30e / 2f7a080。本表标称范围与适用页以代码 SCOPE_MAP/APPLICABLE_PAGES_MAP 为准。")
    lines.append("")

    out = REPO / "docs" / "检测项范围对照表-v1.md"
    out.write_text("\n".join(lines), encoding="utf-8")
    print(f"written: {out} ({len(results)} items)")


if __name__ == "__main__":
    main()
