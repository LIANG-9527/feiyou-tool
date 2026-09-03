"""HTML 报告生成"""
import html as html_mod
from site_audit.core.models import AuditReport, Status, Category
from site_audit.core.scorer import grade_from_score


def e(text) -> str:
    """HTML 转义，防止内容中的 < > 破坏页面结构"""
    if text is None:
        return ""
    return html_mod.escape(str(text), quote=True)


STATUS_STYLES = {
    Status.PASS: {"label": "通过", "color": "#10b981", "bg": "#ecfdf5"},
    Status.WARN: {"label": "警告", "color": "#f59e0b", "bg": "#fffbeb"},
    Status.FAIL: {"label": "失败", "color": "#ef4444", "bg": "#fef2f2"},
    Status.SKIP: {"label": "未检查", "color": "#dc2626", "bg": "#fef2f2"},
}

GRADE_COLORS = {
    "A": "#10b981",
    "B": "#3b82f6",
    "C": "#f59e0b",
    "D": "#f97316",
    "F": "#ef4444",
}


def _score_bar(score: float) -> str:
    """生成分数进度条"""
    color = "#10b981" if score >= 75 else ("#f59e0b" if score >= 50 else "#ef4444")
    return f'''
    <div style="background:#e5e7eb;border-radius:4px;height:8px;overflow:hidden;">
        <div style="width:{score}%;background:{color};height:100%;border-radius:4px;"></div>
    </div>'''


def _standards_html(r) -> str:
    """生成三级标准的 HTML"""
    excellent = e(r.excellent) or "-"
    normal = e(r.normal) or "-"
    failed = e(r.failed) or "-"
    return f'''
    <div style="font-size:11px;line-height:1.6;">
        <div style="color:#10b981;"><b>优秀：</b>{excellent}</div>
        <div style="color:#f59e0b;"><b>普通：</b>{normal}</div>
        <div style="color:#ef4444;"><b>未完成：</b>{failed}</div>
    </div>'''


def generate_html_report(report: AuditReport) -> str:
    """生成完整 HTML 报告"""
    summary = report.summary
    grade_color = GRADE_COLORS.get(report.grade, "#6b7280")

    # 维度得分卡片
    category_cards = ""
    for c in report.categories:
        category_cards += f'''
        <div style="background:#fff;border-radius:8px;padding:16px;box-shadow:0 1px 3px rgba(0,0,0,0.1);">
            <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:8px;">
                <span style="font-weight:600;color:#374151;">{e(c.category.value)}</span>
                <span style="font-size:20px;font-weight:700;color:#111827;">{c.score}</span>
            </div>
            {_score_bar(c.score)}
            <div style="margin-top:8px;font-size:12px;color:#6b7280;">
                通过 {c.passed} · 警告 {c.warned} · 失败 {c.failed}
            </div>
        </div>'''

    # 行渲染函数（详细诊断和待优化清单共用）
    def _render_row(r):
        style = STATUS_STYLES[r.status]
        detail_html = f'<div style="margin-top:4px;font-size:11px;color:#6b7280;">{e(r.detail)}</div>' if r.detail else ""
        purpose_impact_html = ""
        if r.purpose or r.impact:
            purpose_text = f'<div style="font-size:11px;color:#4b5563;line-height:1.5;margin-bottom:4px;"><b>作用：</b>{e(r.purpose)}</div>' if r.purpose else ""
            impact_text = f'<div style="font-size:11px;color:#dc2626;line-height:1.5;"><b>缺失影响：</b>{e(r.impact)}</div>' if r.impact else ""
            purpose_impact_html = purpose_text + impact_text
        manual_html = f'<div style="font-size:11px;color:#4b5563;line-height:1.5;">{e(r.manual_check)}</div>' if r.manual_check else '<div style="font-size:11px;color:#9ca3af;">-</div>'
        standards_html = _standards_html(r) if (r.excellent or r.normal or r.failed) else '<div style="font-size:11px;color:#9ca3af;">-</div>'
        scope_color = "#0ea5e9" if r.scope == "全站" else "#8b5cf6"
        scope_bg = "#f0f9ff" if r.scope == "全站" else "#f5f3ff"
        scope_label = f'{r.scope}级'
        checked_page = report.final_url if r.scope == "首页" else "全站配置"
        app_color = "#f59e0b" if r.applicable_pages == "内页" else "#10b981"
        app_bg = "#fffbeb" if r.applicable_pages == "内页" else "#ecfdf5"
        scope_html = f'''
        <div style="margin-top:6px;">
            <span style="display:inline-block;padding:1px 6px;border-radius:8px;
                font-size:10px;font-weight:600;color:{scope_color};background:{scope_bg};">
                {scope_label}
            </span>
            <span style="display:inline-block;padding:1px 6px;border-radius:8px;
                font-size:10px;font-weight:600;color:{app_color};background:{app_bg};margin-left:4px;">
                {r.applicable_pages}适用
            </span>
            <span style="font-size:10px;color:#9ca3af;margin-left:4px;">检测页面: {e(checked_page)}</span>
        </div>'''
        suggestion_html = ""
        if r.suggestion and r.status in (Status.FAIL, Status.WARN):
            suggestion_html = f'''
            <div style="margin-top:8px;padding:8px;background:#f0fdf4;border-left:3px solid #22c55e;border-radius:0 4px 4px 0;">
                <div style="font-size:10px;font-weight:700;color:#16a34a;margin-bottom:2px;">修复建议</div>
                <div style="font-size:11px;color:#166534;line-height:1.5;">{e(r.suggestion)}</div>
            </div>'''
        return f'''
        <tr style="border-bottom:1px solid #f3f4f6;vertical-align:top;">
            <td style="padding:12px 10px;min-width:130px;">
                <div style="font-weight:600;color:#374151;font-size:13px;">{e(r.item_name)}</div>
                <div style="margin-top:3px;">
                    <span style="display:inline-block;padding:1px 6px;border-radius:8px;
                        font-size:10px;font-weight:600;color:#fff;background:#6366f1;">
                        {e(r.priority.value)}
                    </span>
                </div>
            </td>
            <td style="padding:12px 8px;text-align:center;white-space:nowrap;">
                <span style="display:inline-block;padding:3px 10px;border-radius:12px;
                    font-size:12px;font-weight:600;color:{style["color"]};background:{style["bg"]};">
                    {style["label"]}
                </span>
            </td>
            <td style="padding:12px 10px;font-size:12px;color:#4b5563;line-height:1.6;min-width:200px;">
                {e(r.message)}
                {detail_html}
                {suggestion_html}
                {scope_html}
            </td>
            <td style="padding:12px 10px;min-width:220px;">
                {purpose_impact_html if purpose_impact_html else '<div style="font-size:11px;color:#9ca3af;">-</div>'}
            </td>
            <td style="padding:12px 10px;min-width:180px;">
                {manual_html}
            </td>
            <td style="padding:12px 10px;min-width:200px;">
                {standards_html}
            </td>
        </tr>'''

    # 表格头（共用）
    _table_head = '''<tr style="background:#f9fafb;">
        <th style="padding:10px;text-align:left;font-size:12px;color:#6b7280;width:11%;">检查项</th>
        <th style="padding:10px;text-align:center;font-size:12px;color:#6b7280;width:7%;">状态</th>
        <th style="padding:10px;text-align:left;font-size:12px;color:#6b7280;width:15%;">检测结果</th>
        <th style="padding:10px;text-align:left;font-size:12px;color:#6b7280;width:24%;">作用与缺失影响</th>
        <th style="padding:10px;text-align:left;font-size:12px;color:#6b7280;width:19%;">怎么检查（手动）</th>
        <th style="padding:10px;text-align:left;font-size:12px;color:#6b7280;width:24%;">优秀 / 普通 / 未完成标准</th>
    </tr>'''

    # CMS诊断独立板块（tech-14/tech-15 不参与维度详细结果）
    cms_item_ids = {"tech-14", "tech-15"}

    # 按维度分组的详细结果（排除CMS诊断项，它们单独展示）
    detail_sections = ""
    for cat in Category:
        cat_results = [r for r in report.results if r.category == cat and r.item_id not in cms_item_ids]
        if not cat_results:
            continue
        rows = "".join(_render_row(r) for r in cat_results)
        detail_sections += f'''
        <div style="margin-top:28px;">
            <h3 style="font-size:16px;font-weight:700;color:#111827;margin-bottom:12px;
                padding-left:10px;border-left:4px solid #3b82f6;">{e(cat.value)}</h3>
            <div style="overflow-x:auto;">
            <table style="width:100%;border-collapse:collapse;background:#fff;border-radius:8px;
                overflow:hidden;box-shadow:0 1px 3px rgba(0,0,0,0.1);min-width:1200px;">
                <thead>{_table_head}</thead>
                <tbody>{rows}</tbody>
            </table>
            </div>
        </div>'''

    # 待优化清单（所有未通过项：fail + warn + skip，排除CMS诊断项）
    non_pass = [r for r in report.results if r.status in (Status.FAIL, Status.WARN, Status.SKIP) and r.item_id not in cms_item_ids]
    status_order = {Status.FAIL: 0, Status.WARN: 1, Status.SKIP: 2}
    priority_order = {"P0": 0, "P1": 1, "P2": 2}
    non_pass.sort(key=lambda r: (priority_order.get(r.priority.value, 9), status_order.get(r.status, 9)))

    todo_list_html = ""
    if non_pass:
        fail_count = sum(1 for r in non_pass if r.status == Status.FAIL)
        warn_count = sum(1 for r in non_pass if r.status == Status.WARN)
        skip_count = sum(1 for r in non_pass if r.status == Status.SKIP)
        todo_rows = "".join(_render_row(r) for r in non_pass)
        todo_list_html = f'''
    <!-- 待优化清单 -->
    <div style="margin-top:28px;">
        <h2 style="font-size:18px;font-weight:700;color:#111827;margin-bottom:4px;">待优化清单</h2>
        <p style="font-size:13px;color:#6b7280;margin-top:0;">
            共 {len(non_pass)} 项待处理 ·
            <span style="color:#ef4444;font-weight:600;">失败 {fail_count}</span> ·
            <span style="color:#f59e0b;font-weight:600;">警告 {warn_count}</span> ·
            <span style="color:#9ca3af;font-weight:600;">未检查 {skip_count}</span>
            （按优先级和严重程度排序）
        </p>
        <div style="overflow-x:auto;margin-top:12px;">
        <table style="width:100%;border-collapse:collapse;background:#fff;border-radius:8px;
            overflow:hidden;box-shadow:0 1px 3px rgba(0,0,0,0.1);min-width:1200px;">
            <thead>{_table_head}</thead>
            <tbody>{todo_rows}</tbody>
        </table>
        </div>
    </div>'''

    # 优先修复建议（P0 失败项，排除CMS诊断项）
    p0_fails = [r for r in report.results if r.priority.value == "P0" and r.status == Status.FAIL and r.item_id not in cms_item_ids]
    fix_suggestions = ""
    if p0_fails:
        items = ""
        for r in p0_fails:
            normal_text = e(r.normal) if r.normal else ""
            items += f"<li><b>{e(r.item_name)}</b> — {e(r.message)}<br><span style='font-size:12px;color:#991b1b;opacity:0.8;'>修复参考：{normal_text}</span></li>"
        fix_suggestions = f'''
        <div style="margin-top:24px;background:#fef2f2;border-radius:8px;padding:20px;">
            <h3 style="font-size:16px;font-weight:700;color:#991b1b;margin:0 0 12px 0;">
                优先修复（P0 失败项）
            </h3>
            <ul style="margin:0;padding-left:20px;color:#7f1d1d;font-size:14px;line-height:2;">
                {items}
            </ul>
        </div>'''

    # CMS诊断独立板块
    cms_results = [r for r in report.results if r.item_id in cms_item_ids]
    cms_rows = ""
    for r in cms_results:
        cms_rows += _render_row(r)
    cms_section_html = f'''
    <div style="margin-top:28px;">
        <h2 style="font-size:18px;font-weight:700;color:#111827;margin-bottom:4px;">CMS诊断与接手评估</h2>
        <p style="font-size:13px;color:#6b7280;margin-top:0;">
            识别建站系统类型，评估技术栈是否适合接手整改，检测后台路径安全性
        </p>
        <div style="overflow-x:auto;margin-top:12px;">
        <table style="width:100%;border-collapse:collapse;background:#fff;border-radius:8px;
            overflow:hidden;box-shadow:0 1px 3px rgba(0,0,0,0.1);min-width:1200px;">
            <thead>{_table_head}</thead>
            <tbody>{cms_rows}</tbody>
        </table>
        </div>
    </div>''' if cms_rows else ""

    # 维度得分卡片HTML
    dimension_scores_html = f'''
    <div style="display:grid;grid-template-columns:repeat(3,1fr);gap:16px;margin-bottom:8px;">
        {category_cards}
    </div>'''

    # 打分逻辑说明表格
    weight_rows = ""
    for c in report.categories:
        weight_pct = int(c.weight * 100)
        score_color = GRADE_COLORS.get(grade_from_score(c.score), "#6b7280")
        weight_rows += (
            '<tr style="border-bottom:1px solid #f3f4f6;">'
            f'<td style="padding:8px 12px;font-size:13px;color:#374151;">{e(c.category.value)}</td>'
            f'<td style="padding:8px 12px;text-align:center;font-size:13px;font-weight:600;color:#111827;">{weight_pct}%</td>'
            f'<td style="padding:8px 12px;text-align:center;font-size:13px;color:#6b7280;">{c.total} 项</td>'
            f'<td style="padding:8px 12px;text-align:center;font-size:13px;">'
            f'<span style="font-weight:700;color:{score_color};">{c.score}</span></td>'
            '</tr>'
        )

    scoring_logic = f"""
    <div style="margin-top:20px;background:#fff;border-radius:8px;padding:20px;box-shadow:0 1px 3px rgba(0,0,0,0.1);">
        <h3 style="font-size:15px;font-weight:700;color:#111827;margin:0 0 16px 0;
            padding-left:10px;border-left:4px solid #6366f1;">打分逻辑说明</h3>
        <div style="display:grid;grid-template-columns:1fr 1fr;gap:20px;">
            <div>
                <div style="font-size:13px;font-weight:600;color:#374151;margin-bottom:8px;">一、维度权重与得分</div>
                <table style="width:100%;border-collapse:collapse;font-size:12px;">
                    <thead>
                        <tr style="background:#f9fafb;">
                            <th style="padding:8px 12px;text-align:left;font-size:12px;color:#6b7280;">维度</th>
                            <th style="padding:8px 12px;text-align:center;font-size:12px;color:#6b7280;">权重</th>
                            <th style="padding:8px 12px;text-align:center;font-size:12px;color:#6b7280;">检查项</th>
                            <th style="padding:8px 12px;text-align:center;font-size:12px;color:#6b7280;">得分</th>
                        </tr>
                    </thead>
                    <tbody>{weight_rows}</tbody>
                </table>
                <div style="margin-top:8px;font-size:11px;color:#9ca3af;">
                    总分 = Σ(各维度得分 × 维度权重)
                </div>
            </div>
            <div>
                <div style="font-size:13px;font-weight:600;color:#374151;margin-bottom:8px;">二、单项计分规则</div>
                <div style="display:grid;grid-template-columns:repeat(4,1fr);gap:8px;margin-bottom:16px;">
                    <div style="background:#ecfdf5;border-radius:6px;padding:10px;text-align:center;">
                        <div style="font-size:18px;font-weight:700;color:#10b981;">1分</div>
                        <div style="font-size:11px;color:#065f46;">通过</div>
                    </div>
                    <div style="background:#fffbeb;border-radius:6px;padding:10px;text-align:center;">
                        <div style="font-size:18px;font-weight:700;color:#f59e0b;">0.5分</div>
                        <div style="font-size:11px;color:#92400e;">警告</div>
                    </div>
                    <div style="background:#fef2f2;border-radius:6px;padding:10px;text-align:center;">
                        <div style="font-size:18px;font-weight:700;color:#ef4444;">0分</div>
                        <div style="font-size:11px;color:#991b1b;">失败</div>
                    </div>
                    <div style="background:#f3f4f6;border-radius:6px;padding:10px;text-align:center;">
                        <div style="font-size:18px;font-weight:700;color:#6b7280;">--</div>
                        <div style="font-size:11px;color:#374151;">未检查<br><span style="font-size:10px;">不计入分母</span></div>
                    </div>
                </div>
                <div style="font-size:13px;font-weight:600;color:#374151;margin-bottom:8px;">三、等级划分标准</div>
                <div style="display:grid;grid-template-columns:repeat(5,1fr);gap:6px;">
                    <div style="background:#ecfdf5;border-radius:6px;padding:8px;text-align:center;">
                        <div style="font-size:16px;font-weight:700;color:#10b981;">A</div>
                        <div style="font-size:10px;color:#065f46;">90-100</div>
                    </div>
                    <div style="background:#eff6ff;border-radius:6px;padding:8px;text-align:center;">
                        <div style="font-size:16px;font-weight:700;color:#3b82f6;">B</div>
                        <div style="font-size:10px;color:#1e40af;">75-89</div>
                    </div>
                    <div style="background:#fffbeb;border-radius:6px;padding:8px;text-align:center;">
                        <div style="font-size:16px;font-weight:700;color:#f59e0b;">C</div>
                        <div style="font-size:10px;color:#92400e;">60-74</div>
                    </div>
                    <div style="background:#fff7ed;border-radius:6px;padding:8px;text-align:center;">
                        <div style="font-size:16px;font-weight:700;color:#f97316;">D</div>
                        <div style="font-size:10px;color:#9a3412;">40-59</div>
                    </div>
                    <div style="background:#fef2f2;border-radius:6px;padding:8px;text-align:center;">
                        <div style="font-size:16px;font-weight:700;color:#ef4444;">F</div>
                        <div style="font-size:10px;color:#991b1b;">0-39</div>
                    </div>
                </div>
            </div>
        </div>
    </div>"""

    # 多页面抽样覆盖
    if report.sampled_pages:
        rows = ""
        for idx, p in enumerate(report.sampled_pages, 1):
            status_color = "#16a34a" if p.get("status") == 200 else "#dc2626"
            rows += (
                '<tr style="border-bottom:1px solid #f3f4f6;">'
                f'<td style="padding:7px 12px;font-size:12px;color:#6b7280;white-space:nowrap;">{idx}</td>'
                f'<td style="padding:7px 12px;font-size:12px;color:#111827;max-width:360px;word-break:break-all;">{e(p.get("title") or "(无标题)")}</td>'
                f'<td style="padding:7px 12px;font-size:11px;color:#2563eb;word-break:break-all;max-width:420px;">{e(p.get("url",""))}</td>'
                f'<td style="padding:7px 12px;text-align:center;font-size:12px;font-weight:600;color:{status_color};">{p.get("status")}</td>'
                f'<td style="padding:7px 12px;text-align:center;font-size:12px;color:#6b7280;white-space:nowrap;">{p.get("size_kb", 0):.0f}KB</td>'
                '</tr>'
            )
        sampled_pages_html = f"""
    <div style="margin-top:20px;background:#fff;border-radius:8px;padding:20px;box-shadow:0 1px 3px rgba(0,0,0,0.1);">
        <h3 style="font-size:15px;font-weight:700;color:#111827;margin:0 0 6px 0;
            padding-left:10px;border-left:4px solid #0ea5e9;">多页面抽样覆盖（共 {len(report.sampled_pages)} 个内页）</h3>
        <p style="font-size:12px;color:#6b7280;margin:0 0 12px 10px;">除首页外，按 产品/方案 &gt; 案例 &gt; 资讯 &gt; 关于 的优先级抽样抓取内页；断链检测、Title/Description唯一性、关键词蚕食基于这些页面跨页分析</p>
        <table style="width:100%;border-collapse:collapse;">
            <thead><tr style="background:#f9fafb;border-bottom:2px solid #e5e7eb;">
                <th style="padding:8px 12px;text-align:left;font-size:12px;color:#374151;width:40px;">#</th>
                <th style="padding:8px 12px;text-align:left;font-size:12px;color:#374151;">页面标题</th>
                <th style="padding:8px 12px;text-align:left;font-size:12px;color:#374151;">URL</th>
                <th style="padding:8px 12px;text-align:center;font-size:12px;color:#374151;width:70px;">状态</th>
                <th style="padding:8px 12px;text-align:center;font-size:12px;color:#374151;width:70px;">大小</th>
            </tr></thead>
            <tbody>{rows}</tbody>
        </table>
    </div>"""
    else:
        sampled_pages_html = """
    <div style="margin-top:20px;background:#fffbeb;border:1px solid #fde68a;border-radius:8px;padding:14px 20px;font-size:13px;color:#92400e;">
        本次仅检测到首页，未从首页发现可抽样的同域内页（可能首页链接为 JS 动态渲染或内链极少）；断链检测、Title唯一性、关键词蚕食等跨页面检查已跳过。
    </div>"""

    html = f'''<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>GEO 诊断报告 - {e(report.url)}</title>
<style>
    body {{ font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", "PingFang SC",
        "Hiragino Sans GB", "Microsoft YaHei", sans-serif; background:#f3f4f6; margin:0; padding:0; }}
    .container {{ max-width: 1400px; margin: 0 auto; padding: 24px; }}
</style>
</head>
<body>
<div class="container">
    <!-- 头部 -->
    <div style="background:linear-gradient(135deg,#1e3a5f 0%,#2563eb 100%);border-radius:12px;
        padding:32px;color:#fff;margin-bottom:24px;">
        <div style="font-size:14px;opacity:0.8;margin-bottom:8px;">GEO 官网诊断报告</div>
        <div style="font-size:24px;font-weight:700;margin-bottom:4px;">{e(report.url)}</div>
        <div style="font-size:13px;opacity:0.7;">检测时间: {e(report.timestamp)} · 检测页面: {e(report.final_url)}（首页级检查基于此页面，全站级检查基于整个站点）</div>
    </div>

    <!-- 总分概览 -->
    <div style="display:grid;grid-template-columns:1fr 2fr;gap:24px;margin-bottom:24px;">
        <div style="background:#fff;border-radius:12px;padding:24px;text-align:center;
            box-shadow:0 1px 3px rgba(0,0,0,0.1);">
            <div style="font-size:48px;font-weight:800;color:{grade_color};line-height:1;">
                {report.total_score}
            </div>
            <div style="font-size:14px;color:#6b7280;margin-top:4px;">总分 (0-100)</div>
            <div style="margin-top:12px;display:inline-block;padding:4px 16px;border-radius:20px;
                font-size:18px;font-weight:700;color:#fff;background:{grade_color};">
                {e(report.grade)} 级
            </div>
        </div>
        <div style="background:#fff;border-radius:12px;padding:24px;
            box-shadow:0 1px 3px rgba(0,0,0,0.1);">
            <div style="font-size:14px;font-weight:600;color:#374151;margin-bottom:16px;">检测概览</div>
            <div style="display:grid;grid-template-columns:repeat(4,1fr);gap:12px;text-align:center;">
                <div>
                    <div style="font-size:28px;font-weight:700;color:#374151;">{summary["total"]}</div>
                    <div style="font-size:12px;color:#6b7280;">总检查项</div>
                </div>
                <div>
                    <div style="font-size:28px;font-weight:700;color:#10b981;">{summary["pass"]}</div>
                    <div style="font-size:12px;color:#6b7280;">通过</div>
                </div>
                <div>
                    <div style="font-size:28px;font-weight:700;color:#f59e0b;">{summary["warn"]}</div>
                    <div style="font-size:12px;color:#6b7280;">警告</div>
                </div>
                <div>
                    <div style="font-size:28px;font-weight:700;color:#ef4444;">{summary["fail"]}</div>
                    <div style="font-size:12px;color:#6b7280;">失败</div>
                </div>
            </div>
        </div>
    </div>

    {dimension_scores_html}
    {scoring_logic}

    <!-- CMS诊断板块 -->
    {cms_section_html}

    {sampled_pages_html}

    {todo_list_html}

    {fix_suggestions}

    <!-- 详细结果 -->
    <div style="margin-top:28px;">
        <h2 style="font-size:18px;font-weight:700;color:#111827;margin-bottom:4px;">详细诊断结果</h2>
        <p style="font-size:13px;color:#6b7280;margin-top:0;">每项包含：手动检查方法、作用说明、三级标准（优秀/普通/未完成）</p>
    </div>
    {detail_sections}

    <!-- 页脚 -->
    <div style="margin-top:32px;text-align:center;font-size:12px;color:#9ca3af;padding:16px;">
        GEO 官网诊断工具 · 本报告基于自动化检测生成，仅供参考
    </div>
</div>
</body>
</html>'''
    return html


def save_html_report(report: AuditReport, output_path: str):
    """保存 HTML 报告"""
    html = generate_html_report(report)
    with open(output_path, "w", encoding="utf-8") as f:
        f.write(html)
    return output_path
