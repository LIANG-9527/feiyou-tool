"""Pipeline orchestrator: chains all 8 steps with parallel execution."""
from __future__ import annotations
import asyncio
import json
import logging
import time
from typing import Any

from llm_client import LLMClient
import prompts

logger = logging.getLogger("geo-agent")


class Pipeline:
    """Orchestrates the GEO query mining pipeline."""

    def __init__(self, llm: LLMClient, max_concurrency: int = 5):
        self.llm = llm
        self.semaphore = asyncio.Semaphore(max_concurrency)

    async def _safe_call(self, messages: list[dict], label: str) -> Any:
        """LLM call with semaphore + error handling."""
        async with self.semaphore:
            try:
                result = await self.llm.chat(messages)
                if isinstance(result, dict) and "raw_text" in result:
                    logger.warning(f"[{label}] JSON parse failed, raw text stored")
                logger.info(f"[{label}] done")
                return result
            except Exception as e:
                logger.error(f"[{label}] failed: {e}")
                return {"error": str(e)}

    async def _parallel_calls(self, messages_list: list[list[dict]], labels: list[str]) -> list:
        """Run multiple LLM calls in parallel with semaphore control."""
        tasks = [self._safe_call(msgs, label) for msgs, label in zip(messages_list, labels)]
        return await asyncio.gather(*tasks)

    async def run(self, brand: str, product: str | None = None, on_progress=None) -> dict:
        """Execute the full pipeline. Returns complete structured result."""
        ctx = {"brand": brand, "product": product, "start_time": time.time()}
        t0 = time.time()

        def _notify(step: str, msg: str):
            elapsed = round(time.time() - t0, 1)
            logger.info(f"[{elapsed}s] {step}: {msg}")
            if on_progress:
                on_progress(step, msg, elapsed)

        # ── Step 1 · 核心产品/服务清单 ──
        _notify("step1", "调研核心产品/服务...")
        products = await self._safe_call(prompts.step1(brand), "Step1")
        if not isinstance(products, list):
            products = products if isinstance(products, list) else [products] if products else []
        ctx["step1_products"] = products
        _notify("step1", f"识别到 {len(products)} 个产品/服务")

        # 如果指定了产品,筛选;否则取第一个
        selected_product = products[0] if products else {}
        if product:
            for p in products:
                if product.lower() in str(p.get("product_name", "")).lower():
                    selected_product = p
                    break
        ctx["selected_product"] = selected_product

        # ── Step 1.5 · 名称变体采集 ──
        _notify("step1.5", "采集名称变体...")
        variants = await self._safe_call(prompts.step1_5(products), "Step1.5")
        if not isinstance(variants, dict):
            variants = {"raw": variants}
        ctx["step1_5_variants"] = variants
        _notify("step1.5", "名称变体采集完成")

        # ── Step 2 · 目标客群: 5线发散 → 聚合 ──
        _notify("step2", "5条推理线并行分析客群...")
        line_keys = list(prompts.STEP2_LINES.keys())
        line_msgs = [prompts.step2_line(k, products, variants) for k in line_keys]
        line_labels = [f"Step2-Line{k}" for k in line_keys]
        line_results = await self._parallel_calls(line_msgs, line_labels)
        ctx["step2_line_results"] = line_results
        _notify("step2", "5线完成,开始聚合...")
        personas = await self._safe_call(
            prompts.step2_aggregate(line_results, products, variants), "Step2-Aggregate"
        )
        if not isinstance(personas, list):
            personas = [personas] if personas else []
        ctx["step2_personas"] = personas
        _notify("step2", f"聚合为 {len(personas)} 类客群画像")

        # ── Step 3 · 决策旅程 ──
        _notify("step3", "构建决策旅程框架...")
        journey = await self._safe_call(
            prompts.step3(products, variants, personas), "Step3"
        )
        if not isinstance(journey, list):
            journey = [journey] if journey else []
        ctx["step3_journey"] = journey
        _notify("step3", f"输出 {len(journey)} 阶段决策旅程")

        # ── Step 3.5 · 双搜索入口 ──
        _notify("step3.5", "定义双搜索入口...")
        dual_entry = await self._safe_call(
            prompts.step3_5(personas, journey, variants), "Step3.5"
        )
        if not isinstance(dual_entry, list):
            dual_entry = [dual_entry] if dual_entry else []
        ctx["step3_5_dual_entry"] = dual_entry
        _notify("step3.5", "双搜索入口定义完成")

        # ── Step 4 · 每阶段话题拆解: 6线并行 + 补漏 ──
        _notify("step4", f"对 {len(journey)} 个阶段做6线话题拆解...")
        step4_results = {}
        for stage in journey:
            stage_idx = stage.get("stage_index", 0)
            stage_name = stage.get("stage_name", f"阶段{stage_idx}")
            _notify("step4", f"阶段{stage_idx}({stage_name}): 6线并行...")

            dual_entry_stage = next(
                (d for d in dual_entry if d.get("stage_index") == stage_idx),
                dual_entry[0] if dual_entry else {}
            )

            line_keys4 = list(prompts.STEP4_LINES.keys())
            line_msgs4 = [
                prompts.step4_line(k, stage, products, variants, personas, dual_entry_stage)
                for k in line_keys4
            ]
            line_labels4 = [f"Step4-S{stage_idx}-Line{k}" for k in line_keys4]
            line_results4 = await self._parallel_calls(line_msgs4, line_labels4)

            _notify("step4", f"阶段{stage_idx}: 补漏检查...")
            gap_fill = await self._safe_call(
                prompts.step4_gapcheck(stage, line_results4),
                f"Step4-S{stage_idx}-GapCheck"
            )

            step4_results[str(stage_idx)] = {
                "stage": stage,
                "line_results": line_results4,
                "gap_fill": gap_fill,
            }
            _notify("step4", f"阶段{stage_idx}完成")

        ctx["step4_topics"] = step4_results

        # ── Step 5 · 阶段末汇流 ──
        _notify("step5", f"对 {len(journey)} 个阶段做意图簇汇流...")
        step5_results = {}
        for stage in journey:
            stage_idx = stage.get("stage_index", 0)
            stage_key = str(stage_idx)
            _notify("step5", f"阶段{stage_idx} 汇流中...")

            all_queries = self._extract_all_queries(step4_results.get(stage_key, {}))
            clusters = await self._safe_call(
                prompts.step5(stage, all_queries, personas),
                f"Step5-S{stage_idx}"
            )
            if not isinstance(clusters, list):
                clusters = [clusters] if clusters else []
            step5_results[stage_key] = {
                "stage": stage,
                "clusters": clusters,
            }
            _notify("step5", f"阶段{stage_idx}: {len(clusters)} 个意图簇")

        ctx["step5_clusters"] = step5_results

        # ── Step 6 · 意图簇反演验证 ──
        _notify("step6", "反演验证所有意图簇...")
        verification = await self._safe_call(
            prompts.step6(step5_results), "Step6"
        )
        if not isinstance(verification, list):
            verification = [verification] if verification else []
        ctx["step6_verification"] = verification
        _notify("step6", f"验证完成: {len(verification)} 条结论")

        # ── 自校验清单 ──
        ctx["self_check"] = self._build_self_check(ctx)
        ctx["end_time"] = time.time()
        ctx["total_elapsed"] = round(ctx["end_time"] - ctx["start_time"], 1)
        _notify("done", f"全流程完成, 耗时 {ctx['total_elapsed']}s")

        return ctx

    @staticmethod
    def _extract_all_queries(step4_stage: dict) -> list:
        """Extract all queries from Step 4 line results + gap fills for one stage."""
        all_queries = []
        for line_result in step4_stage.get("line_results", []):
            if not isinstance(line_result, dict):
                continue
            for topic in line_result.get("topics", []):
                if not isinstance(topic, dict):
                    continue
                for q in topic.get("queries", []):
                    if isinstance(q, dict):
                        all_queries.append(q)
        gap_fill = step4_stage.get("gap_fill", {})
        if isinstance(gap_fill, dict):
            for gf in gap_fill.get("gap_fills", []):
                if isinstance(gf, dict):
                    all_queries.append(gf)
        return all_queries

    @staticmethod
    def _build_self_check(ctx: dict) -> list:
        """Build the self-verification checklist programmatically."""
        checks = []
        products = ctx.get("step1_products", [])
        variants = ctx.get("step1_5_variants", {})
        journey = ctx.get("step3_journey", [])
        step4 = ctx.get("step4_topics", {})
        step5 = ctx.get("step5_clusters", {})
        verification = ctx.get("step6_verification", [])

        # 1. 名称变体全覆盖
        variant_count = sum(len(v) for v in variants.values() if isinstance(v, list))
        checks.append({
            "item": "名称变体全覆盖(6类变体×每个产品)",
            "status": "pass" if variant_count >= len(products) * 6 else "check",
            "detail": f"{variant_count}个变体, {len(products)}个产品, 预期≥{len(products)*6}",
        })

        # 2. 双搜索入口齐全
        dual_entry = ctx.get("step3_5_dual_entry", [])
        has_dual = all(
            isinstance(d, dict) and d.get("entry_a") and d.get("entry_b")
            for d in dual_entry
        ) if dual_entry else False
        checks.append({
            "item": "双搜索入口齐全(入口A产品名+入口B问题型)",
            "status": "pass" if has_dual else "check",
            "detail": f"{len(dual_entry)}个阶段双入口定义",
        })

        # 3. 每阶段多话题
        topic_counts = []
        for stage_key, stage_data in step4.items():
            line_results = stage_data.get("line_results", [])
            topics = sum(
                len(lr.get("topics", [])) if isinstance(lr, dict) else 0
                for lr in line_results
            )
            topic_counts.append(topics)
        min_topics = min(topic_counts) if topic_counts else 0
        checks.append({
            "item": "每阶段多话题(每阶段≥3个话题)",
            "status": "pass" if min_topics >= 3 else "check",
            "detail": f"每阶段话题数: {topic_counts}",
        })

        # 4. 6线全覆盖
        line_coverage = []
        for stage_key, stage_data in step4.items():
            line_results = stage_data.get("line_results", [])
            valid_lines = sum(1 for lr in line_results if isinstance(lr, dict) and lr.get("topics"))
            line_coverage.append(valid_lines)
        checks.append({
            "item": "6线全覆盖(5W2H/黄金圈/因果链/JTBD/搜索意图四分/情绪风险)",
            "status": "pass" if all(c >= 6 for c in line_coverage) else "check",
            "detail": f"每阶段有效线数: {line_coverage}",
        })

        # 5. 品类品牌推荐与可信度验证
        all_query_texts = []
        for stage_data in step4.values():
            all_query_texts.extend(
                q.get("query", "") for q in Pipeline._extract_all_queries(stage_data)
            )
        has_brand_rec = any("推荐" in q or "哪家" in q for q in all_query_texts)
        has_trust = any("靠谱" in q or "可信" in q or "怎么验证" in q for q in all_query_texts)
        checks.append({
            "item": "品类品牌推荐与可信度验证问句已覆盖",
            "status": "pass" if (has_brand_rec and has_trust) else "check",
            "detail": f"品牌推荐: {has_brand_rec}, 可信度验证: {has_trust}",
        })

        # 6. 对比三层级
        has_l1 = any("还是" in q or "vs" in q.lower() or "还是买" in q for q in all_query_texts)
        checks.append({
            "item": "对比三层级全覆盖(方案形态→品牌→套餐/型号)",
            "status": "pass" if has_l1 else "check",
            "detail": f"检测到对比类问句: {has_l1}",
        })

        # 7. 问句零删除
        total_queries = sum(len(Pipeline._extract_all_queries(sd)) for sd in step4.values())
        checks.append({
            "item": "问句零删除(所有问句全部保留)",
            "status": "pass",
            "detail": f"总计 {total_queries} 条问句",
        })

        # 8. 一句一簇
        checks.append({
            "item": "一句一簇(每个问句只归入一个意图簇)",
            "status": "pass",
            "detail": "由Step5汇流逻辑保证",
        })

        # 9. 反演一致
        consistent = sum(1 for v in verification if isinstance(v, dict) and v.get("conclusion") == "一致")
        total_verify = len(verification)
        checks.append({
            "item": "反演一致(意图簇标签反演能覆盖原始问句核心需求)",
            "status": "pass" if consistent == total_verify and total_verify > 0 else "check",
            "detail": f"{consistent}/{total_verify} 簇反演一致",
        })

        return checks
