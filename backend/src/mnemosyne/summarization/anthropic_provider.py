"""Anthropic summarization provider."""

import logging
import os

import httpx

logger = logging.getLogger(__name__)


class AnthropicProvider:
    """Summarization via the Anthropic Messages API."""

    name = "anthropic"

    def __init__(self, api_key: str | None = None):
        self.api_key = api_key or os.environ.get("ANTHROPIC_API_KEY", "")
        self.base_url = "https://api.anthropic.com/v1"

    async def list_models(self) -> list[str]:
        """Return available Anthropic models."""
        if not self.api_key:
            return []
        # Anthropic doesn't have a list-models endpoint; return known models
        return [
            "claude-sonnet-4-20250514",
            "claude-haiku-4-20250414",
            "claude-3-5-sonnet-20241022",
            "claude-3-5-haiku-20241022",
        ]

    async def summarize(
        self,
        transcript: str,
        model: str,
        system_prompt: str,
    ) -> str:
        """Generate summary using Anthropic Messages API."""
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
                        {"role": "user", "content": f"Please summarize this transcript:\n\n{transcript}"},
                    ],
                },
            )
            resp.raise_for_status()
            data = resp.json()
            content = data.get("content", [])
            if content and content[0].get("type") == "text":
                return content[0]["text"]
            return ""
