"""Summarization service and endpoint with a FakeProvider."""

import pytest

from mnemosyne.services.summarization_service import SummarizationService
from mnemosyne.summarization.prompts import (
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
        "hubspot": None,
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


def test_summary_reads_the_attendees_notes(client, ctx, fake_provider, transcribed_session):
    sid = transcribed_session["id"]
    client.post(f"/api/sessions/{sid}/notes", json={"notes": "- Ana owns the budget"})
    assert run_summarize(client, sid, {"provider": "fake"})["status"] == "completed"
    assert "Ana owns the budget" in fake_provider.calls[-1]["system_prompt"]


def test_empty_notes_add_nothing_to_the_summary(client, ctx, fake_provider, transcribed_session):
    sid = transcribed_session["id"]
    client.post(f"/api/sessions/{sid}/notes", json={"notes": "   \n"})
    assert run_summarize(client, sid, {"provider": "fake"})["status"] == "completed"
    assert "Attendees' notes" not in fake_provider.calls[-1]["system_prompt"]


# ---- action items typed in the notes (services/notes_items.py) -------------------------------


def _meeting_with_notes_items(ctx):
    from datetime import datetime

    from mnemosyne.models.session import NotesItem, Session
    from mnemosyne.models.transcript import TranscriptSegment

    sid = ctx.repo.save(
        Session(
            name="Planning",
            created_at=datetime(2026, 10, 8, 10),
            transcript=[TranscriptSegment(text="the deck", speaker="Ann", start=0, end=2)],
        )
    ).id
    ctx.repo.set_notes_items(
        sid,
        [
            NotesItem(id="a" * 16, line="1" * 64, text="Send the deck to the board", owner="Ada"),
            NotesItem(id="b" * 16, line="2" * 64, text="Book the room", owner="Bo", done=True),
            NotesItem(id="c" * 16, line="3" * 64, text="Water the plants", owner=None),
        ],
    )
    return sid


def _llm_reply(*items) -> str:
    import json

    return json.dumps(
        {"summary": ["Notes."], "decisions": [], "open_questions": [], "action_items": list(items)}
    )


def test_notes_items_hint_lists_text_and_owner():
    from mnemosyne.summarization.prompts import notes_items_hint

    assert notes_items_hint([]) == ""
    block = notes_items_hint([("Send the deck", "Ada"), ("Book the room", None)])
    assert block.startswith(
        "Already recorded as action items in the meeting notes (do not list them again):"
    )
    assert "- Send the deck (owner: Ada)\n- Book the room" in block


def test_the_summary_prompt_tells_which_action_items_the_notes_hold(client, ctx, fake_provider):
    sid = _meeting_with_notes_items(ctx)
    fake_provider.summary = _llm_reply()
    assert run_summarize(client, sid, {"provider": "fake"})["status"] == "completed"
    prompt = fake_provider.calls[0]["system_prompt"]
    assert "Already recorded as action items in the meeting notes" in prompt
    assert "- Send the deck to the board (owner: Ada)" in prompt
    assert "- Book the room (owner: Bo)" in prompt
    assert "- Water the plants" in prompt


def test_no_notes_items_no_hint(client, ctx, fake_provider):
    sid = _meeting_with_notes_items(ctx)
    ctx.repo.set_notes_items(sid, [])
    fake_provider.summary = _llm_reply()
    assert run_summarize(client, sid, {"provider": "fake"})["status"] == "completed"
    assert "Already recorded as action items" not in fake_provider.calls[0]["system_prompt"]


def test_an_llm_item_that_repeats_a_notes_item_is_dropped(client, ctx, fake_provider):
    sid = _meeting_with_notes_items(ctx)
    fake_provider.summary = _llm_reply(
        {"text": "Send the deck to the board.", "owner": "Ada"},  # the same, owner the same
        {"text": "Book the room", "owner": "Cy"},  # same words, another owner: stays
        {"text": "Book the room"},  # same words, no owner: dropped
        {"text": "water the plants!", "owner": "Cy"},  # the notes item has no owner: dropped
        {"text": "Order badges", "owner": "Ada"},
    )
    assert run_summarize(client, sid, {"provider": "fake"})["status"] == "completed"
    texts = [(a.text, a.owner) for a in ctx.repo.get(sid).summary_data.action_items]
    assert texts == [("Book the room", "Cy"), ("Order badges", "Ada")]
    assert len(ctx.repo.notes_items(sid)) == 3  # the notes' own items are untouched


def test_an_ownerless_llm_item_matches_a_notes_item_with_an_owner(client, ctx, fake_provider):
    sid = _meeting_with_notes_items(ctx)
    fake_provider.summary = _llm_reply({"text": "Send the deck to the board"})
    assert run_summarize(client, sid, {"provider": "fake"})["status"] == "completed"
    assert ctx.repo.get(sid).summary_data.action_items == []
