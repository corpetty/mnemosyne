"""LLM provider protocol."""

from typing import Protocol, runtime_checkable


def think_kwargs(think: bool) -> dict:
    """complete()'s `think` for a wrapped provider: only when off, so a provider written
    before the switch still takes the call."""
    return {} if think else {"think": False}


def summarize_user_prompt(transcript: str) -> str:
    return f"Please summarize this transcript:\n\n{transcript}"


@runtime_checkable
class SummarizationProvider(Protocol):
    """Interface for LLM providers (Ollama, vLLM, OpenAI, Anthropic)."""

    name: str

    async def list_models(self) -> list[str]:
        """Return available model names from this provider."""
        ...

    async def complete(
        self, system_prompt: str, user_prompt: str, model: str, think: bool = True
    ) -> str:
        """One chat turn (system + user message); returns the assistant's text. think=False
        asks a reasoning model not to think first, for short mechanical answers (the glossary
        pass), where providers can switch it (vLLM, Ollama, the built-in model); others
        ignore it."""
        ...

    async def summarize(self, transcript: str, model: str, system_prompt: str) -> str:
        """complete() with the standard summarize user message."""
        ...
