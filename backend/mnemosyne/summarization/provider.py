"""LLM provider protocol."""

from typing import Protocol, runtime_checkable


def summarize_user_prompt(transcript: str) -> str:
    return f"Please summarize this transcript:\n\n{transcript}"


@runtime_checkable
class SummarizationProvider(Protocol):
    """Interface for LLM providers (Ollama, vLLM, OpenAI, Anthropic)."""

    name: str

    async def list_models(self) -> list[str]:
        """Return available model names from this provider."""
        ...

    async def complete(self, system_prompt: str, user_prompt: str, model: str) -> str:
        """One chat turn (system + user message); returns the assistant's text."""
        ...

    async def summarize(self, transcript: str, model: str, system_prompt: str) -> str:
        """complete() with the standard summarize user message."""
        ...
