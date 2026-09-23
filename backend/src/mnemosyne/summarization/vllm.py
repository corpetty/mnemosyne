"""vLLM summarization provider (OpenAI-compatible API on LAN)."""

import logging

import httpx

from .provider import summarize_user_prompt

logger = logging.getLogger(__name__)

DEFAULT_URL = "http://localhost:8000"


class VLLMProvider:
    """Summarization via vLLM's OpenAI-compatible API."""

    name = "vllm"

    def __init__(self, base_url: str = DEFAULT_URL):
        self.base_url = base_url.rstrip("/")

    async def list_models(self) -> list[str]:
        """Fetch available models from vLLM."""
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                resp = await client.get(f"{self.base_url}/v1/models")
                resp.raise_for_status()
                data = resp.json()
                return [m["id"] for m in data.get("data", [])]
        except Exception as e:
            logger.warning("Failed to list vLLM models: %s", e)
            return []

    async def summarize(self, transcript: str, model: str, system_prompt: str) -> str:
        return await self.complete(system_prompt, summarize_user_prompt(transcript), model)

    async def complete(self, system_prompt: str, user_prompt: str, model: str) -> str:
        """One chat turn: system + user message, returns the assistant text."""
        async with httpx.AsyncClient(timeout=300) as client:
            resp = await client.post(
                f"{self.base_url}/v1/chat/completions",
                json={
                    "model": model,
                    "messages": [
                        {"role": "system", "content": system_prompt},
                        {
                            "role": "user",
                            "content": user_prompt,
                        },
                    ],
                },
            )
            resp.raise_for_status()
            data = resp.json()
            choices = data.get("choices", [])
            if choices:
                return choices[0].get("message", {}).get("content", "")
            return ""
