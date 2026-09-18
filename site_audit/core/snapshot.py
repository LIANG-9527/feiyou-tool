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
      及结构化抽取内容（_extract_content 产出，即各 AI 检查实际看到的输入）；
      顶层带 note 说明正文为有损摘要，防止调试时误当页面全文
    - llm_raw.json：本次诊断全部 LLM 调用的 system/user prompt 与原始响应
      （仅配置了 AI 引擎时生成）
    - search_raw.json：外部数据通道原始查询响应（百科品牌实体词条页正文片段等），
      供重放取证与人工复核（有外部通道查询时生成）
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
            json.dumps({
                "note": "说明：正文(body_text)为有损摘要——仅收录正文段落、上限6000字，"
                        "并剔除页眉/页脚/导航/侧栏等模板区，不代表页面全文；"
                        "核验页脚或整站内容请以现场抓取为准。",
                "pages": pages_snap,
            }, ensure_ascii=False, indent=2), encoding="utf-8")

        if ai_engine is not None:
            (snap_dir / "llm_raw.json").write_text(
                json.dumps(getattr(ai_engine, "raw_log", []),
                           ensure_ascii=False, indent=2),
                encoding="utf-8")
            # search_raw.json：外部数据通道（百科品牌实体等）的原始查询响应留痕，
            # 词条页正文片段+搜索原始结果，供重放取证与人工复核
            search_raw = getattr(ai_engine, "search_raw", [])
            if search_raw:
                (snap_dir / "search_raw.json").write_text(
                    json.dumps(search_raw, ensure_ascii=False, indent=2),
                    encoding="utf-8")

        # 快照哈希：sha256(pages.json + llm_raw.json + search_raw.json)，写入 snapshot_hash.txt，
        # 供前后对比页凭证字段核验快照未被篡改
        import hashlib
        h = hashlib.sha256()
        for fname in ("pages.json", "llm_raw.json", "search_raw.json"):
            fp = snap_dir / fname
            if fp.exists():
                h.update(fp.read_bytes())
        (snap_dir / "snapshot_hash.txt").write_text(h.hexdigest(), encoding="utf-8")

        return str(snap_dir)
    except Exception as e:
        logger.warning(f"快照落盘失败（不阻断诊断）: {e}")
        return ""
