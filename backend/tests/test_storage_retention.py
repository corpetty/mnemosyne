"""Storage report, per-session audio deletion, retention cleanup."""

import asyncio
from datetime import datetime, timedelta

import pytest
from src.mnemosyne.models.session import Recording, Session
from src.mnemosyne.models.transcript import TranscriptSegment


def _with_audio(ctx, name, days_old, transcript=True, size=1000):
    s = Session(
        name=name,
        created_at=datetime.now() - timedelta(days=days_old),
        transcript=[TranscriptSegment(text="hi", speaker="S", start=0, end=1)]
        if transcript
        else [],
    )
    folder = ctx.settings.recordings_dir / s.id
    folder.mkdir(parents=True)
    mixed = folder / "x_mixed.ogg"
    mixed.write_bytes(b"a" * size)
    (folder / "x_device_1.ogg").write_bytes(b"b" * size)
    s.audio_file = str(mixed)
    s.recordings = [
        Recording(source="mic", device_id=1, device_name="m", path=str(folder / "x_device_1.ogg"))
    ]
    ctx.repo.save(s)
    return s


def test_report_lists_largest_and_totals(client, ctx):
    small = _with_audio(ctx, "small", 1, size=100)
    big = _with_audio(ctx, "big", 1, size=5000)
    client.post("/api/sessions", json={"name": "no audio"})
    body = client.get("/api/storage").json()
    assert body["recordings_bytes"] == 10200
    assert body["sessions_with_audio"] == 2
    assert [u["name"] for u in body["largest"]] == ["big", "small"]
    assert body["largest"][0]["audio_bytes"] == 10000
    assert body["database_bytes"] > 0 and body["retention_days"] == 0
    assert {s["name"]: s["has_audio"] for s in client.get("/api/sessions").json()} == {
        "small": True,
        "big": True,
        "no audio": False,
    }
    assert big.id and small.id


def test_delete_audio_keeps_transcript(client, ctx):
    s = _with_audio(ctx, "m", 1)
    resp = client.delete(f"/api/sessions/{s.id}/audio")
    assert resp.status_code == 200
    body = resp.json()
    assert body["audio_file"] is None and body["recordings"] == []
    assert len(body["transcript"]) == 1
    assert not (ctx.settings.recordings_dir / s.id).exists()
    assert client.get(f"/api/audio/file/{s.id}").status_code == 404
    assert client.delete("/api/sessions/nope/audio").status_code == 404


def test_delete_audio_refused_while_recording(client, ctx):
    s = _with_audio(ctx, "m", 1)
    ctx.active_recordings[s.id] = object()
    assert client.delete(f"/api/sessions/{s.id}/audio").status_code == 409
    ctx.active_recordings.clear()


def test_cleanup_only_old_transcribed_sessions(client, ctx):
    old = _with_audio(ctx, "old", 40)
    old_untranscribed = _with_audio(ctx, "old-raw", 40, transcript=False)
    recent = _with_audio(ctx, "recent", 2)

    assert client.post("/api/storage/cleanup").status_code == 400  # retention off
    dry = client.post("/api/storage/cleanup", params={"days": 30}).json()
    assert dry["dry_run"] is True
    assert [u["name"] for u in dry["sessions"]] == ["old"]
    assert dry["freed_bytes"] == 2000
    assert (ctx.settings.recordings_dir / old.id).exists()  # dry run touched nothing

    real = client.post("/api/storage/cleanup", params={"days": 30, "dry_run": False}).json()
    assert [u["name"] for u in real["sessions"]] == ["old"]
    assert not (ctx.settings.recordings_dir / old.id).exists()
    assert (ctx.settings.recordings_dir / old_untranscribed.id).exists()
    assert (ctx.settings.recordings_dir / recent.id).exists()
    assert len(client.get(f"/api/sessions/{old.id}").json()["transcript"]) == 1


def test_run_retention_uses_setting_and_skips_busy(ctx):
    a = _with_audio(ctx, "a", 40)
    b = _with_audio(ctx, "b", 40)
    assert ctx.run_retention() is None  # off by default
    ctx.settings.audio_retention_days = 30
    ctx.active_recordings[b.id] = object()
    result = ctx.run_retention()
    assert [u.name for u in result.sessions] == ["a"]
    assert not (ctx.settings.recordings_dir / a.id).exists()
    assert (ctx.settings.recordings_dir / b.id).exists()
    ctx.active_recordings.clear()


@pytest.mark.anyio
async def test_retention_loop_runs_on_startup(ctx):
    _with_audio(ctx, "a", 40)
    ctx.settings.audio_retention_days = 30
    await ctx.startup(retention_interval=3600)
    await asyncio.sleep(0.05)
    assert ctx.storage.report(30).sessions_with_audio == 0
    ctx._retention_task.cancel()
