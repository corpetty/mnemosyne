"""Demo mode (used by the browser tests): builds from settings and speaks every prompt type."""

import pytest

from mnemosyne import demo
from mnemosyne.config import Settings
from mnemosyne.services.summarization_service import SummarizationService
from mnemosyne.summarization.prompts import get_system_prompt, parse_summary_response
from mnemosyne.transcription.registry import build_engine


@pytest.mark.anyio
async def test_demo_engine_yields_two_speakers(tmp_path):
    engine = build_engine(Settings(transcriber="demo", diarizer="demo"))
    from mnemosyne.transcription.engine import AudioSource

    audio = tmp_path / "a.wav"
    audio.write_bytes(b"")
    segs = [s async for s in engine.transcribe_sources([AudioSource(path=str(audio))])]
    assert [s.text for s in segs] == [t for *_, t in demo.SCRIPT]
    assert {s.speaker for s in segs} == {"SPEAKER_00", "SPEAKER_01"}


@pytest.mark.anyio
async def test_demo_provider_only_when_enabled(monkeypatch):
    assert "demo" not in SummarizationService(Settings()).providers
    monkeypatch.setenv("MNEMOSYNE_DEMO", "1")
    service = SummarizationService(Settings())
    assert list(service.providers) == ["demo"]
    raw = await service.providers["demo"].summarize("x", "demo-model", get_system_prompt(3))
    summary, data = parse_summary_response(raw)
    assert data.title == "Release planning"
    assert data.decisions == ["Ship the Waku migration in October"]
    assert data.action_items[0].owner == "SPEAKER_01"


# ---- the demo share: Ada and a meeting she shared (services/share_demo.py) ------------------


def _seeded(client):
    from mnemosyne.services import share_demo

    ctx = client.app.state.ctx
    return ctx, ctx.repo.contact(share_demo.ada_person())


def test_demo_seeds_ada_and_her_shared_meeting(monkeypatch, settings, keystore):
    from fastapi.testclient import TestClient

    from mnemosyne.api.app import create_app
    from mnemosyne.services import share_demo

    monkeypatch.setenv("MNEMOSYNE_DEMO", "1")
    with TestClient(create_app(settings, keystore=keystore)) as client:
        ctx, row = _seeded(client)
        assert row is not None and row["name"] == "Ada Lovelace"
        assert row["card"]["machines"][0]["name"] == "Ada's laptop"
        s = ctx.repo.get(share_demo.MEETING_ID)
        assert s.name == "Shared: Waku roadmap"
        assert s.shared_from == share_demo.ada_person()
        assert len(s.summary_data.decisions) == 2
        assert len(s.summary_data.action_items) == 2
        assert s.transcript == []
    # A second start changes nothing (and does not fail).
    with TestClient(create_app(settings, keystore=keystore)) as client:
        ctx, row2 = _seeded(client)
        assert row2["created_at"] == row["created_at"]
        assert len(ctx.repo.contacts()) == 1
        assert [x.id for x in ctx.sessions.list_sessions()] == [share_demo.MEETING_ID]


def test_demo_share_signature_verifies(monkeypatch, settings, keystore):
    from fastapi.testclient import TestClient

    from mnemosyne.api.app import create_app
    from mnemosyne.models.shared_meeting import SharedMeeting
    from mnemosyne.services import share_demo, shares

    monkeypatch.setenv("MNEMOSYNE_DEMO", "1")
    ada = share_demo.ada_person()
    sm = SharedMeeting(
        id="ada000000001",
        name="x",
        created_at="2026-10-20T14:00:00Z",
        updated_at="2026-10-20T14:00:00Z",
        owner={"person": ada, "name": "Ada"},
        parts={},
    )
    sm.digest = shares.digest_of(sm)
    sm.sig = share_demo._sign(shares._canonical(sm, {"sig"}))
    assert shares.verify_shared_meeting(sm, ada)
    with TestClient(create_app(settings, keystore=keystore)) as client:
        assert client.app.state.ctx.repo.contact(ada) is not None


def test_no_demo_share_without_demo_mode(client, ctx):
    from mnemosyne.services import share_demo

    assert ctx.repo.contact(share_demo.ada_person()) is None
    assert ctx.sessions.list_sessions() == []
    assert share_demo.seed_demo(ctx) is False
