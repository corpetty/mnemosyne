"""Anthropic summarization provider."""

import logging
import os
import re

import httpx

from .provider import summarize_user_prompt

logger = logging.getLogger(__name__)

# Claude 5 models think by default, and on Opus/Sonnet 5.5 thinking cannot be turned off.
_CLAUDE_5 = re.compile(r"claude-(opus|sonnet|haiku|fable|mythos)-5")


def thinks_by_default(model: str) -> bool:
    """True for Claude 5-family ids (the ones that accept `output_config.effort`)."""
    return _CLAUDE_5.match(model) is not None


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

    async def complete(
        self, system_prompt: str, user_prompt: str, model: str, think: bool = True
    ) -> str:
        """One chat turn: system + user message, returns the assistant text."""
        body = {
            "model": model,
            # Thinking counts against max_tokens; 16000 is the ceiling without streaming.
            "max_tokens": 16000,
            "system": system_prompt,
            "messages": [{"role": "user", "content": user_prompt}],
        }
        if not think and thinks_by_default(model):
            # Thinking cannot be disabled on these models; ask for less of it.
            body["output_config"] = {"effort": "low"}
        async with httpx.AsyncClient(timeout=600) as client:
            resp = await client.post(
                f"{self.base_url}/messages",
                headers={
                    "x-api-key": self.api_key,
                    "anthropic-version": "2023-06-01",
                    "content-type": "application/json",
                },
                json=body,
            )
            resp.raise_for_status()
            data = resp.json()
        # Thinking blocks come first; only the text blocks are the answer.
        text = "".join(
            b.get("text", "") for b in data.get("content") or [] if b.get("type") == "text"
        )
        stop = data.get("stop_reason")
        if not text:
            logger.warning("Anthropic returned no text (model %s, stop_reason %s)", model, stop)
            if stop == "max_tokens":
                raise RuntimeError(
                    f"Anthropic stopped at the token limit before writing an answer (model {model})"
                )
            if stop == "refusal":
                raise RuntimeError(f"Anthropic declined to answer (model {model})")
            raise RuntimeError(f"Anthropic returned no text (model {model}, stop_reason {stop})")
        if stop == "max_tokens":
            logger.warning(
                "Anthropic hit the token limit (model %s); the answer may be cut off", model
            )
        return text
