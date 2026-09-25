"""Live copilot: notes updated from the live transcript, and questions mid-meeting."""

import asyncio
import json

import pytest

from mnemosyne.models.session import Session
from mnemosyne.models.transcript import TranscriptSegment
from mnemosyne.services.copilot import (
    MIN_NEW_CHARS,
    CopilotItem,
    CopilotNotes,
    answer_live,
    copilot_runner,
    parse_notes,
    update_notes,
)


def _seg(i, text, speaker="SPEAKER_00"):
    return TranscriptSegment(text=text, speaker=speaker, start=i * 10.0, end=i * 10.0 + 5)


class FakeLive:
    def __init__(self):
        self.committed: list[TranscriptSegment] = []


def test_parse_notes_tolerant():
    body = {
        "summary": ["a"],
        "decisions": ["d"],
        "action_items": [{"text": "t", "owner": "null"}, "u"],
        "open_questions": [],
    }
    raw = "Sure!\n```json\n" + json.dumps(body) + "\n```"
    n = parse_notes(raw, "s1", 7)
    assert n.summary == ["a"] and n.decisions == ["d"] and n.lines == 7
    assert [(a.text, a.owner) for a in n.action_items] == [("t", None), ("u", None)]
    assert parse_notes("no json at all", "s1", 1) is None


@pytest.mark.anyio
async def test_update_sends_only_new_lines():
    calls = []

    async def complete(system, user):
        calls.append(user)
        return json.dumps({"summary": [f"round {len(calls)}"]})

    segs = [_seg(0, "hello"), _seg(1, "we ship in October")]
    first = await update_notes(complete, None, "s1", segs)
    assert first.lines == 2 and first.summary == ["round 1"]
    assert (
        "Previous notes:\n{}" in calls[0] and "[00:10] SPEAKER_00: we ship in October" in calls[0]
    )
    segs.append(_seg(2, "docs next week"))
    second = await update_notes(complete, first, "s1", segs)
    assert second.lines == 3
    assert "round 1" in calls[1] and "docs next week" in calls[1]
    assert "we ship in October" not in calls[1]  # already folded into the notes
    assert await update_notes(complete, second, "s1", segs) is second  # nothing new


@pytest.mark.anyio
async def test_answer_live_keeps_recent_lines_within_budget():
    seen = {}

    async def complete(system, user):
        seen["user"] = user
        return " It ships in October [00:10]. "

    segs = [_seg(i, "x" * 1000) for i in range(30)] + [_seg(30, "we ship in October")]
    out = await answer_live(complete, CopilotNotes(session_id="s1", decisions=["d"]), segs, "When?")
    assert out == "It ships in October [00:10]."
    assert "(most recent part)" in seen["user"] and "we ship in October" in seen["user"]
    assert seen["user"].endswith("Question: When?") and '"decisions":["d"]' in seen["user"]


@pytest.mark.anyio
async def test_runner_updates_on_enough_text_then_on_interval(ctx, fake_provider):
    from mnemosyne.jobs import JobContext

    session = ctx.repo.save(Session(name="live"))
    ctx.settings.default_provider = "fake"
    ctx.settings.copilot_interval_seconds = 30
    fake_provider.reply = json.dumps({"summary": ["so far"]})
    live = FakeLive()
    ctx.live[session.id] = live
    events = []
    job_ctx = JobContext.__new__(JobContext)
    job_ctx.update = lambda *a, **k: None
    job_ctx.emit = events.append
    task = asyncio.create_task(copilot_runner(ctx, session.id, tick=0.01)(job_ctx))
    await asyncio.sleep(0.05)
    assert events == []  # nothing said yet
    live.committed.append(_seg(0, "short"))
    await asyncio.sleep(0.05)
    assert events == []  # too little for the first notes
    live.committed.append(_seg(1, "y" * MIN_NEW_CHARS))
    for _ in range(100):
        if events:
            break
        await asyncio.sleep(0.01)
    assert events[0]["type"] == "copilot_notes" and events[0]["notes"]["lines"] == 2
    assert ctx.copilot_notes[session.id].summary == ["so far"]
    assert ctx.repo.get(session.id).copilot_notes.summary == ["so far"]  # saved as it goes
    live.committed.append(_seg(2, "z" * 2000))
    await asyncio.sleep(0.1)
    assert len(events) == 1  # more text, but the interval has not passed
    task.cancel()
    assert (await task)["updates"] == 1


def test_copilot_routes(client, ctx, fake_provider):
    from tests.conftest import drain_until_job

    s = ctx.repo.save(Session(name="m"))
    assert client.get(f"/api/sessions/{s.id}/copilot").json() is None
    ctx.copilot_notes[s.id] = CopilotNotes(session_id=s.id, summary=["x"])
    assert client.get(f"/api/sessions/{s.id}/copilot").json()["summary"] == ["x"]
    assert (
        client.post(f"/api/sessions/{s.id}/copilot/ask", json={"question": " "}).status_code == 400
    )
    live = FakeLive()
    live.committed = [_seg(0, "we ship in October")]
    ctx.live[s.id] = live
    ctx.settings.default_provider = "fake"
    fake_provider.reply = "October [00:00]."
    with client.websocket_connect("/ws") as ws:
        ws.receive_json()
        job = client.post(f"/api/sessions/{s.id}/copilot/ask", json={"question": "When?"}).json()
        drain_until_job(ws, job["id"])
    result = client.get(f"/api/jobs/{job['id']}").json()
    assert result["status"] == "completed" and result["result"]["answer"] == "October [00:00]."
    assert "we ship in October" in fake_provider.calls[-1]["transcript"]


def _notes(session_id, todos=(), decisions=()):
    return CopilotNotes(
        session_id=session_id,
        summary=["talked"],
        decisions=list(decisions),
        action_items=[CopilotItem(text=t, owner=o) for t, o in todos],
    )


def test_notes_outlive_the_backend(settings, ctx):
    from fastapi.testclient import TestClient

    from mnemosyne.api.app import create_app

    s = ctx.repo.save(Session(name="m"))
    ctx.repo.update_fields(s.id, copilot_notes=_notes(s.id, decisions=["ship it"]))
    ctx.repo.close()
    with TestClient(create_app(settings)) as client:  # a restarted backend, same data dir
        got = client.get(f"/api/sessions/{s.id}/copilot").json()
        assert got["decisions"] == ["ship it"]
        assert client.get(f"/api/sessions/{s.id}").json()["copilot_notes"]["summary"] == ["talked"]


def test_summary_gets_the_notes_and_keeps_missed_todos(client, ctx, fake_provider):
    from tests.conftest import run_summarize

    s = ctx.repo.save(Session(name="m", transcript=[_seg(0, "we should update the docs and ship")]))
    ctx.repo.update_fields(
        s.id,
        copilot_notes=_notes(
            s.id,
            todos=[("Update the docs", "Ana"), ("Book the venue", None)],
            decisions=["ship Friday"],
        ),
    )
    ctx.settings.default_provider = "fake"
    fake_provider.summary = json.dumps(
        {"summary": "- x", "action_items": [{"text": "Update the docs.", "owner": "Ana"}]}
    )
    run_summarize(client, s.id)

    prompt = fake_provider.calls[-1]["system_prompt"]
    assert "Notes an assistant took live" in prompt and "ship Friday" in prompt
    items = ctx.repo.get(s.id).summary_data.action_items
    assert [(i.text, i.live) for i in items] == [
        ("Update the docs.", False),
        ("Book the venue", True),  # heard live, missed by the summary
    ]
    run_summarize(client, s.id)  # re-summarizing does not add it twice
    assert len(ctx.repo.get(s.id).summary_data.action_items) == 2


def test_a_new_recording_starts_new_notes(client, ctx, fake_pipewire):
    s = ctx.repo.save(Session(name="m"))
    ctx.repo.update_fields(s.id, copilot_notes=_notes(s.id))
    client.post("/api/audio/start", json={"device_ids": [1], "session_id": s.id})
    assert ctx.repo.get(s.id).copilot_notes is None
    client.post(f"/api/audio/stop/{s.id}", json={"transcribe": False})
