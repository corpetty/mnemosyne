"""Summarization service managing providers and model selection."""

import logging
import os

from ..summarization.anthropic_provider import AnthropicProvider
from ..summarization.ollama import OllamaProvider
from ..summarization.openai_provider import OpenAIProvider
from ..summarization.prompts import format_transcript_for_llm, get_system_prompt
from ..summarization.vllm import VLLMProvider

logger = logging.getLogger(__name__)


class SummarizationService:
    """Manages summarization providers and delegates requests."""

    def __init__(self):
        self.providers: dict[str, object] = {}
        self._init_providers()

    def _init_providers(self):
        """Initialize all configured providers."""
        # Ollama is always available (LAN default)
        ollama_url = os.environ.get("OLLAMA_URL", "http://bugger.ender.verse:11434")
        self.providers["ollama"] = OllamaProvider(base_url=ollama_url)

        # vLLM on LAN
        vllm_url = os.environ.get("VLLM_URL", "http://bugger.ender.verse:8000")
        self.providers["vllm"] = VLLMProvider(base_url=vllm_url)

        # Cloud providers (only if API keys are set)
        if os.environ.get("OPENAI_API_KEY"):
            self.providers["openai"] = OpenAIProvider()

        if os.environ.get("ANTHROPIC_API_KEY"):
            self.providers["anthropic"] = AnthropicProvider()

    async def list_all_models(self) -> list[dict]:
        """List models from all providers."""
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
        """Summarize transcript segments using the specified provider.

        Args:
            segments: List of transcript segment dicts.
            provider_name: Which provider to use.
            model: Model name. If empty, uses first available from provider.

        Returns:
            Dict with 'summary', 'provider', and 'model' keys.
        """
        provider = self.providers.get(provider_name)
        if provider is None:
            available = list(self.providers.keys())
            raise ValueError(
                f"Provider '{provider_name}' not available. Available: {available}"
            )

        # If no model specified, pick first available
        if not model:
            models = await provider.list_models()
            if not models:
                raise ValueError(f"No models available from provider '{provider_name}'")
            model = models[0]

        transcript_text = format_transcript_for_llm(segments)
        system_prompt = get_system_prompt(len(segments))

        logger.info("Summarizing with %s/%s (%d segments)", provider_name, model, len(segments))
        summary = await provider.summarize(transcript_text, model, system_prompt)

        return {
            "summary": summary,
            "provider": provider_name,
            "model": model,
        }


# Global singleton
summarization_service = SummarizationService()
