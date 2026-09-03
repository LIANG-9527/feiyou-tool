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
                 timeout: int = 120, max_retries: int = 3):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.model = model
        self.temperature = temperature
        self.max_tokens = max_tokens
        self.timeout = timeout
        self.max_retries = max_retries

    async def chat(self, messages: list[dict], expect_json: bool = True) -> str | dict | list:
        """Send chat messages and return the response text or parsed JSON."""
        payload = {
            "model": self.model,
            "messages": messages,
            "temperature": self.temperature,
            "max_tokens": self.max_tokens,
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
                    text = data["choices"][0]["message"]["content"]
                    if expect_json:
                        return self._extract_json(text)
                    return text
            except Exception as e:
                logger.warning(f"LLM call attempt {attempt}/{self.max_retries} failed: {e}")
                if attempt < self.max_retries:
                    await asyncio.sleep(2 ** attempt)
                else:
                    raise

    async def chat_with_tools(self, messages: list[dict], tools: list = None) -> dict:
        """Call LLM with function-calling support. Returns the full message object."""
        payload = {
            "model": self.model,
            "messages": messages,
            "temperature": self.temperature,
            "max_tokens": self.max_tokens,
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
                    return data["choices"][0]["message"]
            except Exception as e:
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
