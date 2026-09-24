"""Anthropic summarization provider."""

import logging
import os

import httpx

from .provider import summarize_user_prompt

logger = logging.getLogger(__name__)


class AnthropicProvider:
    """Summarization via the Anthropic Messages API."""

    name = "anthropic"

    def __init__(self, api_key: str | None = None):
        self.api_key = api_key or os.environ.get("ANTHROPIC_API_KEY", "")
        self.base_url = "https://api.anthropic.com/v1"

    async def list_models(self) -> list[str]:
        """Models from GET /v1/models, newest first."""
        if not self.api_key:
            return []
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                resp = await client.get(
                    f"{self.base_url}/models",
                    headers={"x-api-key": self.api_key, "anthropic-version": "2023-06-01"},
                    params={"limit": 100},
                )
                resp.raise_for_status()
                return [m["id"] for m in resp.json().get("data", []) if m.get("id")]
        except Exception as e:
            logger.warning("Failed to list Anthropic models: %s", e)
            return []

    async def summarize(self, transcript: str, model: str, system_prompt: str) -> str:
        return await self.complete(system_prompt, summarize_user_prompt(transcript), model)

    async def complete(self, system_prompt: str, user_prompt: str, model: str) -> str:
        """One chat turn: system + user message, returns the assistant text."""
        async with httpx.AsyncClient(timeout=120) as client:
            resp = await client.post(
                f"{self.base_url}/messages",
                headers={
                    "x-api-key": self.api_key,
                    "anthropic-version": "2023-06-01",
                    "content-type": "application/json",
                },
                json={
                    "model": model,
                    "max_tokens": 4096,
                    "system": system_prompt,
                    "messages": [
                        {
                            "role": "user",
                            "content": user_prompt,
                        },
                    ],
                },
            )
            resp.raise_for_status()
            data = resp.json()
            content = data.get("content", [])
            if content and content[0].get("type") == "text":
                return content[0]["text"]
            return ""
