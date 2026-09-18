"""渲染回归自检：真实货拉拉 JSON 渲染断言（数据提取/组装/渲染三层）。"""
import sys, json
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))
from main import generate_markdown_report, _mr_brief_html, _mr_deck_slides_html

jf = Path(__file__).parent.parent / "var" / "output" / "市场调研_深圳依时货拉拉科技有限公司_20260913_210708.json"
d = json.loads(jf.read_text(encoding="utf-8"))
h = generate_markdown_report(d)

# 渲染层
assert "&lt;br&gt;" not in h, "表格内 br 仍被字面转义"
assert "__TITLE__" not in h and "__SLIDES__" not in h and "__REPORT__" not in h, "占位符残留"
assert "市场调研引擎版本: 2026.09.14.1" in h, "版本注释"

# 组装层（画像）
assert h.count('class="pf-card"') >= 6, "画像卡片数"
assert "发展历程（最近在前）" in h, "时间轴卡"
assert "待补清单" in h, "待补卡"
org_json = h.split('<script id="orgData" type="application/json">')[1].split("</script>")[0]
import re
names = re.findall(r'"name": "([^"]+)"', org_json)
assert all(len(n) <= 24 for n in names), "节点名超长"
assert not any(n in ("广州", "上海", "北京", "成都", "武汉", "杭州") for n in names), "城市名仍入图"
assert "各地城市分公司" not in names, "泛称仍入图"
assert "广东深圳" not in names and "讯美科技广场" not in names, "总部不再作为组织图子节点"

# 简报 blocks
assert h.count('class="slide"') == 8, "八页简报"
assert h.count("sb-src") >= 8, "页页来源行"

# 空时间轴不产生空卡（苏奥传感旧数据无 timeline 场景由兼容路径覆盖）
b = _mr_brief_html({"info": {"full_name": "X公司"}}, "X公司")
assert "发展历程" not in b, "空时间轴不应渲染卡"
print("渲染回归自检 OK")
