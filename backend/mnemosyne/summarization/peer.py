"""The models of another of this person's computers (a library-sync peer), used from this one.

One provider per paired computer, named `peer:<its sync id>`. Its models are the other
computer's own providers and models, written `<provider>/<model>` ("vllm/Qwen/Qwen3-32B",
"ollama/llama3.1:8b"). Each call goes through the link tunnel to that computer's backend
(api/routes/compute.py), which runs it with its own provider: a laptop summarizes with the
inference box the desktop already uses.

The other computer lends only providers that keep the meeting on the person's machines
(Ollama, vLLM, its built-in model): never OpenAI or Anthropic, so a peer provider is never a
cloud provider and local-only meetings may use it (summarization/privacy.py).
"""

from __future__ import annotations

import logging
import time
from collections.abc import Awaitable, Callable

import httpx

from .provider import summarize_user_prompt

logger = logging.getLogger(__name__)

PREFIX = "peer:"
LIST_TIMEOUT = 8.0  # seconds: the model list is asked for whenever the UI lists providers
COMPLETE_TIMEOUT = 900.0  # one turn of a long summary on a reasoning model can take minutes
CATALOG_TTL = 60.0  # seconds a model list is kept
FAILED_TTL = 15.0  # ... and how soon an unreachable computer is asked again

Connect = Callable[[], Awaitable[httpx.AsyncClient]]


class PeerUnavailable(Exception):
    """The other computer cannot be reached (not dialed, tunnel down, backend not running)."""


class PeerComputeError(Exception):
    """The other computer answered, but could not do the work; the message says why."""


def peer_provider_name(peer_id: str) -> str:
    return f"{PREFIX}{peer_id}"


def is_peer_provider(name: str) -> bool:
    return name.startswith(PREFIX)


def peer_id_of(name: str) -> str:
    return name[len(PREFIX) :] if is_peer_provider(name) else ""


def detail_of(error: httpx.HTTPStatusError) -> str:
    """The `detail` a FastAPI backend answered with, else the status line."""
    try:
        detail = error.response.json().get("detail")
        if isinstance(detail, str) and detail:
            return detail
    except Exception:  # noqa: BLE001 - not JSON
        pass
    return f"HTTP {error.response.status_code}"


class PeerProvider:
    """SummarizationProvider for one paired computer. `connect()` returns an httpx client for
    that computer's backend (dialed, with the token it gave this computer at pairing) or raises
    PeerUnavailable."""

    def __init__(self, peer_id: str, peer_name: str, connect: Connect):
        self.peer_id = peer_id
        self.peer_name = peer_name
        self.name = peer_provider_name(peer_id)
        self._connect = connect
        self._catalog: list[dict] = []
        self._catalog_at = 0.0
        self._catalog_ok = False

    @property
    def label(self) -> str:
        return f"{self.peer_name} (paired computer)"

    async def catalog(self) -> list[dict]:
        """The other computer's lent providers: [{provider, models, max_chars}], its default
        provider and model first. Kept for a minute; [] when it cannot be reached."""
        ttl = CATALOG_TTL if self._catalog_ok else FAILED_TTL
        if self._catalog_at and time.monotonic() - self._catalog_at < ttl:
            return self._catalog
        try:
            async with await self._connect() as client:
                r = await client.get("/api/compute/models", timeout=LIST_TIMEOUT)
                r.raise_for_status()
                self._catalog = list(r.json().get("providers") or [])
                self._catalog_ok = True
        except (PeerUnavailable, httpx.HTTPError, ValueError) as e:
            logger.info("Models of %s not listed: %s", self.peer_name, e)
            self._catalog, self._catalog_ok = [], False
        self._catalog_at = time.monotonic()
        return self._catalog

    async def list_models(self) -> list[str]:
        return [f"{p['provider']}/{m}" for p in await self.catalog() for m in p.get("models", [])]

    def split_model(self, model: str) -> tuple[str, str]:
        """("vllm", "Qwen/Qwen3-32B") from "vllm/Qwen/Qwen3-32B". A model without one of the
        lent providers in front goes to the other computer's default provider ("", model)."""
        head, sep, rest = model.partition("/")
        known = {p.get("provider") for p in self._catalog}
        if sep and (head in known or not self._catalog):
            return head, rest
        return "", model

    def max_chars_for(self, model: str) -> int:
        """How much transcript one request may carry for this model (0: no limit): the other
        computer's built-in model has a small context (LocalProvider.max_chars)."""
        provider, _ = self.split_model(model)
        for p in self._catalog:
            if p.get("provider") == provider:
                return int(p.get("max_chars") or 0)
        return 0

    async def complete(
        self, system_prompt: str, user_prompt: str, model: str, think: bool = True
    ) -> str:
        if not self._catalog:
            await self.catalog()  # to read the provider off the model name
        provider, name = self.split_model(model)
        body = {
            "provider": provider,
            "model": name,
            "system": system_prompt,
            "user": user_prompt,
            "think": think,
        }
        try:
            async with await self._connect() as client:
                r = await client.post("/api/compute/complete", json=body, timeout=COMPLETE_TIMEOUT)
                r.raise_for_status()
                return str(r.json().get("text") or "")
        except httpx.HTTPStatusError as e:
            raise PeerComputeError(f"{self.peer_name}: {detail_of(e)}") from e
        except httpx.HTTPError as e:
            raise PeerUnavailable(
                f"{self.peer_name} did not answer: {e or type(e).__name__}"
            ) from e

    async def summarize(self, transcript: str, model: str, system_prompt: str) -> str:
        return await self.complete(system_prompt, summarize_user_prompt(transcript), model)
