from __future__ import annotations
import asyncio
import json
import re
import logging
import httpx

logger = logging.getLogger("geo-agent")


class LLMClient:
    """Model-agnostic async LLM client (OpenAI-compatible chat completions)."""

    def __init__(self, base_url: str, api_key: str, model: str,
                 temperature: float = 0.7, max_tokens: int = 4096,
                 timeout: int = 120, max_retries: int = 3,
                 enable_search: bool = False, enable_thinking: bool = False,
                 search_engine: list | None = None):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.model = model
        self.temperature = temperature
        self.max_tokens = max_tokens
        self.timeout = timeout
        self.max_retries = max_retries
        self.enable_search = enable_search
        self.search_engine = search_engine  # 联网检索优先引擎（如 ["baidu"]），None=跟随平台默认
        self.enable_thinking = enable_thinking
        # token 用量累计（与官网诊断 AIEngine 同口径字段，直接喂 estimate_cost_cny）
        # search_calls=联网搜索计费次数（开启enable_search的成功调用每次触发1次检索）
        # failed_attempts=失败/超时尝试次数（服务端可能已计费但响应无usage，费用无法计量，只记数供对账）
        self.token_usage = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0,
                            "prompt_cache_hit_tokens": 0, "prompt_cache_miss_tokens": 0, "calls": 0,
                            "search_calls": 0, "failed_attempts": 0}

    def _accumulate_usage(self, usage: dict):
        """累加一次成功调用的 token 用量（百炼缓存命中在 prompt_tokens_details.cached_tokens）。"""
        if not usage:
            return
        self.token_usage["prompt_tokens"] += usage.get("prompt_tokens", 0)
        self.token_usage["completion_tokens"] += usage.get("completion_tokens", 0)
        self.token_usage["total_tokens"] += usage.get("total_tokens", 0)
        hit = (usage.get("prompt_cache_hit_tokens")
               or (usage.get("prompt_tokens_details") or {}).get("cached_tokens", 0) or 0)
        miss = max(usage.get("prompt_tokens", 0) - hit, 0)
        self.token_usage["prompt_cache_hit_tokens"] += hit
        self.token_usage["prompt_cache_miss_tokens"] += miss
        self.token_usage["calls"] += 1

    def _payload_extra(self, thinking: bool | None = None,
                       enable_search: bool | None = None) -> dict:
        """联网搜索 + 深度思考参数（仅百炼/通义支持）

        thinking / enable_search 显式传入时覆盖实例默认。
        百炼的 qwen3.8 系列**默认开思考**，关闭必须显式传 enable_thinking=false。
        enable_search=False 用于"已有全文上下文、不需要LLM再联网搜索"的场景，
        避免重复搜索和LLM忽略全文上下文。"""
        payload = {}
        eff_search = enable_search if enable_search is not None else self.enable_search
        if eff_search:
            payload["enable_search"] = True
            if self.search_engine:
                payload["search_options"] = {"search_engine": self.search_engine}
        # 思考开关与联网无关，必须始终显式下发：qwen3.8 默认开思考，
        # 漏下发会让链上单调用退化到 150~345s（2026-09-17 提速修复实测）。
        payload["enable_thinking"] = bool(thinking if thinking is not None else self.enable_thinking)
        return payload

    async def chat(self, messages: list[dict], expect_json: bool = True,
                   thinking: bool | None = None,
                   enable_search: bool | None = None) -> str | dict | list:
        """Send chat messages and return the response text or parsed JSON.

        enable_search 显式传入时覆盖实例默认（False=强制关闭联网搜索）。"""
        payload = {
            "model": self.model,
            "messages": messages,
            "temperature": self.temperature,
            "max_tokens": self.max_tokens,
            **self._payload_extra(thinking, enable_search),
        }
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        url = f"{self.base_url}/chat/completions"

        for attempt in range(1, self.max_retries + 1):
            try:
                async with httpx.AsyncClient(timeout=self.timeout) as client:
                    resp = await client.post(url, json=payload, headers=headers)
                    resp.raise_for_status()
                    data = resp.json()
                    self._accumulate_usage(data.get("usage") or {})
                    if payload.get("enable_search"):
                        self.token_usage["search_calls"] += 1
                    text = data["choices"][0]["message"]["content"]
                    if expect_json:
                        return self._extract_json(text)
                    return text
            except Exception as e:
                self.token_usage["failed_attempts"] += 1
                logger.warning(f"LLM call attempt {attempt}/{self.max_retries} failed: {e}")
                if attempt < self.max_retries:
                    await asyncio.sleep(2 ** attempt)
                else:
                    raise

    async def chat_with_tools(self, messages: list[dict], tools: list = None,
                              thinking: bool | None = None,
                              enable_search: bool | None = None) -> dict:
        """Call LLM with function-calling support. Returns the full message object."""
        payload = {
            "model": self.model,
            "messages": messages,
            "temperature": self.temperature,
            "max_tokens": self.max_tokens,
            **self._payload_extra(thinking, enable_search),
        }
        if tools:
            payload["tools"] = tools
            payload["tool_choice"] = "auto"
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        url = f"{self.base_url}/chat/completions"
        for attempt in range(1, self.max_retries + 1):
            try:
                async with httpx.AsyncClient(timeout=self.timeout) as client:
                    resp = await client.post(url, json=payload, headers=headers)
                    resp.raise_for_status()
                    data = resp.json()
                    self._accumulate_usage(data.get("usage") or {})
                    if payload.get("enable_search"):
                        self.token_usage["search_calls"] += 1
                    return data["choices"][0]["message"]
            except Exception as e:
                self.token_usage["failed_attempts"] += 1
                logger.warning(f"LLM tools call attempt {attempt}/{self.max_retries} failed: {e}")
                if attempt < self.max_retries:
                    await asyncio.sleep(2 ** attempt)
                else:
                    raise

    async def chat_batch(self, messages_list: list[list[dict]], expect_json: bool = True) -> list:
        """Run multiple chat calls in parallel."""
        tasks = [self.chat(msgs, expect_json) for msgs in messages_list]
        return await asyncio.gather(*tasks, return_exceptions=True)

    @staticmethod
    def _extract_json(text: str) -> dict | list:
        """Extract JSON from LLM response text (handles markdown code blocks)."""
        # Try direct parse
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            pass

        # Try extracting from ```json ... ``` blocks
        pattern = r"```(?:json)?\s*([\s\S]*?)```"
        matches = re.findall(pattern, text)
        for match in matches:
            try:
                return json.loads(match.strip())
            except json.JSONDecodeError:
                continue

        # Try finding first { or [ and matching close
        for start_char, end_char in [("{", "}"), ("[", "]")]:
            start = text.find(start_char)
            if start == -1:
                continue
            depth = 0
            for i in range(start, len(text)):
                if text[i] == start_char:
                    depth += 1
                elif text[i] == end_char:
                    depth -= 1
                    if depth == 0:
                        try:
                            return json.loads(text[start:i + 1])
                        except json.JSONDecodeError:
                            break

        logger.error(f"Failed to extract JSON from LLM response: {text[:200]}...")
        return {"raw_text": text}
