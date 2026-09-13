"""情报简报 blocks 渲染自检：跑断言 + 产出预览页 output/_deck_preview.html 供人眼验收。"""
import sys, json
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))
from main import _mr_deck_slides_html

deck = {"slides": [
    {"k": "01 · 经营模式", "t": "加盟为主+整家转型", "p": "依赖C端零售与经销体系，正从单品向整家过渡。", "blocks": [
        {"type": "two_col", "cards": [
            {"title": "💰 盈利方式", "rows": [["出厂差价", "品牌制造+经销：赚差价（核心）"], ["品牌授权", "加盟体系品牌与供货"], ["工程/大宗", "占比5-10%，收缩控风险"]]},
            {"title": "🧲 获客渠道", "rows": [["卖场自然客流", "客流较峰值降40-60%（行业共识）"], ["线上付费投放", "SEM/信息流，CAC千元级/线索"], ["小红书种草", "流量洼地，内容同质化"], ["经销商自获客", "区域分散，难统一归因"]]}]},
        {"type": "note", "text": "地区商业氛围：珠三角产业带核心，经销商文化浓厚，对渠道效率话题痛感强"},
        {"type": "src", "text": "来源：2023年报口径 · 渠道权重为研判，首访验证"}]},
    {"k": "03 · 产品×GEO适配", "t": "旧改是实验点", "blocks": [
        {"type": "bars", "items": [
            {"name": "整家定制", "desc": "占比50-60% · 毛利30-35%", "score": 85, "note": "基本盘"},
            {"name": "旧改局装", "desc": "政策红利 · 决策短", "score": None, "note": "实验点候选"},
            {"name": "木门墙柜", "desc": "占比10-15% · 转型单品", "score": 80, "note": "转型点"},
            {"name": "工程大宗", "desc": "回款风险 · 战略收缩", "score": 40, "note": "不碰"}]},
        {"type": "timeline", "items": [["单品期", "整体衣柜专家，心智停留单品"], ["转型期", "整家/门墙柜——战略升级认知未跟上"], ["突围期", "存量旧改+AI入口，补获客基建"]]},
        {"type": "src", "text": "来源：公司年报与公开报道口径 · 适配度为调研推断"}]},
    {"k": "04 · 口碑", "t": "夸环保骂增项", "blocks": [
        {"type": "words", "cols": [
            {"title": "🟢 正面词（抽样归纳）", "chips": [{"w": "原态环保", "src": "小红书抽样"}, {"w": "上市公司", "src": "点评抽样"}, {"w": "门店多", "src": "点评抽样"}, {"w": "设计不错", "src": "推断"}]},
            {"title": "🔴 负面词（抽样归纳）", "chips": [{"w": "增项收费", "src": "黑猫投诉案例"}, {"w": "延期交货", "src": "抖音评论区"}, {"w": "设计还原度差", "src": "微博吐槽帖"}, {"w": "售后响应慢", "src": "黑猫投诉"}]}]},
        {"type": "note", "text": "关键发现：环保信任赤字是 GEO 突破口——认证可视化（检测报告结构化）+ FAQ 纠偏（增项/延期高频问答上首页）"},
        {"type": "src", "text": "来源：黑猫/12315/大众点评/小红书「品牌词×评价·投诉·怎么样」抽样归纳 · 无来源词标推断"}]},
    {"k": "05 · 竞品占位", "t": "头部夹击，蓝海在场景词", "blocks": [
        {"type": "table", "head": ["意图组", "客户出现率", "竞品Top1", "竞品Top2", "差距"],
         "rows": [["本地到店（广州全屋定制哪家好）", "待实测", "待实测", "待实测", "预计红海"],
                  ["品牌对比（好莱客和索菲亚怎么选）", "待实测", "待实测", "待实测", "待实测"],
                  ["需求场景（旧改用什么板材环保）", "待实测", "待实测", "待实测", "预计蓝海"]]},
        {"type": "note", "text": "竞品名单：欧派 200亿+/7000店 大家居心智垄断；索菲亚 150亿+/4000店 数字化最强；志邦 性价比；尚品宅配 线上占比最高"},
        {"type": "src", "text": "来源：第四章竞品对比矩阵 · 出现率一律待实测"}]},
    {"k": "06 · 媒介全景", "t": "重投放，AI入口空白", "blocks": [
        {"type": "media", "groups": [
            {"title": "🌐 线上", "rows": [["SEM/信息流投放", "y", "巨额投入，费率14-16%推算"], ["官网SEO", "y", "基础有，长尾不足"], ["AI GEO布局", "n", "官网无结构化数据"], ["行业媒体软文", "q", "待查"]]},
            {"title": "📺 线下", "rows": [["卖场门店", "y", "核心阵地 1000+ 店"], ["电梯/地铁广告", "q", "有代言历史待核实"], ["展会/经销商大会", "y", "行业常态"]]}]},
        {"type": "note", "text": "调仓线索：年度营销预算约3.8-4.2亿（营收×15%推算，口径待补充）——从低效投放切10-15%做AI入口专项"},
        {"type": "src", "text": "来源：第三章渠道评估 · 预算为估算口径，?项首访验证"}]},
    {"k": "08 · GEO参考", "t": "三档词库先防守", "blocks": [
        {"type": "kws", "cols": [
            {"tier": "防守词（先做）", "desc": "品牌词/负面词 · 难度低见效快", "chips": [{"w": "好莱客怎么样", "src": "正文建议"}, {"w": "好莱客增项多吗", "src": "正文建议"}, {"w": "好莱客和索菲亚怎么选", "src": "正文建议"}]},
            {"tier": "争夺词（主攻）", "desc": "品类词 · 中等难度", "chips": [{"w": "广州全屋定制哪家好", "src": "正文建议"}, "定制衣柜品牌排行", "环保定制家具哪家好"]},
            {"tier": "培育词（资产）", "desc": "场景长尾 · 见效慢但沉淀", "chips": [{"w": "旧房衣柜改造", "src": "正文建议"}, "门墙柜颜色怎么统一", "儿童房定制"]}]},
        {"type": "note", "text": "词源问句池本系统留空（后续单独接入）；此处只列维度占位"},
        {"type": "src", "text": "来源：第六七章方案与预算 · 种子词待全量100问词库扩展"}]},
    {"k": "07 · 区域内同行", "t": "本地工厂店截刚需", "blocks": [
        {"type": "table", "head": ["同行名称", "定位", "区域声量", "直接竞争点"],
         "rows": [["欧派（总部广州）", "行业第一", "高", "同区域正面竞争"],
                  ["索菲亚（增城）", "全国头部", "高", "同区域正面竞争"],
                  ["尚品宅配（佛山）", "线上基因", "中高", "线上流量竞争"],
                  ["本地中小定制厂", "价格杀手", "待查", "下沉/价格敏感客群"]]},
        {"type": "note", "text": "待补：搜「广州定制家居公司」+大众点评同城排名补齐本地名单"},
        {"type": "src", "text": "来源：竞品总部为公开信息 · 本地名单待补"}]},
]}

h = _mr_deck_slides_html(deck)
assert h.count('<div class="slide">') == 7, "7页"
assert '<div class="sb sb-two">' in h and "💰 盈利方式" in h, "two_col"
assert 'width:85%' in h and "待补充 · 实验点候选" in h, "bars 有分/无分"
assert 'class="sb-wd pos">' in h and "<i>黑猫投诉案例</i>" in h, "词云带来源+pos/neg色"
assert "待补充" in h and "推断" in h, "缺数字占位"
assert "sb-note\">地区商业氛围" in h, "note 块"
assert '<div class="sb sb-note">未知内容块</div>' in _mr_deck_slides_html(
    {"slides": [{"t": "x", "blocks": [{"type": "mystery"}]}]}), "无文本未知块兜底文案"
assert "&lt;b&gt;" in _mr_deck_slides_html({"slides": [{"t": "x<b>", "items": ["y<script>"]}]}), "转义"
assert h.count("<ul>") == 0, "blocks 页不误走旧列表"
assert h.count('<div class="sb sb-src">') == 7, "src 块页页有"
esc = _mr_deck_slides_html({"slides": [{"t": "旧格式", "items": ["要点A", "要点B<script>"]}]})
assert "<ul>" in esc and "&lt;script&gt;" in esc and "<script>" not in esc, "旧格式要点卡+转义"
assert _mr_deck_slides_html("非json{") == "" and _mr_deck_slides_html({"slides": [{"k": "x"}]}) == "", "坏输入返回空"

tpl = (Path(__file__).parent.parent / "skills" / "mr_report_template.html").read_text(encoding="utf-8")
assert ".sb-kw.d1" in tpl and ".sb-tablewrap th" in tpl, "模板构件CSS已就位"
out = (Path(__file__).parent.parent / "output" / "_deck_preview.html")
out.write_text(tpl.replace("__TITLE__", "版式预览").replace("__META__", "自检产物").replace("__SLIDES__", h)
               .replace("__TOTAL_PAGES__", "7").replace("__BRIEF__", "").replace("__REPORT__", ""), encoding="utf-8")
print("OK", len(h), "chars →", out)
