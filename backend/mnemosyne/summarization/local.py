"""The built-in model (services/local_llm.py) as a summarization provider: llama-server's
OpenAI-compatible API, started on the first request. On a CPU a long meeting takes minutes, so
requests may run for up to half an hour."""

from __future__ import annotations

import httpx

from ..services.local_llm import MODELS, LocalLLM
from .provider import summarize_user_prompt

TIMEOUT = httpx.Timeout(1800.0, connect=10.0)


class LocalProvider:
    name = "local"

    def __init__(self, llm: LocalLLM):
        self.llm = llm

    async def list_models(self) -> list[str]:
        """The models downloaded, the recommended one first when it is here."""
        return self.llm.downloaded()

    async def summarize(self, transcript: str, model: str, system_prompt: str) -> str:
        return await self.complete(system_prompt, summarize_user_prompt(transcript), model)

    async def complete(self, system_prompt: str, user_prompt: str, model: str) -> str:
        model = model if model in MODELS else (self.llm.downloaded() or [""])[0]
        base = await self.llm.url(model)
        try:
            async with httpx.AsyncClient(timeout=TIMEOUT) as client:
                resp = await client.post(
                    f"{base}/v1/chat/completions",
                    json={
                        "model": model,
                        "messages": [
                            {"role": "system", "content": system_prompt},
                            {"role": "user", "content": user_prompt},
                        ],
                    },
                )
                resp.raise_for_status()
                choices = resp.json().get("choices", [])
                return choices[0].get("message", {}).get("content", "") if choices else ""
        finally:
            self.llm.touch()  # idle time counts from the end of a request
