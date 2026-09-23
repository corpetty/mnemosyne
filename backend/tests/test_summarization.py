"""Summarization service and endpoint with a FakeProvider."""

import pytest
from src.mnemosyne.services.summarization_service import SummarizationService
from src.mnemosyne.summarization.prompts import (
    STYLES,
    format_transcript_for_llm,
    get_system_prompt,
)

from tests.conftest import drain_until_job, run_summarize
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
    job = run_summarize(client, sid, {"provider": "fake"})
    assert job["status"] == "completed"
    assert job["result"]["model"] == "fake-model-a"
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
    job = run_summarize(client, transcribed_session["id"])
    assert job["result"] == {
        "provider": "fake",
        "model": "fake-model-b",
        "title": "",
        "exported": None,
    }
    assert fake_provider.calls[-1]["model"] == "fake-model-b"


def test_summarize_unknown_provider_fails_job(client, fake_provider, transcribed_session):
    sid = transcribed_session["id"]
    job = run_summarize(client, sid, {"provider": "nope"})
    assert job["status"] == "failed"
    assert "not available" in job["error"]
    assert client.get(f"/api/sessions/{sid}").json()["summary"] == ""


def test_summarize_409_when_already_running(client, ctx, transcribed_session):
    import asyncio

    class SlowProvider:
        name = "slow"

        async def list_models(self):
            return ["m"]

        async def summarize(self, transcript, model, system_prompt):
            await asyncio.sleep(0.3)
            return "done"

    ctx.summarizer.providers = {"slow": SlowProvider()}
    sid = transcribed_session["id"]
    with client.websocket_connect("/ws") as ws:
        ws.receive_json()
        first = client.post(f"/api/sessions/{sid}/summarize", json={"provider": "slow"})
        second = client.post(f"/api/sessions/{sid}/summarize", json={"provider": "slow"})
        assert first.status_code == 200
        assert second.status_code == 409
        drain_until_job(ws, first.json()["id"])


def test_auto_summarize_chains_after_transcribe(client, ctx, fake_engine, fake_provider):
    ctx.settings.auto_summarize = True
    ctx.settings.default_provider = "fake"
    sid = client.post("/api/sessions", json={}).json()["id"]
    ctx.sessions.set_audio(sid, "/fake.ogg", [])
    with client.websocket_connect("/ws") as ws:
        ws.receive_json()
        job = client.post(f"/api/sessions/{sid}/transcribe").json()
        drain_until_job(ws, job["id"])
        while True:
            msg = ws.receive_json()
            if (
                msg["type"] == "job"
                and msg["job"]["kind"] == "summarize"
                and msg["job"]["status"] in ("completed", "failed")
            ):
                assert msg["job"]["status"] == "completed", msg["job"]["error"]
                break
    assert client.get(f"/api/sessions/{sid}").json()["summary"] == fake_provider.summary


def test_no_auto_summarize_by_default(client, ctx, fake_engine, fake_provider, transcribed_session):
    assert all(j["kind"] != "summarize" for j in client.get("/api/jobs").json())
