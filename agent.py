"""
Agent — 通用编排器
从 skills/ 目录加载 YAML skill 定义,按 order 顺序执行。
支持4种 skill 类型: single / parallel / iterate / iterate_parallel
改 skill 只需编辑 YAML 文件,无需改代码。
"""
from __future__ import annotations
import asyncio
import json
import re
import time
import yaml
from pathlib import Path
from typing import Optional, Callable, Any
from llm_client import LLMClient

BASE_DIR = Path(__file__).resolve().parent


class Agent:
    def __init__(self, llm: LLMClient, max_concurrency: int = 5, thinking: bool | None = None):
        self.llm = llm
        # 本次任务是否启用深度思考：None=跟随全局配置；True/False=按请求覆盖（如市场调研的开关）
        self.thinking = thinking
        self.semaphore = asyncio.Semaphore(max_concurrency)
        self.skills = self._load_skills()
        self.methods = self._load_methods()
        self.system_prompt = self._load_system_prompt()

    def _load_skills(self) -> list[dict]:
        skills_dir = BASE_DIR / "skills"
        skills = []
        for f in sorted(skills_dir.glob("*.yaml")):
            skill = yaml.safe_load(f.read_text(encoding="utf-8"))
            skills.append(skill)
        skills.sort(key=lambda s: s.get("order", 0))
        return skills

    def _load_methods(self) -> list[dict]:
        p = BASE_DIR / "methods.yaml"
        if not p.exists():
            return []
        data = yaml.safe_load(p.read_text(encoding="utf-8"))
        return data.get("methods", [])

    def get_method(self, method_id: str | None) -> dict | None:
        if not method_id or not self.methods:
            return None
        return next((m for m in self.methods if m["id"] == method_id), None)

    def get_skills_for_method(self, method_id: str | None) -> list[dict]:
        """Return skills filtered by method, or all skills if no method specified."""
        method = self.get_method(method_id)
        if not method:
            return self.skills
        skill_ids = set(method.get("skills", []))
        return [s for s in self.skills if s["id"] in skill_ids]

    def _load_system_prompt(self) -> str:
        p = BASE_DIR / "references" / "system_prompt.txt"
        return p.read_text(encoding="utf-8")

    @staticmethod
    def _render(template: str, context: dict) -> str:
        """Replace {{variable}} with context values. Missing/None → empty string."""
        def replacer(match):
            key = match.group(1)
            value = context.get(key)
            if value is None:
                return ""
            if isinstance(value, str):
                return value
            return json.dumps(value, ensure_ascii=False, indent=2)
        return re.sub(r'\{\{(\w+)\}\}', replacer, template)

    def _build_context(self, skill: dict, ctx: dict, stage: Optional[dict] = None) -> dict:
        """Build context for skill execution, applying context builders."""
        context = dict(ctx)
        if stage:
            context[skill.get("iterate_as", "stage")] = stage
            if isinstance(stage, dict):
                for k in ("stage_name", "stage_index"):
                    if k in stage:
                        context[k] = stage[k]
        for builder in skill.get("context_builders", []):
            btype = builder["type"]
            if btype == "match_by_field":
                source = ctx.get(builder["source"], [])
                match_by = builder["match_by"]
                match_value = stage.get(match_by) if stage else None
                matched = None
                for item in source:
                    if item.get(match_by) == match_value:
                        matched = item
                        break
                context[builder["output_as"]] = matched or {}
            elif btype == "extract_queries":
                source = ctx.get(builder["source"], {})
                match_by = builder["match_by"]
                match_value = stage.get(match_by) if stage else None
                stage_data = source.get(str(match_value), source.get(match_value, {}))
                context[builder["output_as"]] = self._extract_queries(stage_data)
        return context

    @staticmethod
    def _extract_queries(stage_data: dict) -> list[dict]:
        """Extract all queries from a step4 stage result."""
        queries = []
        lines = stage_data.get("lines", [])
        if isinstance(lines, list):
            for line in lines:
                if isinstance(line, dict):
                    for topic in line.get("topics", []):
                        if isinstance(topic, dict):
                            for q in topic.get("queries", []):
                                queries.append(q)
                elif isinstance(line, list):
                    for item in line:
                        if isinstance(item, dict):
                            for topic in item.get("topics", []):
                                if isinstance(topic, dict):
                                    for q in topic.get("queries", []):
                                        queries.append(q)
        for fill in stage_data.get("gap_fills", []):
            if isinstance(fill, dict) and fill.get("query"):
                queries.append(fill)
        return queries

    @staticmethod
    def extract_all_queries(stage_data: dict) -> list[dict]:
        """Public static method for use by main.py summary."""
        return Agent._extract_queries(stage_data)

    async def run(self, brand: str, product: Optional[str] = None,
                  on_progress: Callable = None,
                  method_id: str = None) -> dict:
        ctx: dict[str, Any] = {"brand": brand, "product": product}
        if product:
            ctx["product_hint"] = f"用户特别关注产品/服务: {product}"
        else:
            ctx["product_hint"] = ""
        t0 = time.time()

        skills = self.get_skills_for_method(method_id)
        method = self.get_method(method_id)
        if method:
            ctx["method_id"] = method_id
            ctx["method_name"] = method.get("name", "")

        for skill in skills:
            sid = skill["id"]
            sname = skill["name"]
            stype = skill["type"]
            if on_progress:
                on_progress(sid, f"开始: {sname}", time.time() - t0)

            if stype == "single":
                result = await self._exec_single(skill, ctx)
            elif stype == "parallel":
                result = await self._exec_parallel(skill, ctx, on_progress, t0)
            elif stype == "iterate":
                result = await self._exec_iterate(skill, ctx, on_progress, t0)
            elif stype == "iterate_parallel":
                result = await self._exec_iterate_parallel(skill, ctx, on_progress, t0)
            else:
                raise ValueError(f"Unknown skill type: {stype}")

            ctx[skill["output"]] = result
            if on_progress:
                on_progress(sid, f"完成: {sname}", time.time() - t0)

        ctx["total_elapsed"] = time.time() - t0
        ctx["token_usage"] = getattr(self.llm, "token_usage", None)
        ctx["self_check"] = self._gen_self_check(ctx, skills)
        return ctx

    def _gen_self_check(self, ctx: dict, skills: list[dict] = None) -> list[dict]:
        all_checks = [
            ("Step 1: 核心产品/服务清单", "step1_products", "step1"),
            ("Step 1.5: 名称变体集合", "step1_5_variants", "step1_5"),
            ("Step 2: 目标客群画像", "step2_personas", "step2"),
            ("Step 3: 决策旅程阶段框架", "step3_journey", "step3"),
            ("Step 3.5: 双搜索入口", "step3_5_dual_entry", "step3_5"),
            ("Step 4: 阶段话题+问句", "step4_topics", "step4"),
            ("Step 5: 意图簇汇流", "step5_clusters", "step5"),
            ("Step 6: 意图簇反演验证", "step6_verification", "step6"),
        ]
        if skills:
            skill_ids = {s["id"] for s in skills}
            checks = [(l, k, sid) for l, k, sid in all_checks if sid in skill_ids]
        else:
            checks = all_checks
        result = []
        for label, key, _ in checks:
            val = ctx.get(key)
            has_data = bool(val) if isinstance(val, (list, dict)) else val is not None
            result.append({"item": label, "status": "pass" if has_data else "fail"})
        return result

    async def _llm_call(self, prompt: str, expect_json: bool = True) -> Any:
        messages = [
            {"role": "system", "content": self.system_prompt},
            {"role": "user", "content": prompt},
        ]
        async with self.semaphore:
            return await self.llm.chat(messages, expect_json=expect_json, thinking=self.thinking)

    @staticmethod
    def _clean_md_output(text):
        """清洗调研正文：联网模式下模型偶发把检索计划/伪工具调用当正文输出（qwen3.8 实测）。
        删除成对 tool_call 块与 function=/parameter= 标签行；从第一个 # 标题起截取正文。"""
        import re
        if not isinstance(text, str):
            return text
        text = re.sub("<tool_call[^>]*>[\\s\\S]*?</tool_call>", "", text)
        text = re.sub("^(?:\\s*)</?(?:tool_call|function[=>][^>]*|parameter[=>][^>]*).*$", "", text, flags=re.M)
        m = re.search("^#{1,4} ", text, flags=re.M)
        if m:
            text = text[m.start():]
        return text.strip() + "\
"

    async def _exec_single(self, skill: dict, ctx: dict) -> Any:
        context = self._build_context(skill, ctx)
        prompt = self._render(skill["prompt"], context)
        # format: markdown 的 skill（如市场调研）需返回纯文本，不做 JSON 解析
        as_text = skill.get("format") == "markdown"
        if as_text:
            prompt += "\n\n（直接从 # 标题开始输出正文；禁止开场白、计划陈述和任何工具调用文本）"
        result = await self._llm_call(prompt, expect_json=not as_text)
        if as_text:
            result = self._clean_md_output(result)
        return result

    async def _exec_parallel(self, skill: dict, ctx: dict,
                             on_progress, t0) -> Any:
        context = self._build_context(skill, ctx)
        line_template = skill.get("line_prompt", "")
        lines = skill.get("lines", [])
        if on_progress:
            on_progress(skill["id"], f"并行{len(lines)}线...", time.time() - t0)

        async def run_line(line):
            line_ctx = dict(context)
            line_ctx["line_name"] = line["name"]
            line_ctx["line_instruction"] = line.get("instruction", "")
            prompt = self._render(line_template, line_ctx)
            return await self._llm_call(prompt)

        line_results = await asyncio.gather(*[run_line(l) for l in lines])

        if skill.get("aggregate"):
            context["line_results"] = json.dumps(line_results, ensure_ascii=False, indent=2)
            agg_prompt = self._render(skill["aggregate_prompt"], context)
            return await self._llm_call(agg_prompt)
        return line_results

    async def _exec_iterate(self, skill: dict, ctx: dict,
                            on_progress, t0) -> dict:
        items = ctx.get(skill["iterate_over"], [])
        results = {}
        for i, item in enumerate(items):
            sname = item.get("stage_name", f"阶段{i+1}")
            if on_progress:
                on_progress(skill["id"], f"阶段{i+1}/{len(items)}: {sname}", time.time() - t0)
            context = self._build_context(skill, ctx, item)
            prompt = self._render(skill["prompt"], context)
            result = await self._llm_call(prompt)
            key = str(item.get("stage_index", i))
            results[key] = result
        return results

    async def _exec_iterate_parallel(self, skill: dict, ctx: dict,
                                    on_progress, t0) -> dict:
        items = ctx.get(skill["iterate_over"], [])
        line_template = skill.get("line_prompt", "")
        lines = skill.get("lines", [])
        results = {}
        for i, item in enumerate(items):
            stage_name = item.get("stage_name", f"阶段{i+1}")
            if on_progress:
                on_progress(skill["id"],
                            f"阶段{i+1}/{len(items)}: {stage_name} - 并行{len(lines)}线",
                            time.time() - t0)
            context = self._build_context(skill, ctx, item)

            async def run_line(line):
                line_ctx = dict(context)
                line_ctx["line_name"] = line["name"]
                line_ctx["line_instruction"] = line.get("instruction", "")
                line_ctx["stage_name"] = stage_name
                prompt = self._render(line_template, line_ctx)
                return await self._llm_call(prompt)

            line_results = await asyncio.gather(*[run_line(l) for l in lines])

            stage_data = {
                "stage_index": item.get("stage_index"),
                "stage_name": stage_name,
                "lines": line_results if isinstance(line_results, list) else [line_results],
            }

            if skill.get("gap_check"):
                if on_progress:
                    on_progress(skill["id"], f"阶段{i+1}: 补漏检查", time.time() - t0)
                gap_ctx = dict(context)
                gap_ctx["line_results"] = json.dumps(line_results, ensure_ascii=False, indent=2)
                gap_ctx["stage_name"] = stage_name
                gap_prompt = self._render(skill["gap_check_prompt"], gap_ctx)
                gap_result = await self._llm_call(gap_prompt)
                stage_data["gap_fills"] = (gap_result.get("gap_fills", [])
                                           if isinstance(gap_result, dict)
                                           else (gap_result if isinstance(gap_result, list) else []))

            key = str(item.get("stage_index", i))
            results[key] = stage_data
        return results


class ChatAgent:
    """Conversational Agent — uses LLM function-calling to autonomously select and execute skills."""

    def __init__(self, llm: LLMClient, max_concurrency: int = 5):
        self.llm = llm
        self.runner = Agent(llm, max_concurrency)
        self.skills = self.runner.skills
        self.system_prompt = self._build_system_prompt()
        self.tools = self._build_tools()

    def _build_system_prompt(self) -> str:
        skill_list = "\n".join(
            f"  - {s['id']} ({s['name']}): {s.get('description', '')} [type: {s['type']}]"
            for s in self.skills
        )
        method_list = ""
        if self.runner.methods:
            method_list = "\n".join(
                f"  - {m['id']} ({m['name']}): {m.get('description', '')} — skills: {', '.join(m.get('skills', []))}"
                for m in self.runner.methods
            )
        method_section = f"\n## 可用分析方法\n{method_list}\n" if method_list else ""
        return (
            "你是GEO搜索问句挖掘Agent,一个能对话、能自主调用skill的智能体。\n\n"
            "## 你的能力\n"
            "1. 调用skill: 你可以调用以下skill来完成搜索问句挖掘任务\n"
            "2. 对话: 与用户自然交流,回答问题,解释结果\n"
            "3. 管理skill: 读取、修改skill的YAML配置\n"
            "4. 查看报告: 列出历史分析报告\n"
            "5. 管理分析方法: 列出、查看分析方法配置\n"
            f"{method_section}\n"
            "## 可用Skill列表\n"
            f"{skill_list}\n\n"
            "## 工作流\n"
            "分析品牌时,先确认分析方法,然后按顺序调用该方法对应的skill。\n"
            "每个skill的输出会自动作为后续skill的输入。\n"
            "首次调用step1时必须提供brand参数。\n\n"
            "## 对话风格\n"
            "- 简洁专业,不说废话\n"
            "- 调用skill前简要说明要做什么\n"
            "- 调用skill后简要总结结果\n"
            "- 遇到问题主动询问用户\n"
        )

    def _build_tools(self) -> list[dict]:
        tools = []
        for skill in self.skills:
            params: dict[str, dict] = {}
            required: list[str] = []
            for inp in skill.get("inputs", []):
                if inp in ("brand", "product"):
                    desc = "企业/品牌名称" if inp == "brand" else "目标产品/服务(可选)"
                    params[inp] = {"type": "string", "description": desc}
                    if inp == "brand":
                        required.append(inp)
            tools.append({
                "type": "function",
                "function": {
                    "name": skill["id"],
                    "description": f"{skill['name']} — {skill.get('description', '')}",
                    "parameters": {
                        "type": "object",
                        "properties": params,
                        "required": required,
                    },
                },
            })
        tools.append({
            "type": "function",
            "function": {
                "name": "list_skills",
                "description": "列出所有可用的skill及其配置",
                "parameters": {"type": "object", "properties": {}},
            },
        })
        tools.append({
            "type": "function",
            "function": {
                "name": "read_skill",
                "description": "读取某个skill的YAML配置内容",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "filename": {"type": "string", "description": "skill文件名,如01_core_products.yaml"}
                    },
                    "required": ["filename"],
                },
            },
        })
        tools.append({
            "type": "function",
            "function": {
                "name": "update_skill",
                "description": "更新某个skill的YAML配置内容",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "filename": {"type": "string", "description": "skill文件名"},
                        "content": {"type": "string", "description": "新的YAML内容"},
                    },
                    "required": ["filename", "content"],
                },
            },
        })
        tools.append({
            "type": "function",
            "function": {
                "name": "list_reports",
                "description": "列出所有已生成的分析报告",
                "parameters": {"type": "object", "properties": {}},
            },
        })
        tools.append({
            "type": "function",
            "function": {
                "name": "list_methods",
                "description": "列出所有可用的分析方法及其skill配置",
                "parameters": {"type": "object", "properties": {}},
            },
        })
        return tools

    async def chat(self, user_message: str, history: list[dict], ctx: dict,
                   on_event: Callable = None) -> tuple[str, list[dict]]:
        """Agent loop: call LLM with tools, execute skills, respond."""
        import yaml as _yaml
        messages = [{"role": "system", "content": self.system_prompt}] + history
        messages.append({"role": "user", "content": user_message})
        max_iterations = 15

        for _ in range(max_iterations):
            if on_event:
                on_event({"type": "thinking"})
            msg = await self.llm.chat_with_tools(messages, self.tools)

            if msg.get("tool_calls"):
                messages.append({
                    "role": "assistant",
                    "tool_calls": msg["tool_calls"],
                    "content": msg.get("content", ""),
                })
                for tc in msg["tool_calls"]:
                    fn = tc["function"]
                    name = fn["name"]
                    args = (json.loads(fn["arguments"])
                            if isinstance(fn["arguments"], str)
                            else fn["arguments"])
                    if on_event:
                        skill = next((s for s in self.skills if s["id"] == name), None)
                        desc = skill["name"] if skill else name
                        on_event({"type": "tool_start", "tool_name": name, "tool_desc": desc})
                    try:
                        result = await self._execute_tool(name, args, ctx)
                        result_str = json.dumps(result, ensure_ascii=False)
                        if len(result_str) > 8000:
                            result_str = result_str[:8000] + "...(已截断)"
                    except Exception as e:
                        result_str = json.dumps({"error": str(e)}, ensure_ascii=False)
                    if on_event:
                        summary = (result.get("summary", "")
                                   if isinstance(result, dict) else "")
                        on_event({"type": "tool_end", "tool_name": name, "summary": summary})
                    messages.append({
                        "role": "tool",
                        "tool_call_id": tc["id"],
                        "content": result_str,
                    })
            else:
                text = msg.get("content", "")
                if on_event:
                    on_event({"type": "text", "content": text})
                return text, messages[1:]

        return "已达到最大工具调用次数,请缩小问题范围后重试。", messages[1:]

    async def _execute_tool(self, name: str, args: dict, ctx: dict) -> dict:
        if name == "list_skills":
            return {"skills": [
                {"id": s["id"], "name": s["name"], "type": s["type"], "order": s["order"]}
                for s in self.skills
            ]}
        if name == "read_skill":
            path = BASE_DIR / "skills" / args["filename"]
            if not path.exists():
                return {"error": f"文件不存在: {args['filename']}"}
            return {"filename": args["filename"], "content": path.read_text(encoding="utf-8")}
        if name == "update_skill":
            path = BASE_DIR / "skills" / args["filename"]
            yaml.safe_load(args["content"])
            path.write_text(args["content"], encoding="utf-8")
            return {"status": "ok", "message": "保存成功,下次分析自动生效"}
        if name == "list_reports":
            out_dir = BASE_DIR / "output"
            if not out_dir.exists():
                return {"reports": []}
            reports = []
            for f in out_dir.iterdir():
                if f.suffix == ".html":
                    reports.append({
                        "filename": f.name,
                        "brand": f.stem.rsplit("_", 2)[0] if "_" in f.stem else f.stem,
                    })
            return {"reports": reports}
        if name == "list_methods":
            return {"methods": [
                {"id": m["id"], "name": m["name"],
                 "description": m.get("description", ""),
                 "skills": m.get("skills", []),
                 "input_fields": m.get("input_fields", [])}
                for m in self.runner.methods
            ]}

        skill = next((s for s in self.skills if s["id"] == name), None)
        if not skill:
            return {"error": f"未知的skill: {name}"}
        for key, value in args.items():
            ctx[key] = value
            if key == "product" and value:
                ctx["product_hint"] = f"用户特别关注产品/服务: {value}"
        missing = [inp for inp in skill.get("inputs", [])
                   if inp not in ctx and inp not in ("brand", "product", "product_hint")]
        if missing:
            return {"error": f"缺少必要的上下文数据: {missing}。请先执行前置skill。"}
        stype = skill["type"]
        runner = self.runner
        if stype == "single":
            result = await runner._exec_single(skill, ctx)
        elif stype == "parallel":
            result = await runner._exec_parallel(skill, ctx, None, 0)
        elif stype == "iterate":
            result = await runner._exec_iterate(skill, ctx, None, 0)
        elif stype == "iterate_parallel":
            result = await runner._exec_iterate_parallel(skill, ctx, None, 0)
        else:
            return {"error": f"不支持的skill类型: {stype}"}
        ctx[skill["output"]] = result
        summary = self._summarize(skill, result)
        return {"skill_id": name, "output": skill["output"], "summary": summary}

    @staticmethod
    def _summarize(skill: dict, result: Any) -> str:
        if isinstance(result, list):
            n = len(result)
            if skill["id"] == "step1":
                names = [r.get("product_name", "") for r in result if isinstance(r, dict)]
                return f"找到{n}个产品: {', '.join(names)}"
            if skill["id"] == "step3":
                names = [r.get("stage_name", "") for r in result if isinstance(r, dict)]
                return f"定义{n}个阶段: {', '.join(names)}"
            return f"返回{n}条记录"
        if isinstance(result, dict):
            if "lines" in result:
                return f"包含{len(result.get('lines', []))}条线的结果"
            if "clusters" in result:
                return f"归为{len(result.get('clusters', []))}个意图簇"
            return f"返回{len(result)}个字段"
        return "完成"
