"""对已生成的市场调研报告，只重排「情报简报」一步并重渲染整份 HTML。

用法:
  python3 scripts/rerun_mr_deck.py output/市场调研_xxx.json          # 调模型重排(产出新版式)
  python3 scripts/rerun_mr_deck.py output/市场调研_xxx.json --local  # 纯本地重渲染(旧数据无构件版式不变)

新报告写到同目录，文件名加 _deckv2；原报告与原始 JSON 不动。
"""
import sys, json, asyncio
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))
from main import load_config, build_agent, generate_markdown_report
import yaml

as_text_mode = "--local" in sys.argv
json_path = Path(next(a for a in sys.argv[1:] if not a.startswith("--"))).resolve()
result = json.loads(json_path.read_text(encoding="utf-8"))

skill = yaml.safe_load((ROOT / "skills" / "15_mr_deck.yaml").read_text(encoding="utf-8"))


async def rerun_deck():
    agent = build_agent(load_config())
    prompt = agent._render(skill["prompt"], result)
    new_deck = await agent._llm_call(prompt)  # expect_json → {"slides":[...]}
    slides = new_deck.get("slides") if isinstance(new_deck, dict) else None
    if not slides or not any(s.get("blocks") for s in slides):
        raise SystemExit("模型未产出 blocks 构件，放弃覆盖（原数据保留）")
    result["mr_deck"] = new_deck
    print("重排完成:", len(slides), "页 ·",
          sum(len(s.get("blocks") or []) for s in slides), "个构件块")


if not as_text_mode:
    asyncio.run(rerun_deck())

html = generate_markdown_report(result)
out = json_path.with_name(json_path.stem + "_deckv2.html")
out.write_text(html, encoding="utf-8")
print("→", out)
