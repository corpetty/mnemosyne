"""Transient LLM failures are retried (summarization/retry.py); real errors are not."""

import httpx
import pytest

from mnemosyne.summarization.retry import RetryingProvider, transient


def _status(code: int, headers: dict | None = None) -> httpx.HTTPStatusError:
    request = httpx.Request("POST", "https://llm.example/v1")
    response = httpx.Response(code, request=request, headers=headers or {})
    return httpx.HTTPStatusError(f"{code}", request=request, response=response)


class Flaky:
    name = "flaky"

    def __init__(self, errors):
        self.errors = list(errors)
        self.calls = 0

    async def list_models(self):
        return ["m"]

    async def complete(self, system_prompt, user_prompt, model):
        self.calls += 1
        if self.errors:
            raise self.errors.pop(0)
        return "ok"

    async def summarize(self, transcript, model, system_prompt):
        return await self.complete(system_prompt, transcript, model)


def test_what_counts_as_transient():
    assert transient(_status(429)) and transient(_status(529)) and transient(_status(503))
    assert not transient(_status(400)) and not transient(_status(401))
    assert transient(httpx.ConnectError("refused"))
    assert transient(httpx.ReadTimeout("slow"))
    assert not transient(ValueError("bad json"))


@pytest.mark.anyio
async def test_rate_limits_and_dropped_connections_are_retried():
    inner = Flaky([_status(429), httpx.RemoteProtocolError("reset")])
    provider = RetryingProvider(inner, delays=(0, 0, 0))
    assert await provider.complete("s", "u", "m") == "ok"
    assert inner.calls == 3


@pytest.mark.anyio
async def test_a_bad_request_fails_at_once():
    inner = Flaky([_status(401)])
    with pytest.raises(httpx.HTTPStatusError):
        await RetryingProvider(inner, delays=(0, 0, 0)).summarize("t", "m", "s")
    assert inner.calls == 1


@pytest.mark.anyio
async def test_it_gives_up_after_the_last_retry():
    inner = Flaky([_status(503)] * 4)
    with pytest.raises(httpx.HTTPStatusError):
        await RetryingProvider(inner, delays=(0, 0)).complete("s", "u", "m")
    assert inner.calls == 3


def test_providers_are_wrapped(settings):
    from mnemosyne.services.summarization_service import SummarizationService

    svc = SummarizationService(settings)
    assert all(isinstance(p, RetryingProvider) for p in svc.providers.values())
    assert svc.providers["ollama"].name == "ollama"
