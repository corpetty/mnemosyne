"""Ollama summarization provider (LAN-first)."""

import logging

import httpx

logger = logging.getLogger(__name__)

DEFAULT_URL = "http://localhost:11434"


class OllamaProvider:
    """Summarization via Ollama API."""

    name = "ollama"

    def __init__(self, base_url: str = DEFAULT_URL):
        self.base_url = base_url.rstrip("/")

    # Families that are embedding-only and can't do chat
    EMBEDDING_FAMILIES = {
        "bert", "nomic-bert", "nomic-bert-moe",
    }
    EMBEDDING_KEYWORDS = {"embed", "embedding"}

    async def list_models(self) -> list[str]:
        """Fetch available chat-capable models from Ollama."""
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                resp = await client.get(f"{self.base_url}/api/tags")
                resp.raise_for_status()
                data = resp.json()
                models = []
                for m in data.get("models", []):
                    name = m["name"]
                    family = m.get("details", {}).get("family", "")
                    # Skip embedding models
                    if family in self.EMBEDDING_FAMILIES:
                        continue
                    if any(kw in name.lower() for kw in self.EMBEDDING_KEYWORDS):
                        continue
                    models.append(name)
                return models
        except Exception as e:
            logger.warning("Failed to list Ollama models: %s", e)
            return []

    async def summarize(
        self,
        transcript: str,
        model: str,
        system_prompt: str,
    ) -> str:
        """Generate summary using Ollama chat API."""
        async with httpx.AsyncClient(timeout=300) as client:
            resp = await client.post(
                f"{self.base_url}/api/chat",
                json={
                    "model": model,
                    "messages": [
                        {"role": "system", "content": system_prompt},
                        {"role": "user", "content": f"Please summarize this transcript:\n\n{transcript}"},
                    ],
                    "stream": False,
                },
            )
            resp.raise_for_status()
            data = resp.json()
            return data.get("message", {}).get("content", "")
