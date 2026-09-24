"""Local-only meetings and redaction before cloud providers."""

import json
from datetime import datetime

import pytest

from mnemosyne.models.session import Session, SummaryData
from mnemosyne.models.transcript import TranscriptSegment
from mnemosyne.summarization.privacy import RedactingProvider, Redactor
from tests.conftest import drain_until_job
from tests.fakes import FakeProvider


def test_redactor_round_trip():
    r = Redactor(["Jakub Sokołowski", "Alice"])
    text = (
        "Alice asked jakub sokołowski (jakub@status.im, +48 601 234 567) at 12:30. "
        "Jakub agreed; alice will follow up. Call ext 42."
    )
    out = r.redact(text)
    assert "Alice" not in out and "Jakub" not in out and "jakub" not in out
    assert "status.im" not in out and "601" not in out
    assert "12:30" in out and "42" in out  # times and short numbers stay
    assert out.count("[PERSON_1]") == 2  # same person, same placeholder, any case
    reply = "Summary: [PERSON_1] and [PERSON_2] met; email [EMAIL_1]. [PERSON_9] unknown."
    back = r.restore(reply)
    assert back.startswith("Summary: Alice and jakub sokołowski met; email jakub@status.im.")
    assert "[PERSON_9]" in back
    assert Redactor([]).redact("no names here") == "no names here"


@pytest.mark.anyio
async def test_redacting_provider_wraps_both_calls():
    inner = FakeProvider()
    inner.reply = "Talked to [PERSON_1]."
    p = RedactingProvider(inner, lambda: ["Corey"])
    assert await p.complete("sys Corey", "Corey said hi", "m") == "Talked to Corey."
    assert inner.calls[-1]["transcript"] == "[PERSON_1] said hi"
    assert inner.calls[-1]["system_prompt"] == "sys [PERSON_1]"
    inner.summary = json.dumps({"summary": "[PERSON_1] leads"})
    raw = await p.summarize("Corey: I lead", "m", "system")
    assert json.loads(raw)["summary"] == "Corey leads"
    assert inner.calls[-1]["transcript"] == "[PERSON_1]: I lead"
    assert await p.list_models() == inner.models


def _session(ctx, local_only=True):
    s = Session(
        name="Board prep",
        created_at=datetime(2026, 9, 24, 9),
        local_only=local_only,
        transcript=[TranscriptSegment(text="secret merger plans", speaker="A", start=0, end=4)],
        summary="Merger talk.",
        summary_data=SummaryData(),
    )
    return ctx.repo.save(s)


def test_local_only_round_trip(client, ctx):
    s = _session(ctx, local_only=False)
    r = client.put(f"/api/sessions/{s.id}/local-only", json={"local_only": True})
    assert r.status_code == 200 and r.json()["local_only"] is True
    listed = {x["id"]: x for x in client.get("/api/sessions").json()}
    assert listed[s.id]["local_only"] is True
    assert ctx.repo.local_only_ids() == {s.id}


def test_local_only_blocks_cloud_summaries(client, ctx):
    s = _session(ctx)
    cloud = FakeProvider()
    ctx.summarizer.providers = {"openai": cloud, "fake": FakeProvider()}
    r = client.post(f"/api/sessions/{s.id}/summarize", json={"provider": "openai"})
    assert r.status_code == 400 and "local-only" in r.json()["detail"]
    r = client.post(f"/api/sessions/{s.id}/followup", json={"provider": "openai"})
    assert r.status_code == 400
    assert cloud.calls == []
    # A local provider is fine.
    assert (
        client.post(f"/api/sessions/{s.id}/summarize", json={"provider": "fake"}).status_code == 200
    )


def test_ask_leaves_local_only_meetings_out_for_cloud(client, ctx):
    _session(ctx)
    cloud = FakeProvider()
    cloud.reply = "Nothing [1]."
    ctx.summarizer.providers = {"openai": cloud, "fake": FakeProvider()}
    with client.websocket_connect("/ws") as ws:
        ws.receive_json()
        job = client.post(
            "/api/ask", json={"question": "merger plans", "provider": "openai"}
        ).json()
        drain_until_job(ws, job["id"])
    result = client.get(f"/api/jobs/{job['id']}").json()["result"]
    assert "merger" not in json.dumps(cloud.calls)  # never sent
    assert result["citations"] == []


def test_cloud_redaction_setting_wraps_cloud_providers(ctx, monkeypatch):
    from mnemosyne.services.summarization_service import SummarizationService

    st = ctx.settings.model_copy(update={"cloud_redaction": True, "openai_api_key": "sk-x"})
    svc = SummarizationService(st)
    assert isinstance(svc.providers["openai"], RedactingProvider)
    assert not isinstance(svc.providers["ollama"], RedactingProvider)
