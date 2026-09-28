"""Retries for LLM calls that fail for a moment: a rate limit, an overloaded or restarting
server, a dropped connection. A summary of an hour's meeting should not fail on one 529."""

from __future__ import annotations

import asyncio
import logging
import random

import httpx

logger = logging.getLogger(__name__)

# 408 timeout, 409/425 try again, 429 rate limited, 5xx server trouble, 529 overloaded.
TRANSIENT_STATUS = {408, 409, 425, 429, 500, 502, 503, 504, 529}
DELAYS = (2.0, 8.0, 30.0)  # before each retry
MAX_RETRY_AFTER = 60.0


def transient(error: BaseException) -> bool:
    if isinstance(error, httpx.HTTPStatusError):
        return error.response.status_code in TRANSIENT_STATUS
    # Connection refused or reset, a read that timed out: the server may be starting (Ollama
    # loading a model) or the network hiccuped.
    return isinstance(error, httpx.TransportError)


def _delay(error: BaseException, base: float) -> float:
    if isinstance(error, httpx.HTTPStatusError):
        after = error.response.headers.get("retry-after", "")
        try:
            return min(float(after), MAX_RETRY_AFTER)
        except ValueError:
            pass
    return base * random.uniform(0.8, 1.2)


async def with_retries(call, what: str, delays=DELAYS):
    """`await call()`, again after a pause when it fails for a transient reason."""
    for attempt in range(len(delays) + 1):
        try:
            return await call()
        except Exception as e:
            if attempt == len(delays) or not transient(e):
                raise
            wait = _delay(e, delays[attempt])
            logger.warning("%s failed (%s); retrying in %.0f s", what, e, wait)
            await asyncio.sleep(wait)


class RetryingProvider:
    """Wraps a provider: complete() and summarize() are retried on transient errors.
    list_models() is not (it answers the Settings page, which should not hang)."""

    def __init__(self, inner, delays=DELAYS):
        self.inner = inner
        self.name = getattr(inner, "name", "")
        self._delays = delays

    async def list_models(self) -> list[str]:
        return await self.inner.list_models()

    async def complete(self, system_prompt: str, user_prompt: str, model: str) -> str:
        return await with_retries(
            lambda: self.inner.complete(system_prompt, user_prompt, model),
            f"{self.name} {model}",
            self._delays,
        )

    async def summarize(self, transcript: str, model: str, system_prompt: str) -> str:
        return await with_retries(
            lambda: self.inner.summarize(transcript, model, system_prompt),
            f"{self.name} {model}",
            self._delays,
        )

    def __getattr__(self, item):
        return getattr(self.inner, item)
