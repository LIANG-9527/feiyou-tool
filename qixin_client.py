#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""启信慧眼工商数据源（qixin.com）——惊贤索接口的备用/替代供应商。

对外暴露与 BusinessAPIClient 同名的四个方法（search_company /
get_company_detail_by_name / get_company_detail_by_credit_code /
get_shareholders / get_branch_orgs），返回惊贤索同构形状，
下游（组织架构图/画像管线/名称核查）零改动。
启用：环境变量 QIXIN_APPKEY / QIXIN_SECRET_KEY 存在且 BUSINESS_PROVIDER=qixin。
注意：启信慧眼后台需配置出口 IP 白名单，否则报 104。
"""
import hashlib
import json
import os
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

import httpx

from business_api_client import BusinessAPIError, norm_name

BASE_URL = "https://api.qixin.com"
DETAIL_PATH = "/APIService/enterprise/getDetailAndContactByName"
CACHE_DIR = Path(os.environ.get("API_CACHE_DIR") or "var/api_cache")
CACHE_TTL = 24 * 3600


def _now_ms() -> str:
    return str(int(time.time() * 1000))


def _sign(appkey: str, ts: str, secret: str) -> str:
    return hashlib.md5(f"{appkey}{ts}{secret}".encode()).hexdigest().lower()


class QixinClient:
    """启信慧眼客户端：一个接口（按名称/信用代码查详情+股东+分支）覆盖
    惊贤索的 search_company / detail / shareholders / branches 四个方法。"""

    def __init__(self, appkey: str = None, secret_key: str = None):
        self.appkey = appkey or os.environ.get("QIXIN_APPKEY", "")
        self.secret_key = secret_key or os.environ.get("QIXIN_SECRET_KEY", "")
        self.cache_dir = CACHE_DIR
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self._client: Optional[httpx.AsyncClient] = None
        self._last_detail: Dict[str, Any] = {}   # qixin_id → 原始 data（shareholders/branches 复用）

    # ── 基础设施 ──
    def _headers(self) -> Dict[str, str]:
        ts = _now_ms()
        return {"Auth-Version": "2.0", "appkey": self.appkey,
                "timestamp": ts, "sign": _sign(self.appkey, ts, self.secret_key)}

    def _cache_path(self, kind: str, key: str) -> Path:
        import re as _re
        safe = _re.sub(r"[\s（）()：:，,。·、]", "", key)
        return self.cache_dir / f"qx_{kind}_{safe}.json"

    def _cache_get(self, kind: str, key: str) -> Optional[dict]:
        p = self._cache_path(kind, key)
        try:
            if p.exists() and time.time() - p.stat().st_mtime < CACHE_TTL:
                return json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            pass
        return None

    def _cache_set(self, kind: str, key: str, data: dict):
        try:
            self._cache_path(kind, key).write_text(
                json.dumps(data, ensure_ascii=False), encoding="utf-8")
        except Exception:
            pass

    async def _get(self, keyword: str) -> Dict[str, Any]:
        """调启信慧眼详情接口，返回原始 data 对象；业务错误抛 BusinessAPIError。"""
        if not self.appkey or not self.secret_key:
            raise BusinessAPIError("启信慧眼密钥未配置（QIXIN_APPKEY/QIXIN_SECRET_KEY）")
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=30)
        r = await self._client.get(
            BASE_URL + DETAIL_PATH, params={"keyword": keyword},
            headers=self._headers())
        r.raise_for_status()
        body = r.json()
        status = str(body.get("status") or "")
        if status == "200":
            return body.get("data") or {}
        if status == "201":
            return {}   # 查询无结果 = 正常空
        # 213/216/102=额度类，214/101=鉴权类，104=IP白名单……原样带码抛出
        raise BusinessAPIError(int(status), body.get("message") or "未知错误")

    # ── 内部映射：启信慧眼 data → 惊贤索同构字段 ──
    @staticmethod
    def _map_detail(d: Dict[str, Any]) -> Dict[str, Any]:
        contact = d.get("contact") or {}
        staff = d.get("collegues_num")
        return {
            "id": d.get("id"),
            "companyName": d.get("name"), "name": d.get("name"),
            "format_name": d.get("format_name"),
            "creditNumber": d.get("credit_no"), "creditNo": d.get("credit_no"),
            "credit_code": d.get("credit_no"),
            "juridicalPerson": d.get("oper_name"), "oper_name": d.get("oper_name"),
            "businessStatus": d.get("new_status") or d.get("status"),
            "status": d.get("new_status") or d.get("status"),
            "companyType": d.get("econ_kind"), "econ_kind": d.get("econ_kind"),
            "establishTime": d.get("start_date"), "start_date": d.get("start_date"),
            "founded": d.get("start_date"),
            "registCapi": d.get("regist_capi"), "regist_capi": d.get("regist_capi"),
            "address": contact.get("address") or d.get("address"),
            "telephone": contact.get("telephone"),
            "email": contact.get("email"),
            "scope": d.get("scope"),
            "org_no": d.get("org_no"), "belong_org": d.get("belong_org"),
            "term_start": d.get("term_start"), "term_end": d.get("term_end"),
            "history_names": d.get("history_names") or [],
            "is_quoted": d.get("is_quoted"),
            "staffNumber": staff if (staff and str(staff).strip().isdigit()) else None,
            "employees": d.get("employees") or [],
            "partners": d.get("partners") or [],
            "branches": d.get("branches") or [],
            "websites": d.get("websites") or [],
            "changerecords": d.get("changerecords") or [],
            "abnormal_items": d.get("abnormal_items") or [],
        }

    @staticmethod
    def _map_shareholders(partners: list) -> Dict[str, Any]:
        recs = [{"name": p.get("name"), "shareholdingRatio": p.get("stock_percent"),
                 "type": ("个人" if "自然人" in str(p.get("stock_type") or "") else "公司"),
                 "stock_type": p.get("stock_type")}
                for p in (partners or []) if isinstance(p, dict) and p.get("name")]
        return {"records": recs}

    @staticmethod
    def _map_branches(branches: list) -> Dict[str, Any]:
        recs = [{"name": b.get("name"), "id": b.get("id") or b.get("name"),
                 "companyStatus": None, "judicalPerson": None}
                for b in (branches or []) if isinstance(b, dict) and b.get("name")]
        return {"records": recs}

    # ── 对外：与惊贤索同形的四个方法 ──
    async def search_company(self, keywords: str, page_size: int = 5,
                             use_cache: bool = True) -> Dict[str, Any]:
        """按关键词查企业：启信慧眼按名称/信用代码精确匹配，返回 0 或 1 条主记录。"""
        key = norm_name(keywords)
        if use_cache:
            cached = self._cache_get("search", key)
            if cached is not None:
                return cached
        data = await self._get(keywords)
        if not data:
            return {"records": [], "totalRecords": 0}
        mapped = self._map_detail(data)
        out = {"records": [{"companyName": mapped.get("companyName"),
                            "id": mapped.get("id"),
                            "creditNumber": mapped.get("creditNumber"),
                            "juridicalPerson": mapped.get("juridicalPerson")}],
               "totalRecords": 1}
        if use_cache:
            self._cache_set("search", key, out)
        return out

    async def get_company_detail_by_name(self, company_name: str,
                                         use_cache: bool = True) -> Dict[str, Any]:
        key = norm_name(company_name)
        if use_cache:
            cached = self._cache_get("detail", key)
            if cached is not None:
                return cached
        data = await self._get(company_name)
        if not data:
            return {}
        mapped = self._map_detail(data)
        self._cache_set("detail", key, mapped)
        if mapped.get("id"):
            self._last_detail[mapped["id"]] = data
        return mapped

    async def get_company_detail_by_credit_code(self, credit_code: str,
                                                use_cache: bool = True) -> Dict[str, Any]:
        return await self.get_company_detail_by_name(credit_code, use_cache)

    async def get_shareholders(self, qixin_id: str, **kw) -> Dict[str, Any]:
        raw = self._last_detail.get(qixin_id) or {}
        return self._map_shareholders(raw.get("partners"))

    async def get_branch_orgs(self, qixin_id: str, **kw) -> Dict[str, Any]:
        raw = self._last_detail.get(qixin_id) or {}
        return self._map_branches(raw.get("branches"))

    async def close(self):
        if self._client:
            await self._client.aclose()
            self._client = None


# ── 供应商开关：环境变量决定用启信慧眼还是惊贤索（默认惊贤索）──
_instance: Optional[QixinClient] = None


def get_qixin_client() -> QixinClient:
    global _instance
    if _instance is None:
        _instance = QixinClient()
    return _instance


def qixin_enabled() -> bool:
    """启信慧眼密钥存在且 BUSINESS_PROVIDER=qixin 时启用。"""
    return bool(os.environ.get("QIXIN_APPKEY")) and \
        os.environ.get("BUSINESS_PROVIDER", "").lower() == "qixin"
