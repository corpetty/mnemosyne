"""Summarization service and endpoint with a FakeProvider."""

import pytest
from src.mnemosyne.services.summarization_service import SummarizationService
from src.mnemosyne.summarization.prompts import (
    COMPACT_SYSTEM_PROMPT,
    SYSTEM_PROMPT,
    format_transcript_for_llm,
    get_system_prompt,
)

from tests.fakes import FakeProvider


def test_format_transcript_for_llm():
    segments = [
        {"speaker": "SPEAKER_00", "start": 0.0, "text": " Hello "},
        {"speaker": "SPEAKER_01", "start": 65.4, "text": "Hi"},
    ]
    assert (
        format_transcript_for_llm(segments) == "[00:00] SPEAKER_00: Hello\n[01:05] SPEAKER_01: Hi"
    )


def test_prompt_selection_by_length():
    assert get_system_prompt(3) == COMPACT_SYSTEM_PROMPT
    assert get_system_prompt(10) == SYSTEM_PROMPT


@pytest.mark.anyio
async def test_service_uses_first_model_when_unspecified():
    service = SummarizationService.__new__(SummarizationService)
    provider = FakeProvider(models=["m1", "m2"])
    service.providers = {"fake": provider}

    result = await service.summarize(
        segments=[{"speaker": "S", "start": 0, "text": "x"}], provider_name="fake"
    )
    assert result == {"summary": provider.summary, "provider": "fake", "model": "m1"}
    assert provider.calls[0]["transcript"] == "[00:00] S: x"


@pytest.mark.anyio
async def test_service_rejects_unknown_provider():
    service = SummarizationService.__new__(SummarizationService)
    service.providers = {"fake": FakeProvider()}
    with pytest.raises(ValueError, match="not available"):
        await service.summarize(segments=[], provider_name="nope")


@pytest.mark.anyio
async def test_service_rejects_provider_with_no_models():
    service = SummarizationService.__new__(SummarizationService)
    service.providers = {"fake": FakeProvider(models=[])}
    with pytest.raises(ValueError, match="No models"):
        await service.summarize(segments=[], provider_name="fake")


def test_models_endpoint_lists_providers(client, fake_provider):
    assert client.get("/api/models").json() == [
        {"provider": "fake", "models": ["fake-model-a", "fake-model-b"]}
    ]


def test_summarize_endpoint_saves_summary(client, fake_provider, session_with_transcript):
    sid = session_with_transcript["id"]
    resp = client.post(f"/api/sessions/{sid}/summarize", json={"provider": "fake"})
    assert resp.status_code == 200
    assert resp.json()["model"] == "fake-model-a"
    assert client.get(f"/api/sessions/{sid}").json()["summary"] == fake_provider.summary


def test_summarize_requires_transcript(client, fake_provider):
    sid = client.post("/api/sessions", json={}).json()["id"]
    resp = client.post(f"/api/sessions/{sid}/summarize", json={"provider": "fake"})
    assert resp.status_code == 400


def test_summarize_unknown_provider_is_400(client, fake_provider, session_with_transcript):
    sid = session_with_transcript["id"]
    resp = client.post(f"/api/sessions/{sid}/summarize", json={"provider": "nope"})
    assert resp.status_code == 400
