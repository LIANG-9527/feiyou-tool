"""可变数据路径唯一来源：全部落在 var/ 下（2026-09-16 目录重组）。

根目录=代码/配置/文档（发版可整体覆盖服务器）；var/=数据（服务器资产，
上传/发版永远不碰）。改任何数据落盘位置只动本文件。
"""
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
VAR_DIR = BASE_DIR / "var"

OUTPUT_DIR = VAR_DIR / "output"          # 报告与快照
DATA_DIR = VAR_DIR / "data"              # 企业缓存库 company_info.db
TASKS_DIR = VAR_DIR / "tasks"            # 任务记录 json
API_CACHE_DIR = VAR_DIR / "api_cache"    # 工商接口响应缓存
LOG_DIR = VAR_DIR / "log"                # 服务日志

for _d in (OUTPUT_DIR, DATA_DIR, TASKS_DIR, API_CACHE_DIR, LOG_DIR):
    _d.mkdir(parents=True, exist_ok=True)
