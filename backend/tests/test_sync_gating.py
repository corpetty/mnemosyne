"""A meeting recorded on another computer: its audio is not here, so the work that needs the
audio is refused (409 naming the computer) or skipped, and the automation runs only where it was
recorded (docs/plans/2026-10-14-library-sync-rules.md, 3 and 4)."""

import asyncio
import json
import os
import time
from datetime import datetime

import pytest
from fastapi import HTTPException

from mnemosyne.api.routes.export import before_export, export_session
from mnemosyne.models.session import (
    ActionItem,
    Recording,
    Session,
    SessionStatus,
    SummaryData,
    TranscriptSegment,
)
from mnemosyne.services import encryption, history, hubspot, origin, pipeline, records, recovery
from mnemosyne.services.parts import add_part
from mnemosyne.services.vault_sync import sync_vault

MSG = "The audio is on Laptop"


@pytest.fixture(autouse=True)
def laptop(ctx):
    path = ctx.settings.data_dir / "sync_peers.json"
    path.write_text(
        json.dumps(
            {"self": {"id": "me", "name": "Here"}, "peers": [{"id": "peer-1", "name": "Laptop"}]}
        )
    )


def _save(ctx, tmp_path, name="Standup", foreign=True, audio=True, summary=False) -> Session:
    audio_file = None
    recordings = []
    if audio:
        audio_file = tmp_path / f"{name}.ogg"
        audio_file.write_bytes(b"ogg")
        recordings = [Recording(source="mic", device_id=0, device_name="Mic", path=str(audio_file))]
    s = Session(
        name=name,
        created_at=datetime(2026, 10, 12, 9),
        audio_file=str(audio_file) if audio_file else None,
        recordings=recordings,
        transcript=[
            TranscriptSegment(text="We ship Friday.", speaker="Ana", start=0.5, end=4.0),
            TranscriptSegment(text="Friday works.", speaker="Bo", start=4.5, end=8.0),
        ],
    )
    if summary:
        s.summary = "s"
        s.summary_data = SummaryData(action_items=[ActionItem(id="a1", text="Write the guide")])
    ctx.repo.save(s)
    if foreign:
        ctx.repo.set_sync_state(s.id, {"peer-1": 1}, "peer-1")
    return ctx.sessions.get_session(s.id)


def _detail(r) -> str:
    return r.json()["detail"]


# ---- names -----------------------------------------------------------------------------------


def test_origin_name_and_fallbacks(ctx, tmp_path):
    foreign = _save(ctx, tmp_path)
    here = _save(ctx, tmp_path, "Here", foreign=False)
    assert origin.is_foreign(foreign) and not origin.is_foreign(here)
    assert origin.origin_name(ctx, foreign) == "Laptop"
    ctx.repo.set_sync_state(foreign.id, {}, "unknown-peer")
    assert origin.origin_name(ctx, ctx.sessions.get_session(foreign.id)) == "another computer"
    (ctx.settings.data_dir / "sync_peers.json").unlink()
    assert origin.origin_name(ctx, foreign) == "another computer"


# ---- 409s ------------------------------------------------------------------------------------


def test_playing_the_audio(client, ctx, tmp_path):
    foreign = _save(ctx, tmp_path)
    here = _save(ctx, tmp_path, "Here", foreign=False)
    r = client.get(f"/api/audio/file/{foreign.id}")
    assert r.status_code == 409 and _detail(r) == MSG
    assert client.get(f"/api/audio/file/{here.id}").status_code == 200


def test_transcribing(client, ctx, tmp_path):
    foreign = _save(ctx, tmp_path)
    r = client.post(f"/api/sessions/{foreign.id}/transcribe")
    assert r.status_code == 409 and _detail(r) == MSG
    with pytest.raises(HTTPException) as e:  # every other submit site goes through this
        pipeline.transcribe_session(ctx, foreign.id)
    assert e.value.status_code == 409 and e.value.detail == MSG
    here = _save(ctx, tmp_path, "Here", foreign=False)
    assert callable(pipeline.transcribe_session(ctx, here.id))


def test_recording_into_it(client, ctx, tmp_path):
    foreign = _save(ctx, tmp_path)
    r = client.post("/api/audio/start", json={"device_ids": [1], "session_id": foreign.id})
    assert r.status_code == 409 and _detail(r) == MSG
    r = client.post(
        "/api/audio/start-browser",
        json={"session_id": foreign.id, "sources": [{"device_id": -1}]},
    )
    assert r.status_code in (409, 422)


def test_importing_a_part_into_it(client, ctx, tmp_path):
    foreign = _save(ctx, tmp_path)
    r = client.post(
        "/api/audio/import",
        data={"session_id": foreign.id},
        files={"file": ("a.wav", b"RIFF", "audio/wav")},
    )
    assert r.status_code == 409 and _detail(r) == MSG


def test_add_part(ctx, tmp_path):
    foreign = _save(ctx, tmp_path)
    with pytest.raises(HTTPException) as e:
        asyncio.run(add_part(ctx, foreign.id, 1, [], tmp_path / "x.ogg"))
    assert e.value.detail == MSG


def test_combine(client, ctx, tmp_path):
    foreign = _save(ctx, tmp_path)
    here = _save(ctx, tmp_path, "Here", foreign=False)
    for a, b in ((foreign, here), (here, foreign)):
        r = client.post(f"/api/sessions/{a.id}/combine", json={"other_id": b.id})
        assert r.status_code == 409 and _detail(r) == MSG


def test_clips_and_quotes(client, ctx, tmp_path):
    foreign = _save(ctx, tmp_path)
    r = client.post(f"/api/sessions/{foreign.id}/clip", json={"first_idx": 0, "last_idx": 1})
    body = r.json()
    assert r.status_code == 200 and body["clip"] is None and "We ship Friday." in body["text"]
    r = client.get(f"/api/sessions/{foreign.id}/clips/0-1")
    assert r.status_code == 409 and _detail(r) == MSG
    r = client.post(
        f"/api/sessions/{foreign.id}/clips/0-1/save", json={"path": str(tmp_path / "c.ogg")}
    )
    assert r.status_code == 409 and _detail(r) == MSG


def test_history_recover_and_build(client, ctx, tmp_path):
    foreign = _save(ctx, tmp_path)
    r = client.post(f"/api/sessions/{foreign.id}/recover")
    assert r.status_code == 409 and _detail(r) == MSG
    # A file that is not on this disk is not "missing", and the parts still come from rows.
    for p in (tmp_path / "Standup.ogg",):
        p.unlink()
    (ctx.settings.recordings_dir / foreign.id).mkdir(parents=True)
    (ctx.settings.recordings_dir / foreign.id / "stray.ogg").write_bytes(b"x")
    built = history.build(ctx, foreign.id)
    assert built.missing == [] and built.orphans == [] and built.pending == []
    assert [p.part for p in built.parts] == [0] and built.parts[0].seconds is None
    here = _save(ctx, tmp_path, "Here", foreign=False)
    here_path = tmp_path / "Here.ogg"
    here_path.unlink()
    assert set(history.build(ctx, here.id).missing) == {"Here.ogg"}


def test_storage(client, ctx, tmp_path):
    foreign = _save(ctx, tmp_path)
    here = _save(ctx, tmp_path, "Here", foreign=False)
    for s in (foreign, here):
        folder = ctx.settings.recordings_dir / s.id
        folder.mkdir(parents=True)
        (folder / "a.ogg").write_bytes(b"x" * 100)
    r = client.delete(f"/api/sessions/{foreign.id}/audio")
    assert r.status_code == 409 and _detail(r) == MSG
    assert (ctx.settings.recordings_dir / foreign.id / "a.ogg").exists()
    report = ctx.storage.report(30)
    assert [u.session_id for u in report.largest] == [here.id] and report.sessions_with_audio == 1
    old = datetime(2027, 1, 1)
    expired = ctx.storage.expired(30, now=old)
    assert [u.session_id for u in expired] == [here.id]
    done = ctx.storage.cleanup(30, now=old)
    assert [u.session_id for u in done.sessions] == [here.id]
    assert (ctx.settings.recordings_dir / foreign.id / "a.ogg").exists()


def test_records(ctx, tmp_path):
    foreign = _save(ctx, tmp_path, summary=True)
    here = _save(ctx, tmp_path, "Here", foreign=False, summary=True)
    assert records.audio_files(foreign) == [] and records.audio_hashes(foreign) == {}
    assert len(records.audio_files(here)) == 1
    # A seal made where it was recorded names audio files that are not here.
    ctx.repo.add_seal(
        foreign.id,
        "summarized",
        records.content_hash(records.content_of(ctx.repo.get(foreign.id))),
        {"Standup.ogg": "abc"},
    )
    v = records.verify(ctx, foreign.id)
    assert v.ok and v.seals == 1 and v.problems == []


# ---- automation ------------------------------------------------------------------------------


def test_startup_resets_and_recovery_skip_it(ctx, tmp_path):
    foreign = _save(ctx, tmp_path)
    here = _save(ctx, tmp_path, "Here", foreign=False)
    for s in (foreign, here):
        ctx.sessions.set_status(s.id, SessionStatus.RECORDING)
    assert recovery.interrupted_sessions(ctx) == [here.id]


def test_auto_export_and_pull_back_are_origin_only(ctx, tmp_path):
    vault = tmp_path / "vault"
    vault.mkdir()
    st = ctx.settings
    st.obsidian_vault_path = str(vault)
    st.obsidian_subfolder = "meetings"
    st.obsidian_people_notes = False
    st.obsidian_daily_notes = False
    foreign = _save(ctx, tmp_path, summary=True)
    here = _save(ctx, tmp_path, "Here", foreign=False, summary=True)
    assert pipeline._auto_export(ctx, foreign.id) is None
    assert not list(vault.rglob("*.md"))
    assert pipeline._auto_export(ctx, here.id)
    assert len(list(vault.rglob("*.md"))) == 1
    # Manual export still writes a foreign meeting's note.
    path = export_session(ctx, foreign, str(vault))
    assert path.exists()
    # A tick in the note comes back for the meeting recorded here, not for the other.
    for name in ("Standup", "Here"):
        note = next(vault.rglob(f"*{name}*.md"))
        text = note.read_text(encoding="utf-8")
        assert "- [ ] Write the guide" in text
        note.write_text(
            text.replace("- [ ] Write the guide", "- [x] Write the guide"), encoding="utf-8"
        )
        t = time.time() + 5
        os.utime(note, (t, t))
    assert sync_vault(ctx) == 1
    assert ctx.sessions.get_session(here.id).summary_data.action_items[0].done
    assert not ctx.sessions.get_session(foreign.id).summary_data.action_items[0].done
    before_export(ctx, foreign, str(vault))
    assert not ctx.sessions.get_session(foreign.id).summary_data.action_items[0].done


def test_export_all_lists_only_origin_meetings(client, ctx, tmp_path):
    vault = tmp_path / "vault"
    vault.mkdir()
    ctx.settings.obsidian_vault_path = str(vault)
    ctx.settings.obsidian_people_notes = False
    ctx.settings.obsidian_daily_notes = False
    _save(ctx, tmp_path, summary=True)
    _save(ctx, tmp_path, "Here", foreign=False, summary=True)
    job = client.post("/api/export/obsidian/all").json()
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        got = client.get(f"/api/jobs/{job['id']}").json()
        if got["status"] in ("completed", "failed"):
            break
        time.sleep(0.05)
    assert got["result"]["total"] == 1


def test_hubspot_auto_push_skips(ctx, tmp_path, monkeypatch):
    ctx.settings.hubspot_auto_push = True
    ctx.settings.hubspot_token = "t"
    monkeypatch.setattr(hubspot, "load_state", lambda app, sid: type("S", (), {"contacts": [1]})())
    pushed = []

    async def push(app, session, auto=False):
        pushed.append(session.id)
        return type("R", (), {"message": "pushed"})()

    monkeypatch.setattr(hubspot, "push_session", push)
    foreign = _save(ctx, tmp_path, summary=True)
    here = _save(ctx, tmp_path, "Here", foreign=False, summary=True)
    assert asyncio.run(hubspot.auto_push(ctx, foreign.id)) is None
    assert asyncio.run(hubspot.auto_push(ctx, here.id)) == "pushed"
    assert pushed == [here.id]


def test_encryption_leaves_foreign_audio_alone(ctx, tmp_path):
    foreign = _save(ctx, tmp_path)
    assert encryption._convert_audio(ctx, foreign.id, b"k" * 32) == (0, [])
    assert not (tmp_path / "Standup.ogg.enc").exists()
