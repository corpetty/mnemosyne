"""Semantic search: chunking, vector index, hybrid fusion, background indexer."""

import asyncio
from datetime import datetime

import pytest

from mnemosyne.events import EventBus
from mnemosyne.models.session import Session, SummaryData
from mnemosyne.models.transcript import TranscriptSegment
from mnemosyne.search.embeddings import HashEmbedder
from mnemosyne.search.hybrid import hybrid_passages
from mnemosyne.search.index import VectorIndex, chunk_session


def _seg(i, text, speaker="S"):
    return TranscriptSegment(text=text, speaker=speaker, start=i * 10.0, end=i * 10.0 + 5)


def _seed(ctx):
    infra = Session(
        name="Infra weekly",
        created_at=datetime(2026, 9, 22, 15),
        transcript=[
            _seg(0, "Morning all"),
            _seg(1, "Our cloud costs went up eighteen percent this quarter"),
            _seg(2, "Mostly egress and storage"),
        ],
        summary="Costs are up; move store nodes.",
        summary_data=SummaryData(decisions=["Move store nodes after rc1"]),
    )
    hiring = Session(
        name="Hiring",
        created_at=datetime(2026, 9, 23, 10),
        transcript=[_seg(0, "The candidate knows Svelte well")],
    )
    ctx.repo.save(infra)
    ctx.repo.save(hiring)
    for s in (infra, hiring):
        ctx.index.index_session(s.id)
    return infra, hiring


def test_chunk_session():
    s = Session(
        name="n",
        transcript=[_seg(i, "x" * 100) for i in range(14)],
        summary="sum",
        summary_data=SummaryData(decisions=["d"]),
    )
    chunks = chunk_session(s)
    lines = [c for c in chunks if c.kind == "lines"]
    assert [(c.first, c.last) for c in lines] == [(0, 5), (6, 11), (12, 13)]  # 600 chars each
    summary = [c for c in chunks if c.kind == "summary"][0]
    assert summary.text == "n\nsum\nd"
    assert chunk_session(Session(name="empty")) == []


def test_index_and_query(ctx):
    infra, hiring = _seed(ctx)
    # "budget" is a synonym of "costs" in the test embedder; FTS would not find it.
    hits = ctx.index.query("the budget?", k=5)
    assert hits and hits[0].session_id == infra.id
    assert ctx.index.index_session(infra.id) == 0  # nothing changed: not re-embedded
    status = ctx.index.status()
    assert status.indexed_sessions == 2 and status.total_sessions == 2 and status.ready


def test_model_change_clears_vectors(ctx):
    _seed(ctx)
    ctx.repo.set_meta("embedder", "something-else")
    fresh = VectorIndex(ctx.repo, ctx.settings, lambda s: HashEmbedder(dim=128))
    assert fresh.embedder() is not None
    assert ctx.repo.indexed_session_count() == 0


def test_broken_embedder_disables_quietly(ctx):
    def boom(settings):
        raise OSError("no network")

    idx = VectorIndex(ctx.repo, ctx.settings, boom)
    assert idx.query("anything") == []
    assert "no network" in idx.status().error


def test_hybrid_search_adds_semantic_matches(client, ctx):
    infra, _ = _seed(ctx)
    assert client.get("/api/search", params={"q": "budget", "mode": "keyword"}).json() == []
    hits = client.get("/api/search", params={"q": "budget"}).json()
    assert hits[0]["session_id"] == infra.id and hits[0]["match"] == "semantic"
    seg = hits[0]["segments"][0]
    assert seg["semantic"] is True and "costs" in seg["snippet"]
    both = client.get("/api/search", params={"q": "costs"}).json()
    assert both[0]["match"] == "both"


def test_hybrid_passages_for_ask(ctx):
    infra, _ = _seed(ctx)
    passages = hybrid_passages(ctx.repo, ctx.index, "What about spending?")
    texts = [
        " ".join(ln.text for ln in p.lines) if p.kind == "transcript" else p.text for p in passages
    ]
    assert any("cloud costs" in t for t in texts)
    # Keyword and semantic hits on the same window are fused, not duplicated.
    both = hybrid_passages(ctx.repo, ctx.index, "costs")
    windows = [(p.session_id, p.lines[0].idx) for p in both if p.kind == "transcript"]
    assert len(windows) == len(set(windows))


def test_index_status_and_rebuild(client, ctx):
    _seed(ctx)
    assert client.get("/api/search/index").json()["indexed_sessions"] == 2
    r = client.post("/api/search/index/rebuild").json()
    assert r["indexed_sessions"] == 0 and r["pending"] == 2


@pytest.mark.anyio
async def test_indexer_follows_session_events(ctx):
    bus = EventBus()
    idx = VectorIndex(ctx.repo, ctx.settings, lambda s: HashEmbedder(synonyms=[{"ship", "launch"}]))
    idx.start(bus, debounce=0)
    await asyncio.sleep(0.05)  # let the follower subscribe
    s = ctx.repo.save(Session(name="later", transcript=[_seg(0, "we will launch in October")]))
    bus.publish({"type": "session", "session_id": s.id, "status": "completed"})
    for _ in range(100):
        if ctx.repo.vector_hashes(s.id):
            break
        await asyncio.sleep(0.01)
    assert ctx.repo.vector_hashes(s.id)
    assert idx.query("ship")[0].session_id == s.id
    await idx.stop()


def test_failed_model_load_is_retried(ctx, monkeypatch):
    from mnemosyne.search import index as index_mod

    calls = {"n": 0}

    def flaky(settings):
        calls["n"] += 1
        if calls["n"] == 1:
            raise OSError("offline")
        return HashEmbedder()

    idx = VectorIndex(ctx.repo, ctx.settings, flaky)
    assert idx.embedder() is None and "offline" in idx.status().error
    assert idx.embedder() is None and calls["n"] == 1  # not retried right away
    monkeypatch.setattr(index_mod, "RETRY_SECONDS", 0)
    assert idx.embedder() is not None and idx.status().error is None


def test_unchanged_sessions_are_skipped_cheaply(ctx, monkeypatch):
    infra, _ = _seed(ctx)
    loads = []
    real_get = ctx.repo.get
    monkeypatch.setattr(ctx.repo, "get", lambda sid: loads.append(sid) or real_get(sid))
    assert ctx.index.index_session(infra.id) == 0
    assert loads == []  # skipped on the stamp, without loading the transcript
    ctx.repo.update_fields(infra.id, name="Infra weekly (renamed)")
    loads.clear()  # update_fields reads the session back itself
    assert ctx.index.index_session(infra.id) == 2  # the summary chunk includes the name
    assert loads == [infra.id]
