"""Summarization provider protocol."""

from typing import Protocol, runtime_checkable


@runtime_checkable
class SummarizationProvider(Protocol):
    """Interface for LLM summarization providers."""

    name: str

    async def list_models(self) -> list[str]:
        """Return available model names from this provider."""
        ...

    async def summarize(
        self,
        transcript: str,
        model: str,
        system_prompt: str,
    ) -> str:
        """Generate a summary from a transcript.

        Args:
            transcript: The formatted transcript text.
            model: The model name to use.
            system_prompt: The system prompt for summarization.

        Returns:
            The generated summary as markdown text.
        """
        ...
