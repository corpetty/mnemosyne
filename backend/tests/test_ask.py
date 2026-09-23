"""Ask across meetings: retrieval, prompt/citations, job, history."""

from datetime import datetime

from src.mnemosyne.models.ask import Passage, PassageLine
from src.mnemosyne.models.session import Session
from src.mnemosyne.models.transcript import TranscriptSegment
from src.mnemosyne.services.ask_service import (
    NO_RESULTS,
    build_citations,
    cited_numbers,
    format_passages,
)
from src.mnemosyne.storage.sqlite import fts_any_query

from tests.conftest import drain_until_job


def _seg(i, text, speaker="S"):
    return TranscriptSegment(text=text, speaker=speaker, start=i * 10.0, end=i * 10.0 + 5)


def _seed(ctx):
    a = Session(
        name="Release sync",
        created_at=datetime(2026, 9, 12, 10),
        transcript=[
            _seg(0, "Morning everyone"),
            _seg(1, "Let's talk about the Waku migration timeline", "Alice"),
            _seg(2, "We agreed the migration ships in October", "Bob"),
            _seg(3, "Docs need an update first", "Alice"),
            _seg(4, "Unrelated chatter about lunch"),
            _seg(5, "More unrelated chatter"),
            _seg(6, "Still nothing relevant"),
            _seg(7, "Budget for the offsite", "Bob"),
        ],
        summary="Decided to ship the Waku migration in October.",
    )
    b = Session(
        name="Hiring",
        created_at=datetime(2026, 9, 15, 14),
        transcript=[_seg(0, "Two candidates for the frontend role")],
    )
    ctx.repo.save(a)
    ctx.repo.save(b)
    return a, b


def test_fts_any_query_drops_stopwords():
    q = fts_any_query("What did we decide about the Waku migration last week?")
    assert q == '"decide"* OR "waku"* OR "migration"*'
    assert fts_any_query("what did we say?") == ""


def test_retrieve_merges_windows_and_includes_summaries(ctx):
    a, _ = _seed(ctx)
    passages = ctx.repo.retrieve("Waku migration", context=1)
    kinds = [(p.session_name, p.kind) for p in passages]
    assert ("Release sync", "transcript") in kinds and ("Release sync", "summary") in kinds
    t = next(p for p in passages if p.kind == "transcript")
    # hits on lines 1 and 2, each widened by 1 -> one merged window 0..3
    assert [ln.idx for ln in t.lines] == [0, 1, 2, 3]
    assert t.focus_idx in (1, 2)  # one of the lines that actually matched
    assert all(p.session_name != "Hiring" for p in passages)
    assert ctx.repo.retrieve("zebra quantum") == []


def test_format_and_citations():
    p1 = Passage(
        session_id="s1",
        session_name="Release sync",
        created_at=datetime(2026, 9, 12),
        lines=[
            PassageLine(idx=3, speaker="Al", start=50.0, text="Intro"),
            PassageLine(idx=4, speaker="Bob", start=65.0, text="Ships in October"),
        ],
        focus_idx=4,
    )
    p2 = Passage(
        session_id="s1",
        session_name="Release sync",
        created_at=datetime(2026, 9, 12),
        kind="summary",
        text="x" * 500,
    )
    block, used = format_passages([p1, p2])
    assert block.startswith(
        '[1] "Release sync" (2026-09-12) at 00:50\nAl: Intro\nBob: Ships in October'
    )
    assert '[2] Summary of "Release sync"' in block
    assert len(used) == 2
    assert format_passages([p1, p2], char_budget=10)[1] == [p1]  # first always included

    assert cited_numbers("Yes [2], per [1, 2] and [9].") == [2, 1, 9]
    cites = build_citations("Ships in October [1]. See [2][7].", used)
    assert [(c.n, c.idx, c.start) for c in cites] == [(1, 4, 65.0), (2, None, None)]
    assert cites[1].excerpt.endswith("…")
    assert cites[0].excerpt == "Bob: Ships in October"  # starts at the focus line


def _ask(client, question, **extra):
    with client.websocket_connect("/ws") as ws:
        ws.receive_json()
        resp = client.post("/api/ask", json={"question": question, **extra})
        assert resp.status_code == 200, resp.text
        job = resp.json()
        assert job["kind"] == "ask" and job["session_id"] is None
        drain_until_job(ws, job["id"])
    return client.get(f"/api/jobs/{job['id']}").json()


def test_ask_job_answers_with_citations_and_saves(client, ctx, fake_provider):
    _seed(ctx)
    fake_provider.reply = "It ships in October [1], as recorded in the summary [2]."
    job = _ask(client, "When does the Waku migration ship?", provider="fake")
    assert job["status"] == "completed", job["error"]
    ask = job["result"]
    assert ask["answer"].startswith("It ships in October")
    assert [c["n"] for c in ask["citations"]] == [1, 2]
    assert ask["citations"][0]["session_name"] == "Release sync"
    assert ask["provider"] == "fake" and ask["model"] == "fake-model-a"
    sent = fake_provider.calls[-1]
    assert "ONLY the numbered excerpts" in sent["system_prompt"]
    assert sent["transcript"].startswith("Question: When does the Waku migration ship?")
    assert "Waku migration" in sent["transcript"]

    history = client.get("/api/asks").json()
    assert [h["id"] for h in history] == [ask["id"]]
    assert client.delete(f"/api/asks/{ask['id']}").status_code == 200
    assert client.get("/api/asks").json() == []
    assert client.delete(f"/api/asks/{ask['id']}").status_code == 404


def test_ask_without_matches_skips_llm(client, ctx, fake_provider):
    _seed(ctx)
    job = _ask(client, "zebra quantum", provider="fake")
    assert job["result"]["answer"] == NO_RESULTS
    assert fake_provider.calls == []


def test_ask_validation_and_failures(client, ctx, fake_provider):
    assert client.post("/api/ask", json={"question": "  "}).status_code == 400
    assert client.post("/api/ask", json={"question": "x" * 2001}).status_code == 400
    _seed(ctx)
    job = _ask(client, "Waku migration", provider="nope")
    assert job["status"] == "failed" and "not available" in job["error"]


def test_passages_preview_endpoint(client, ctx):
    _seed(ctx)
    body = client.get("/api/ask/passages", params={"q": "offsite budget"}).json()
    assert body and body[0]["lines"][-1]["text"] == "Budget for the offsite"
