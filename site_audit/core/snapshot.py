"""页面快照落盘：留存每次诊断的页面抽取内容与 LLM 原始响应。

用途（质量计划书v1·确定性标准）：复检基于快照重放、前后对比报告取证。
快照目录与 HTML/JSON 报告同名：{output_dir}/snapshots/{domain}_{ts}/
"""
from __future__ import annotations
import json
import logging
from pathlib import Path

from site_audit.core.ai_engine import _extract_content

logger = logging.getLogger("site_audit.snapshot")


def save_snapshot(output_dir, name: str, pages: list, ai_engine=None) -> str:
    """把本次诊断的快照落盘。失败不阻断诊断，返回空串。

    - pages.json：每个被检页面（首页+抽样内页）的 URL/final_url/status_code
      及结构化抽取内容（_extract_content 产出，即各 AI 检查实际看到的输入）
    - llm_raw.json：本次诊断全部 LLM 调用的 system/user prompt 与原始响应
      （仅配置了 AI 引擎时生成）
    """
    try:
        snap_dir = Path(output_dir) / "snapshots" / name
        snap_dir.mkdir(parents=True, exist_ok=True)

        pages_snap = []
        for p in pages:
            try:
                content = _extract_content(p)
                content["status_code"] = p.status_code
                pages_snap.append(content)
            except Exception as e:
                pages_snap.append({
                    "url": getattr(p, "final_url", ""),
                    "status_code": getattr(p, "status_code", 0),
                    "error": f"内容抽取失败: {e}",
                })
        (snap_dir / "pages.json").write_text(
            json.dumps(pages_snap, ensure_ascii=False, indent=2), encoding="utf-8")

        if ai_engine is not None:
            (snap_dir / "llm_raw.json").write_text(
                json.dumps(getattr(ai_engine, "raw_log", []),
                           ensure_ascii=False, indent=2),
                encoding="utf-8")

        return str(snap_dir)
    except Exception as e:
        logger.warning(f"快照落盘失败（不阻断诊断）: {e}")
        return ""
