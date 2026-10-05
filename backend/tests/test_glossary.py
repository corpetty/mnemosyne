"""Glossary parsing, corrections, LLM correction guard rails, and pipeline integration."""

import json

import pytest

from mnemosyne.models.transcript import TranscriptSegment
from mnemosyne.transcription.glossary import (
    apply_corrections,
    glossary_instructions,
    initial_prompt,
    llm_correct,
    parse_glossary,
)
from tests.conftest import drain_until_job, run_summarize
from tests.fakes import FakeEngine

TEXT = """
# names and projects
Waku
Nimbus
walk you -> Waku
corey petty -> Corey Petty   # full name
 -> nothing
Waku
"""


def seg(text):
    return TranscriptSegment(text=text, speaker="S", start=0, end=1)


def test_parse():
    g = parse_glossary(TEXT)
    assert g.terms == ["Waku", "Nimbus", "Corey Petty"]
    assert len(g.corrections) == 2
    assert parse_glossary("").empty and parse_glossary("# only a comment").empty


def test_apply_corrections_whole_words_case_insensitive():
    g = parse_glossary(TEXT)
    out, n = apply_corrections(
        [seg("We shipped Walk You relay"), seg("walkyou stays"), seg("ask corey petty")], g
    )
    assert [s.text for s in out] == ["We shipped Waku relay", "walkyou stays", "ask Corey Petty"]
    assert n == 2
    same, n = apply_corrections([seg("x")], parse_glossary("Waku"))
    assert n == 0 and same[0].text == "x"


def test_prompts():
    g = parse_glossary(TEXT)
    assert initial_prompt(g) == "Glossary: Waku, Nimbus, Corey Petty."
    assert initial_prompt(parse_glossary("")) is None
    assert "exactly as written: Waku, Nimbus, Corey Petty" in glossary_instructions(g)
    assert glossary_instructions(parse_glossary("")) == ""


@pytest.mark.anyio
async def test_llm_correct_accepts_fixes_and_rejects_rewrites():
    g = parse_glossary("Waku\nNimbus")
    segs = [seg("the wah coo relay is ready"), seg("nimbus sync is slow"), seg("lunch was good")]

    async def complete(system, user):
        assert "Glossary:\n- Waku\n- Nimbus" in user and "0: the wah coo relay" in user
        return "Here you go: " + json.dumps(
            [
                {"i": 0, "text": "the Waku relay is ready"},
                {"i": 1, "text": "Nimbus sync is slow"},
                {"i": 2, "text": "The team discussed food options at length today"},  # rewrite
                {"i": 9, "text": "out of range"},
            ]
        )

    out, n = await llm_correct(segs, g, complete)
    assert [s.text for s in out] == [
        "the Waku relay is ready",
        "Nimbus sync is slow",
        "lunch was good",
    ]
    assert n == 2


@pytest.mark.anyio
async def test_llm_correct_survives_bad_replies_and_batches():
    g = parse_glossary("Waku")
    calls = []

    async def complete(system, user):
        calls.append(user)
        if len(calls) == 1:
            raise RuntimeError("model down")
        return "not json at all"

    segs = [seg(f"line {i}") for i in range(5)]
    out, n = await llm_correct(segs, g, complete, batch_size=3)
    assert n == 0 and out == segs and len(calls) == 2


def test_pipeline_applies_glossary_and_llm(client, ctx, fake_provider):
    ctx.settings.glossary = "walk you -> Waku\nNimbus"
    ctx.settings.glossary_llm_correct = True
    ctx.settings.default_provider = "fake"
    fake_provider.reply = json.dumps([{"i": 1, "text": "Nimbus sync is slow"}])
    ctx.models._engine = FakeEngine(
        segments=[
            TranscriptSegment(text="walk you relay is up", speaker="SPEAKER_00", start=0, end=1),
            TranscriptSegment(text="nimbus sink is slow", speaker="SPEAKER_01", start=1, end=2),
        ]
    )
    sid = client.post("/api/sessions", json={}).json()["id"]
    ctx.sessions.set_audio(sid, "/fake.ogg", [])
    with client.websocket_connect("/ws") as ws:
        ws.receive_json()
        job = client.post(f"/api/sessions/{sid}/transcribe").json()
        drain_until_job(ws, job["id"])
    texts = [s["text"] for s in client.get(f"/api/sessions/{sid}").json()["transcript"]]
    assert texts == ["Waku relay is up", "Nimbus sync is slow"]
    assert client.get(f"/api/jobs/{job['id']}").json()["result"]["glossary_fixes"] == 2


def test_glossary_reaches_summary_and_ask(client, ctx, fake_provider, transcribed_session):
    ctx.settings.glossary = "Waku"
    run_summarize(client, transcribed_session["id"], {"provider": "fake"})
    assert "exactly as written: Waku" in fake_provider.calls[-1]["system_prompt"]
    with client.websocket_connect("/ws") as ws:
        ws.receive_json()
        job = client.post(
            "/api/ask", json={"question": "hello everyone", "provider": "fake"}
        ).json()
        drain_until_job(ws, job["id"])
    assert "exactly as written: Waku" in fake_provider.calls[-1]["system_prompt"]


@pytest.mark.anyio
async def test_llm_correct_asks_batches_side_by_side_and_counts_them():
    import asyncio

    g = parse_glossary("Waku")
    running, most, progress = 0, 0, []

    async def complete(system, user):
        nonlocal running, most
        running += 1
        most = max(most, running)
        await asyncio.sleep(0.01)
        running -= 1
        return "[]"

    segs = [seg(f"line {i}") for i in range(10)]
    out, n = await llm_correct(
        segs, g, complete, batch_size=2, parallel=3, on_batch=lambda d, t: progress.append((d, t))
    )
    assert out == segs and n == 0
    assert most == 3  # 5 batches, 3 at a time
    assert progress == [(1, 5), (2, 5), (3, 5), (4, 5), (5, 5)]


def test_pipeline_asks_for_no_thinking(client, ctx, fake_provider):
    """A reasoning model took 20-150 s a glossary batch thinking first; 1-3 s without."""
    ctx.settings.glossary = "Nimbus"
    ctx.settings.glossary_llm_correct = True
    ctx.settings.default_provider = "fake"
    fake_provider.reply = "[]"
    ctx.models._engine = FakeEngine(
        segments=[TranscriptSegment(text="nimbus", speaker="SPEAKER_00", start=0, end=1)]
    )
    sid = client.post("/api/sessions", json={}).json()["id"]
    ctx.sessions.set_audio(sid, "/fake.ogg", [])
    with client.websocket_connect("/ws") as ws:
        ws.receive_json()
        job = client.post(f"/api/sessions/{sid}/transcribe").json()
        drain_until_job(ws, job["id"])
    assert [c["think"] for c in fake_provider.calls] == [False]


def test_a_stop_in_the_name_check_keeps_the_transcript(client, ctx, fake_provider):
    """The transcription is done before the LLM checks names: an app restart (2026-10-05) or
    Cancel then must not throw it away."""
    import asyncio
    import time

    ctx.settings.glossary = "Nimbus"
    ctx.settings.glossary_llm_correct = True
    ctx.settings.default_provider = "fake"

    async def forever(*args, **kwargs):
        await asyncio.Event().wait()

    fake_provider.complete = forever
    ctx.models._engine = FakeEngine(
        segments=[TranscriptSegment(text="nimbus sink", speaker="SPEAKER_00", start=0, end=1)]
    )
    sid = client.post("/api/sessions", json={}).json()["id"]
    ctx.sessions.set_audio(sid, "/fake.ogg", [])
    job = client.post(f"/api/sessions/{sid}/transcribe").json()
    deadline = time.monotonic() + 10
    while "Checking names" not in client.get(f"/api/jobs/{job['id']}").json()["message"]:
        assert time.monotonic() < deadline
        time.sleep(0.05)
    assert client.portal.call(ctx.jobs.cancel, job["id"])
    client.portal.call(ctx.jobs.wait, job["id"])
    session = client.get(f"/api/sessions/{sid}").json()
    assert [s["text"] for s in session["transcript"]] == ["nimbus sink"]
    assert session["status"] == "completed"


@pytest.mark.anyio
async def test_providers_switch_thinking_off(monkeypatch):
    import httpx

    from mnemosyne.summarization.ollama import OllamaProvider
    from mnemosyne.summarization.vllm import VLLMProvider

    bodies, old_ollama = [], False

    def handler(request):
        body = json.loads(request.content)
        bodies.append(body)
        if request.url.path == "/api/chat":
            if old_ollama and "think" in body:
                return httpx.Response(400, json={"error": "unknown field"})
            return httpx.Response(200, json={"message": {"content": "ok"}})
        return httpx.Response(200, json={"choices": [{"message": {"content": "ok"}}]})

    real = httpx.AsyncClient
    monkeypatch.setattr(
        httpx, "AsyncClient", lambda **kw: real(transport=httpx.MockTransport(handler), **kw)
    )
    vllm = VLLMProvider("http://vllm")
    assert await vllm.complete("s", "u", "m", think=False) == "ok"
    assert bodies[-1]["chat_template_kwargs"] == {"enable_thinking": False}
    await vllm.complete("s", "u", "m")
    assert "chat_template_kwargs" not in bodies[-1]

    ollama = OllamaProvider("http://ollama")
    assert await ollama.complete("s", "u", "m", think=False) == "ok"
    assert bodies[-1]["think"] is False
    old_ollama = True  # answers 400 to the field: asked again without it
    bodies.clear()
    assert await ollama.complete("s", "u", "m", think=False) == "ok"
    assert ["think" in b for b in bodies] == [True, False]
