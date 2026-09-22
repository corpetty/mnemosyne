"""Summarization service managing providers and model selection."""

from __future__ import annotations

import logging

from ..config import Settings
from ..summarization.anthropic_provider import AnthropicProvider
from ..summarization.ollama import OllamaProvider
from ..summarization.openai_provider import OpenAIProvider
from ..summarization.prompts import format_transcript_for_llm, get_system_prompt
from ..summarization.provider import SummarizationProvider
from ..summarization.vllm import VLLMProvider

logger = logging.getLogger(__name__)


class SummarizationService:
    def __init__(self, settings: Settings | None = None):
        self.providers: dict[str, SummarizationProvider] = {}
        if settings is not None:
            self._init_providers(settings)

    def _init_providers(self, settings: Settings) -> None:
        self.providers["ollama"] = OllamaProvider(base_url=settings.ollama_url)
        self.providers["vllm"] = VLLMProvider(base_url=settings.vllm_url)
        if settings.openai_api_key:
            self.providers["openai"] = OpenAIProvider(api_key=settings.openai_api_key)
        if settings.anthropic_api_key:
            self.providers["anthropic"] = AnthropicProvider(api_key=settings.anthropic_api_key)

    async def list_all_models(self) -> list[dict]:
        results = []
        for name, provider in self.providers.items():
            models = await provider.list_models()
            results.append({"provider": name, "models": models})
        return results

    async def summarize(
        self,
        segments: list[dict],
        provider_name: str = "ollama",
        model: str = "",
    ) -> dict:
        provider = self.providers.get(provider_name)
        if provider is None:
            available = list(self.providers.keys())
            raise ValueError(f"Provider '{provider_name}' not available. Available: {available}")

        if not model:
            models = await provider.list_models()
            if not models:
                raise ValueError(f"No models available from provider '{provider_name}'")
            model = models[0]

        transcript_text = format_transcript_for_llm(segments)
        system_prompt = get_system_prompt(len(segments))

        logger.info("Summarizing with %s/%s (%d segments)", provider_name, model, len(segments))
        summary = await provider.summarize(transcript_text, model, system_prompt)
        return {"summary": summary, "provider": provider_name, "model": model}
