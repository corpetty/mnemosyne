"""OpenAI summarization provider."""

import logging
import os

import httpx

logger = logging.getLogger(__name__)


class OpenAIProvider:
    """Summarization via the OpenAI API."""

    name = "openai"

    def __init__(self, api_key: str | None = None):
        self.api_key = api_key or os.environ.get("OPENAI_API_KEY", "")
        self.base_url = "https://api.openai.com/v1"

    async def list_models(self) -> list[str]:
        """Return a curated list of chat-capable models."""
        if not self.api_key:
            return []
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                resp = await client.get(
                    f"{self.base_url}/models",
                    headers={"Authorization": f"Bearer {self.api_key}"},
                )
                resp.raise_for_status()
                data = resp.json()
                # Filter to chat models
                chat_prefixes = ("gpt-4", "gpt-3.5", "o1", "o3")
                return sorted(
                    m["id"]
                    for m in data.get("data", [])
                    if any(m["id"].startswith(p) for p in chat_prefixes)
                )
        except Exception as e:
            logger.warning("Failed to list OpenAI models: %s", e)
            return []

    async def summarize(
        self,
        transcript: str,
        model: str,
        system_prompt: str,
    ) -> str:
        """Generate summary using OpenAI chat completions API."""
        async with httpx.AsyncClient(timeout=120) as client:
            resp = await client.post(
                f"{self.base_url}/chat/completions",
                headers={"Authorization": f"Bearer {self.api_key}"},
                json={
                    "model": model,
                    "messages": [
                        {"role": "system", "content": system_prompt},
                        {"role": "user", "content": f"Please summarize this transcript:\n\n{transcript}"},
                    ],
                },
            )
            resp.raise_for_status()
            data = resp.json()
            choices = data.get("choices", [])
            if choices:
                return choices[0].get("message", {}).get("content", "")
            return ""
