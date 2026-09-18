"""工商数据 API 客户端（惊贤所 jingxiansuo.com）

鉴权方式：双请求头
- Authorization = Base64(sassApi:appId:timestamp)
- AuthorizationMD5 = MD5(sassApi + secret + timestamp).upper()

缓存策略：
- LAYER1 基础字段按企业名称/信用代码缓存 30 天
- 字段级 TTL：基础信息30天，股东/人员30天，风险信息3天
- 缓存文件存到 business_api_cache/ 目录

使用方式：
    from business_api_client import BusinessAPIClient
    client = BusinessAPIClient()
    # 按名称查工商详情
    data = client.get_company_detail_by_name("深圳依时货拉拉科技有限公司")
    # 按信用代码查
    data = client.get_company_detail_by_credit_code("91440300MA5F...")
"""
from __future__ import annotations
import hashlib
import base64
import json
import time
import logging
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional, Dict, Any

import httpx

logger = logging.getLogger("geo-agent.business-api")

# ---------------------------------------------------------------------------
# 配置
# ---------------------------------------------------------------------------

APP_ID = "qudao_env_183"
SECRET = "hwmCoQsxmxTC3Jg5HFFQrLNUmZNTA2sHX"
SASS_API = "sassApi"  # 固定值
BASE_URL = "https://api3.jingxiansuo.com"
API_TIMEOUT = 15  # 秒

# 缓存配置
from paths import API_CACHE_DIR as CACHE_DIR
CACHE_TTL_BASIC = 30 * 24 * 3600  # 基础信息缓存30天
CACHE_TTL_RISK = 3 * 24 * 3600     # 风险信息缓存3天
CACHE_TTL_SEARCH = 24 * 3600       # 模糊搜索缓存1天（按次计费接口，防重复付费）


def norm_name(name: str) -> str:
    """企业名送查前归一：工商数据源按注册档案原文精确匹配，括号存半角，
    而中文输入法常打全角括号/全角字母数字（实测「考拉出海（广州）…」查无、
    半角命中）。NFKC 把全角转半角并去首尾空白，查询与缓存键同走此函数保持一致。"""
    import unicodedata
    return unicodedata.normalize("NFKC", str(name or "")).strip()

# 状态码
STATUS_OK = 200
STATUS_PARAM_ERROR = 300
STATUS_AUTH_FAILED = 401
STATUS_TOKEN_EXPIRED = 402
STATUS_QUOTA_EXCEEDED = 403
STATUS_NOT_FOUND = 410


class BusinessAPIError(Exception):
    """工商 API 调用错误。"""

    def __init__(self, status_code: int, message: str):
        self.status_code = status_code
        self.message = message
        super().__init__(f"Business API error [{status_code}]: {message}")


class BusinessAPIClient:
    """工商数据 API 客户端，带鉴权、缓存、重试。"""

    def __init__(self, cache_dir: str = None, enable_cache: bool = True):
        self.app_id = APP_ID
        self.secret = SECRET
        self.sass_api = SASS_API
        self.base_url = BASE_URL
        self.enable_cache = enable_cache
        self.cache_dir = Path(cache_dir) if cache_dir else CACHE_DIR
        if self.enable_cache:
            self.cache_dir.mkdir(parents=True, exist_ok=True)
        self._client: Optional[httpx.AsyncClient] = None

    # -----------------------------------------------------------------------
    # 鉴权
    # -----------------------------------------------------------------------

    def _get_headers(self) -> Dict[str, str]:
        """生成鉴权请求头。

        Authorization = Base64(sassApi:appId:timestamp)
        AuthorizationMD5 = MD5(sassApi + secret + timestamp).upper()
        """
        ts = datetime.now().strftime("%Y%m%d%H%M%S")
        auth_str = f"{self.sass_api}:{self.app_id}:{ts}"
        auth_md5_str = f"{self.sass_api}{self.secret}{ts}"
        return {
            "Authorization": base64.b64encode(auth_str.encode()).decode(),
            "AuthorizationMD5": hashlib.md5(auth_md5_str.encode()).hexdigest().upper(),
            "Content-Type": "application/json",
        }

    # -----------------------------------------------------------------------
    # 缓存
    # -----------------------------------------------------------------------

    def _cache_key(self, prefix: str, identifier: str) -> str:
        """生成缓存文件名（安全处理特殊字符）。"""
        safe = identifier.replace("/", "_").replace("\\", "_").replace(":", "_")
        return f"{prefix}_{safe}.json"

    def _get_cache(self, prefix: str, identifier: str, ttl: int) -> Optional[Dict[str, Any]]:
        """读取缓存，过期返回 None。"""
        if not self.enable_cache:
            return None
        cache_file = self.cache_dir / self._cache_key(prefix, identifier)
        if not cache_file.exists():
            return None
        try:
            with open(cache_file, "r", encoding="utf-8") as f:
                data = json.load(f)
            cached_at = data.get("_cached_at", 0)
            if time.time() - cached_at > ttl:
                return None  # 过期
            return data.get("data")
        except Exception as e:
            logger.debug(f"Cache read failed: {e}")
            return None

    def _set_cache(self, prefix: str, identifier: str, data: Dict[str, Any]):
        """写入缓存。"""
        if not self.enable_cache:
            return
        cache_file = self.cache_dir / self._cache_key(prefix, identifier)
        try:
            with open(cache_file, "w", encoding="utf-8") as f:
                json.dump({"_cached_at": time.time(), "data": data}, f, ensure_ascii=False, indent=2)
        except Exception as e:
            logger.debug(f"Cache write failed: {e}")

    # -----------------------------------------------------------------------
    # HTTP 请求
    # -----------------------------------------------------------------------

    async def _request(self, method: str, path: str, params: dict = None,
                       json_body: dict = None) -> Dict[str, Any]:
        """发送 API 请求，处理鉴权和错误。

        Raises:
            BusinessAPIError: API 返回错误状态码
        """
        url = f"{self.base_url}{path}"
        headers = self._get_headers()

        async with httpx.AsyncClient(timeout=API_TIMEOUT, follow_redirects=True) as client:
            if method.upper() == "GET":
                resp = await client.get(url, headers=headers, params=params)
            elif method.upper() == "POST":
                resp = await client.post(url, headers=headers, params=params, json=json_body)
            else:
                raise ValueError(f"Unsupported method: {method}")

        # 解析响应
        try:
            result = resp.json()
        except Exception:
            result = {"code": resp.status_code, "message": resp.text[:500]}

        status_code = result.get("code", resp.status_code)

        if status_code == STATUS_OK:
            return result.get("data") or {}
        elif status_code == STATUS_NOT_FOUND:
            # 企业不存在，返回空 dict（不抛异常，上层可以判断）
            logger.info(f"Business API: company not found ({path})")
            return {}
        elif status_code == STATUS_AUTH_FAILED:
            raise BusinessAPIError(status_code, "鉴权失败，请检查 appId/secret")
        elif status_code == STATUS_TOKEN_EXPIRED:
            raise BusinessAPIError(status_code, "token 已过期")
        elif status_code == STATUS_QUOTA_EXCEEDED:
            raise BusinessAPIError(status_code, "用量不足，请充值")
        elif status_code == STATUS_PARAM_ERROR:
            raise BusinessAPIError(status_code, f"参数错误: {result.get('message', '')}")
        else:
            raise BusinessAPIError(status_code, f"未知错误: {result.get('message', resp.text[:200])}")

    # -----------------------------------------------------------------------
    # 常用接口
    # -----------------------------------------------------------------------

    async def get_company_detail_by_name(self, company_name: str,
                                          use_cache: bool = True) -> Dict[str, Any]:
        """按企业名称查工商详情。

        Args:
            company_name: 企业全称（精准匹配，带括号时用英文半角）
            use_cache: 是否使用缓存

        Returns:
            工商详情数据，企业不存在返回 {}
        """
        company_name = norm_name(company_name)
        if use_cache:
            cached = self._get_cache("detail", company_name, CACHE_TTL_BASIC)
            if cached is not None:
                return cached

        path = f"/DataService/api/v3/company/detail/{company_name}"
        params = {"queryType": 1}  # 1=企业名称
        data = await self._request("GET", path, params=params)

        if use_cache and data:
            self._set_cache("detail", company_name, data)

        return data

    async def get_company_detail_by_credit_code(self, credit_code: str,
                                                  use_cache: bool = True) -> Dict[str, Any]:
        """按统一社会信用代码查工商详情。"""
        if use_cache:
            cached = self._get_cache("detail", credit_code, CACHE_TTL_BASIC)
            if cached is not None:
                return cached

        path = f"/DataService/api/v3/company/detail/{credit_code}"
        params = {"queryType": 2}  # 2=统一社会信用代码
        data = await self._request("GET", path, params=params)

        if use_cache and data:
            self._set_cache("detail", credit_code, data)

        return data

    async def search_company(self, keywords: str, page_index: int = 1,
                             page_size: int = 20,
                             use_cache: bool = True) -> Dict[str, Any]:
        """模糊搜索企业（用于名称不确认时的候选列表）。

        Args:
            keywords: 搜索关键词
            page_index: 页码
            page_size: 每页数量
            use_cache: 1 天短缓存（接口按次计费，绑定重试/批量场景不重复付费）

        Returns:
            搜索结果，包含企业列表
        """
        key = norm_name(keywords)
        if use_cache:
            cached = self._get_cache("search", key, CACHE_TTL_SEARCH)
            if cached is not None:
                return cached
        path = "/DataService/api/v3/company/list/filter"
        body = {"keywords": key, "pageIndex": page_index, "pageSize": page_size}
        data = await self._request("POST", path, json_body=body)
        if use_cache and data:
            self._set_cache("search", key, data)
        return data

    async def get_shareholders(self, company_id: str, page_index: int = 1,
                                page_size: int = 20, use_cache: bool = True) -> Dict[str, Any]:
        """查股东信息（需要企业ID，24位hex）。"""
        cache_key = f"{company_id}_p{page_index}"
        if use_cache:
            cached = self._get_cache("shareholder", cache_key, CACHE_TTL_BASIC)
            if cached is not None:
                return cached

        path = f"/DataService/api/v3/company/shareholder/{company_id}"
        params = {"pageIndex": page_index, "pageSize": page_size}
        data = await self._request("GET", path, params=params)

        if use_cache and data:
            self._set_cache("shareholder", cache_key, data)

        return data

    async def get_key_persons(self, company_id: str, page_index: int = 1,
                               page_size: int = 20, is_history: int = 0,
                               use_cache: bool = True) -> Dict[str, Any]:
        """查主要人员（需要企业ID）。"""
        cache_key = f"{company_id}_p{page_index}_h{is_history}"
        if use_cache:
            cached = self._get_cache("keyperson", cache_key, CACHE_TTL_BASIC)
            if cached is not None:
                return cached

        path = f"/DataService/api/v3/company/keyPerson/{company_id}"
        params = {"pageIndex": page_index, "pageSize": page_size, "isHistory": is_history}
        data = await self._request("GET", path, params=params)

        if use_cache and data:
            self._set_cache("keyperson", cache_key, data)

        return data

    async def get_judicial_count(self, company_id: str,
                                  use_cache: bool = True) -> Dict[str, Any]:
        """查司法信息计数（需要企业ID），缓存3天。"""
        if use_cache:
            cached = self._get_cache("judicial_count", company_id, CACHE_TTL_RISK)
            if cached is not None:
                return cached

        path = f"/DataService/api/v3/company/judicial/count/{company_id}"
        data = await self._request("GET", path)

        if use_cache and data:
            self._set_cache("judicial_count", company_id, data)

        return data

    async def get_intellectual_count(self, company_id: str,
                                       use_cache: bool = True) -> Dict[str, Any]:
        """查知识产权计数（需要企业ID），缓存30天。"""
        if use_cache:
            cached = self._get_cache("ip_count", company_id, CACHE_TTL_BASIC)
            if cached is not None:
                return cached

        path = f"/DataService/api/v3/company/intellectual/count/{company_id}"
        data = await self._request("GET", path)

        if use_cache and data:
            self._set_cache("ip_count", company_id, data)

        return data

    async def get_branch_orgs(self, company_id: str, page_index: int = 0,
                                page_size: int = 20, use_cache: bool = True) -> Dict[str, Any]:
        """查分支机构信息（需要企业ID），缓存30天。

        Args:
            company_id: 企业ID（24位hex）
            page_index: 页码，默认0
            page_size: 每页数量，默认20

        Returns:
            分支机构数据，包含 records 列表，每个分支含：
            - id: 分支机构ID
            - name: 分支机构企业名称
            - judicalPerson: 负责人
            - companyStatus: 经营状态
            - regitDate: 成立日期（时间戳，Long）
        """
        cache_key = f"{company_id}_p{page_index}"
        if use_cache:
            cached = self._get_cache("branch_org", cache_key, CACHE_TTL_BASIC)
            if cached is not None:
                return cached

        path = f"/DataService/api/v3/company/branchOrgList/{company_id}"
        params = {"pageIndex": page_index, "pageSize": page_size}
        data = await self._request("GET", path, params=params)

        if use_cache and data:
            self._set_cache("branch_org", cache_key, data)

        return data

    # -----------------------------------------------------------------------
    # 便捷方法：从工商详情中提取 LAYER1 字段
    # -----------------------------------------------------------------------

    # 工商详情接口原始字段 → 档案字段全量映射（API 给什么存什么；与既有 LAYER1 字段同名合并）
    RAW_FIELD_MAP = {
        "companyName": "full_name", "creditNumber": "credit_code", "licenseNumber": "license_number",
        "orgCode": "org_code", "juridicalPerson": "legal_person", "establishTime": "founded",
        "companyType": "company_type", "businessStatus": "status", "address": "address",
        "regitAddress": "regit_address", "province": "province", "belongCity": "city",
        "district": "district", "companyIndustry": "industry", "companyIndustryCode": "industry_code",
        "staffNumber": "staff_number", "socialSecurityNum": "social_security_num",
        "socialSecurityFrom": "social_security_from", "registeredCapital": "reg_capital",
        "contributedCapital": "contributed_capital", "annualTurnover": "annual_turnover",
        "reportTaxFrom": "report_tax_from", "reportTaxTotal": "report_tax_total",
        "approveDate": "approve_date", "revokeDate": "revoke_date", "businessPeriod": "business_period",
        "issuingAuthority": "issuing_authority", "addressLastChangeDate": "address_last_change",
        "businessScope": "business_scope", "products": "products", "companyProfile": "company_profile",
        "historyNames": "history_names", "id": "api_id",
    }

    @staticmethod
    def extract_raw_fields(detail: Dict[str, Any]) -> Dict[str, Any]:
        """工商详情接口返回的全量字段（按映射改名，空值丢弃）。
        曾用名 history_names 等列表原样保留。"""
        out = {}
        if not isinstance(detail, dict):
            return out
        for k, v in detail.items():
            mapped = BusinessAPIClient.RAW_FIELD_MAP.get(k)
            if not mapped or v in (None, "", [], {}, "None"):
                continue
            out[mapped] = v if isinstance(v, list) else str(v).strip()
        return out

    @staticmethod
    def extract_layer1_fields(detail: Dict[str, Any]) -> Dict[str, str]:
        """从工商详情 API 返回中提取 LAYER1 字段。

        兼容惊贤所 API 的实际返回字段名（creditNumber/juridicalPerson/establishTime等）。
        """
        if not detail:
            return {}

        result = {}

        # 公司全称
        for key in ["companyName", "name", "entName", "fullName", "ent_name"]:
            if detail.get(key):
                result["full_name"] = str(detail[key])
                break

        # 统一社会信用代码
        for key in ["creditNumber", "creditCode", "unifiedSocialCreditCode", "creditNo", "uscc", "credit_code"]:
            if detail.get(key):
                result["credit_code"] = str(detail[key])
                break

        # 法定代表人
        for key in ["juridicalPerson", "legalPerson", "legalRepresentative", "frName", "operName", "legalPersonName", "fr_name"]:
            if detail.get(key):
                result["legal_person"] = str(detail[key])
                break

        # 成立时间
        for key in ["establishTime", "establishDate", "foundDate", "estiblishTime", "createTime", "openTime", "establish_time"]:
            if detail.get(key):
                val = str(detail[key])
                # 提取年月
                if len(val) >= 7:
                    result["founded"] = val[:7]
                else:
                    result["founded"] = val
                break

        # 注册地址（优先用 regitAddress，再用 address）
        for key in ["regitAddress", "regAddress", "registeredAddress", "address", "regAddr", "domicile"]:
            if detail.get(key):
                result["address"] = str(detail[key])
                break

        # 上市状态（根据公司类型判断）
        for key in ["companyType", "entType", "type", "company_type"]:
            if detail.get(key):
                ctype = str(detail[key])
                if "上市" in ctype:
                    result["listed"] = "上市"
                elif "股份" in ctype:
                    result["listed"] = "非上市（股份公司）"
                else:
                    result["listed"] = "未上市"
                break

        # 员工人数（优先用 staffNumber，再用参保人数）
        for key in ["staffNumber", "socialStaffNum", "insuredCount", "employeeCount", "staffNum", "staff_number"]:
            if detail.get(key):
                result["employees"] = f"{detail[key]}人"
                break
        # 如果有参保人数，补充说明
        if detail.get("socialSecurityNum") and "employees" not in result:
            result["employees"] = f"{detail['socialSecurityNum']}人（参保人数）"

        # 所属行业（优先用 companyIndustry，再用经营范围）
        for key in ["companyIndustry", "industry", "industryName", "industryFirst", "company_industry"]:
            if detail.get(key):
                result["industry"] = str(detail[key])[:100]
                break
        # 如果没有行业字段，用经营范围的前几个字
        if "industry" not in result and detail.get("businessScope"):
            result["industry"] = str(detail["businessScope"])[:50]

        # 注册资本（辅助字段，不影响LAYER1核验）
        for key in ["registeredCapital", "regCapital", "capital"]:
            if detail.get(key):
                result["_reg_capital"] = str(detail[key])
                break

        return result


# 全局单例
_client: Optional[BusinessAPIClient] = None


def get_business_api_client() -> BusinessAPIClient:
    """获取工商 API 客户端单例。"""
    global _client
    if _client is None:
        _client = BusinessAPIClient()
    return _client
