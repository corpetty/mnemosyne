"""Summarization service and endpoint with a FakeProvider."""

import pytest
from src.mnemosyne.services.summarization_service import SummarizationService
from src.mnemosyne.summarization.prompts import (
    STYLES,
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
    assert "very brief" in get_system_prompt(3)
    assert "very brief" not in get_system_prompt(10)
    assert get_system_prompt(10).startswith(STYLES["meeting"])


@pytest.mark.anyio
async def test_service_uses_first_model_when_unspecified():
    service = SummarizationService.__new__(SummarizationService)
    provider = FakeProvider(models=["m1", "m2"])
    service.providers = {"fake": provider}

    result = await service.summarize(
        segments=[{"speaker": "S", "start": 0, "text": "x"}], provider_name="fake"
    )
    assert result["summary"] == provider.summary
    assert result["provider"] == "fake" and result["model"] == "m1"
    assert result["data"].provider == "fake"
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


def test_summarize_endpoint_saves_summary(client, fake_provider, transcribed_session):
    sid = transcribed_session["id"]
    resp = client.post(f"/api/sessions/{sid}/summarize", json={"provider": "fake"})
    assert resp.status_code == 200
    assert resp.json()["model"] == "fake-model-a"
    assert client.get(f"/api/sessions/{sid}").json()["summary"] == fake_provider.summary


def test_summarize_requires_transcript(client, fake_provider):
    sid = client.post("/api/sessions", json={}).json()["id"]
    resp = client.post(f"/api/sessions/{sid}/summarize", json={"provider": "fake"})
    assert resp.status_code == 400


def test_summarize_uses_default_provider_from_settings(
    client, ctx, fake_provider, transcribed_session
):
    ctx.settings.default_provider = "fake"
    ctx.settings.default_model = "fake-model-b"
    resp = client.post(f"/api/sessions/{transcribed_session['id']}/summarize", json={})
    assert resp.status_code == 200
    body = resp.json()
    assert body["summary"] == fake_provider.summary
    assert body["provider"] == "fake" and body["model"] == "fake-model-b"


def test_summarize_unknown_provider_is_400(client, fake_provider, transcribed_session):
    sid = transcribed_session["id"]
    resp = client.post(f"/api/sessions/{sid}/summarize", json={"provider": "nope"})
    assert resp.status_code == 400
