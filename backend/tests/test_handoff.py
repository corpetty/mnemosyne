"""A laptop that uses a main computer (docs/plans/2026-11-09-connections-design.md, task 6): the
laptop's backend learns the main computer from its shell (PUT /api/system/main, open to the
desktop app only), and the main computer takes recordings handed over to it
(POST /api/connections/handoff, paired computers only). Two apps in this process over
httpx.ASGITransport, as tests/test_sync_*.py do. No audio is played or decoded: the WAVs are a
header and silence, encoding and mixing are the conftest fakes (`fake_pipewire`)."""

import json
from pathlib import Path

import httpx
import pytest

from mnemosyne.api.app import create_app
from mnemosyne.config import Settings
from mnemosyne.services import parts, records, shared_notes
from mnemosyne.services.pairing import PHONE
from mnemosyne.services.storage_service import ErasureNotSealed
from mnemosyne.storage.crypto import MemoryKeyStore

pytestmark = pytest.mark.anyio

LOOPBACK = ("127.0.0.1", 50000)
TUNNEL = ("127.0.0.2", 50000)  # what the link sidecar's tunnel connects from (auth.py)


def wav(seconds: float = 0.5, rate: int = 16000) -> bytes:
    """A 16-bit mono WAV of silence, sizes filled in."""
    import struct

    data = b"\x00\x00" * int(rate * seconds)
    fmt = struct.pack("<IHHIIHH", 16, 1, 1, rate, rate * 2, 2, 16)
    return (
        b"RIFF"
        + struct.pack("<I", 36 + len(data))
        + b"WAVEfmt "
        + fmt
        + b"data"
        + (struct.pack("<I", len(data)) + data)
    )


def client(app, token: str | None = None, peer=LOOPBACK, host="127.0.0.1:8008"):
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app, client=peer),
        base_url=f"http://{host}",
        headers=headers,
    )


@pytest.fixture
def settings(settings):
    """The main computer: something was paired, so it has an API token (pairing_prep)."""
    settings.api_token = "admin-main"
    settings.live_transcription = False  # no live job behind a streamed recording
    settings.copilot = False
    return settings


@pytest.fixture
def laptop(tmp_path):
    settings = Settings(
        data_dir=tmp_path / "laptop" / "data",
        backup_dir=str(tmp_path / "laptop" / "backups"),
        use_our_servers=False,
        api_token="admin-laptop",
    )
    return create_app(settings, keystore=MemoryKeyStore())


@pytest.fixture
def laptop_token(ctx):
    """The token the main computer gave the laptop at pairing: a DESKTOP device's."""
    _, token = ctx.pairing.add_desktop("Corey's laptop", "e" * 64)
    return token


@pytest.fixture
def timeline(monkeypatch):
    """The fake Ogg files have no length: each part counts as 3 s, joined as bytes."""
    monkeypatch.setattr(parts, "audio_seconds", lambda path, key: 3.0)

    def join(first, second, out, key):
        Path(out).write_bytes(Path(first).read_bytes() + Path(second).read_bytes())
        return out

    monkeypatch.setattr(parts, "join", join)


def handoff_form(meta: dict, files: dict[str, bytes] | None = None):
    sources = meta.get("sources", [])
    files = files or {s["file"]: wav() for s in sources}
    return {
        "data": {"meta": json.dumps(meta)},
        "files": {name: (f"{name}.wav", body, "audio/wav") for name, body in files.items()},
    }


def meta(handoff_id="h-1", parts_=(0,), **extra) -> dict:
    sources = [
        {"source": src, "part": p, "offset": 3.0 * p, "sample_rate": 16000, "file": f"{src}{p}"}
        for p in parts_
        for src in ("mic", "system")
    ]
    return {
        "handoff_id": handoff_id,
        "title": "Standup on the train",
        "started_at": "2026-11-09T09:00:00+00:00",
        "ended_at": "2026-11-09T09:20:00+00:00",
        "notes": "- ask Ana about the budget",
        "sources": sources,
    } | extra


async def finish(ctx, sid: str) -> dict:
    jobs = [j for j in ctx.jobs.list(session_id=sid) if j.kind == "finish"]
    assert jobs, "no finish job"
    done = await ctx.jobs.wait(jobs[-1].id)
    assert done.status == "completed", done.error
    return done.result or {}


# ---- the laptop's side: PUT/GET/DELETE /api/system/main --------------------------------------


async def test_the_shell_tells_its_backend_about_the_main_computer(laptop):
    lctx = laptop.state.ctx
    async with client(laptop) as shell:  # no token: the shell never has it
        assert (await shell.get("/api/system/main")).json() == {
            "connected": False,
            "name": "",
            "url": "",
        }
        r = await shell.put(
            "/api/system/main",
            json={"url": "http://127.0.0.1:8009", "token": "tok-main", "name": "Desktop"},
        )
        assert r.status_code == 200, r.text
        assert r.json() == {"connected": True, "name": "Desktop", "url": "http://127.0.0.1:8009"}
        assert "tok-main" not in (await shell.get("/api/system/main")).text
    main = lctx.main_computer
    assert (main.url, main.token, main.name) == ("http://127.0.0.1:8009", "tok-main", "Desktop")
    path = lctx.settings.data_dir / "main_computer.json"
    assert path.stat().st_mode & 0o777 == 0o600
    # Kept across a restart of the backend.
    again = create_app(lctx.settings, keystore=MemoryKeyStore()).state.ctx
    assert again.main_computer.token == "tok-main"

    async with client(laptop) as shell:
        r = await shell.delete("/api/system/main")
        assert r.status_code == 200 and r.json()["connected"] is False
    assert lctx.main_computer is None and not path.exists()


async def test_only_a_loopback_tunnel_address_is_taken(laptop):
    async with client(laptop) as shell:
        for url in ("https://127.0.0.1:8009", "http://evil.example:8009", "http://127.0.0.1"):
            r = await shell.put("/api/system/main", json={"url": url, "token": "t"})
            assert r.status_code == 400, url
        r = await shell.put("/api/system/main", json={"url": "http://127.0.0.1:8009", "token": ""})
        assert r.status_code == 400
    assert laptop.state.ctx.main_computer is None


async def test_the_main_computer_routes_are_the_desktop_apps_only(laptop):
    lctx = laptop.state.ctx
    body = {"url": "http://127.0.0.1:8009", "token": "tok", "name": "x"}
    # Through the tunnel: no LOCAL_PATHS, and even the admin token does not open the route.
    async with client(laptop, peer=TUNNEL) as tunnelled:
        assert (await tunnelled.put("/api/system/main", json=body)).status_code == 401
    async with client(laptop, token="admin-laptop", peer=TUNNEL) as tunnelled:
        assert (await tunnelled.put("/api/system/main", json=body)).status_code == 403
        assert (await tunnelled.get("/api/system/main")).status_code == 403
        assert (await tunnelled.delete("/api/system/main")).status_code == 403
    # Passed on by a proxy, or naming another host: not the desktop app either.
    async with client(laptop, host="laptop.tailnet.ts.net") as proxied:
        assert (await proxied.put("/api/system/main", json=body)).status_code == 401
    async with client(laptop, token="admin-laptop") as admin:
        r = await admin.put("/api/system/main", json=body, headers={"X-Forwarded-For": "1.2.3.4"})
        assert r.status_code == 403
    # A paired computer's token opens the whole API, but not this.
    _, device_token = lctx.pairing.add_desktop("Other", "f" * 64)
    async with client(laptop, token=device_token, peer=TUNNEL) as other:
        assert (await other.put("/api/system/main", json=body)).status_code == 403
    assert lctx.main_computer is None


async def test_the_shell_reads_what_it_needs_without_the_token(laptop, monkeypatch):
    """lib.rs `gpu_install_wanted`: GET /api/system from the shell, which has no token."""
    from mnemosyne.api.routes import system

    monkeypatch.setattr(system, "_gpu_driver", lambda: False)
    async with client(laptop) as shell:
        r = await shell.get("/api/system")
        assert r.status_code == 200
        assert {"setup_complete", "gpu_support"} <= set(r.json())
        # Other paths still need the token.
        assert (await shell.get("/api/sessions")).status_code == 401
    async with client(laptop, peer=TUNNEL) as tunnelled:
        assert (await tunnelled.get("/api/system")).status_code == 401


# ---- the main computer's side: POST /api/connections/handoff ---------------------------------


async def test_a_handoff_becomes_a_meeting_here(
    app, ctx, laptop_token, fake_pipewire, timeline, fake_engine
):
    ctx.settings.auto_transcribe = False
    async with client(app, laptop_token, peer=TUNNEL) as laptop_:
        r = await laptop_.post("/api/connections/handoff", **handoff_form(meta(parts_=(0, 1))))
    assert r.status_code == 200, r.text
    sid = r.json()["session_id"]
    assert r.json()["replaced"] is False
    result = await finish(ctx, sid)
    assert result["parts"] == 2 and result["transcribe_job_id"] is None

    session = ctx.sessions.get_session(sid)
    assert session.origin == "" and session.name == "Standup on the train"
    assert session.notes == "- ask Ana about the budget"
    assert session.status.value == "created"
    assert sorted((r.part, r.offset, r.source) for r in session.recordings) == [
        (0, 0.0, "mic"),
        (0, 0.0, "system"),
        (1, 3.0, "mic"),
        (1, 3.0, "system"),
    ]
    assert session.created_at.date().isoformat() == "2026-11-09"
    events = ctx.repo.events(sid)
    handed = [e for e in events if e["kind"] == "handed_over"]
    assert len(handed) == 1 and handed[0]["detail"]["by"] == "Corey's laptop"
    assert [e["part"] for e in events if e["kind"] == "part_saved"] == [0, 1]
    # Saved: nothing is left for recovery.
    assert not list((ctx.settings.recordings_dir / sid).glob("recording-*.json"))


async def test_a_handoff_is_transcribed_as_a_stopped_recording_is(
    app, ctx, laptop_token, fake_pipewire, fake_engine
):
    ctx.settings.auto_transcribe = True
    ctx.settings.auto_summarize = False
    async with client(app, laptop_token, peer=TUNNEL) as laptop_:
        r = await laptop_.post("/api/connections/handoff", **handoff_form(meta(notes="")))
    assert r.status_code == 200, r.text
    sid = r.json()["session_id"]
    result = await finish(ctx, sid)
    assert result["transcribe_job_id"]
    done = await ctx.jobs.wait(result["transcribe_job_id"])
    assert done.status == "completed", done.error
    session = ctx.sessions.get_session(sid)
    assert session.transcript and session.notes == ""


async def test_a_retry_returns_the_same_meeting_and_does_nothing_again(
    app, ctx, laptop_token, fake_pipewire, fake_engine
):
    ctx.settings.auto_transcribe = False
    async with client(app, laptop_token, peer=TUNNEL) as laptop_:
        first = await laptop_.post("/api/connections/handoff", **handoff_form(meta()))
        sid = first.json()["session_id"]
        await finish(ctx, sid)
        count = len(ctx.sessions.list_sessions())
        again = await laptop_.post("/api/connections/handoff", **handoff_form(meta()))
    assert again.status_code == 200 and again.json()["session_id"] == sid
    assert len(ctx.sessions.list_sessions()) == count
    assert len([j for j in ctx.jobs.list(session_id=sid) if j.kind == "finish"]) == 1
    assert [e["kind"] for e in ctx.repo.events(sid)].count("handed_over") == 1
    # Remembered across a restart of the backend.
    from mnemosyne.services.handoff import HandoffStore

    store = HandoffStore(ctx.settings.data_dir / "handoffs.json")
    assert store.session_for(store.key(ctx.pairing.verify(laptop_token).id, "h-1")) == sid


async def test_only_paired_computers_and_the_owner_hand_over(app, ctx, laptop_token, fake_pipewire):
    ctx.settings.auto_transcribe = False
    code, _ = ctx.pairing.new_code(PHONE)
    _, phone = ctx.pairing.redeem(code, "Phone")
    async with client(app, phone, peer=TUNNEL) as c:
        r = await c.post("/api/connections/handoff", **handoff_form(meta("p")))
        assert r.status_code == 401
    async with client(app, peer=TUNNEL) as c:  # the tunnel without a token
        assert (await c.post("/api/connections/handoff", **handoff_form(meta()))).status_code == 401
    # The owner (the admin token) may.
    async with client(app, "admin-main") as owner:
        r = await owner.post("/api/connections/handoff", **handoff_form(meta("o")))
        assert r.status_code == 200, r.text
        await finish(ctx, r.json()["session_id"])
    assert len(ctx.sessions.list_sessions()) == 1


async def test_bad_handoffs_are_refused(app, ctx, laptop_token):
    async with client(app, laptop_token, peer=TUNNEL) as c:
        r = await c.post(
            "/api/connections/handoff",
            **handoff_form(meta(), {"mic0": b"not audio at all", "system0": wav()}),
        )
        assert r.status_code == 400
        r = await c.post("/api/connections/handoff", **handoff_form(meta(), {"mic0": wav()}))
        assert r.status_code == 400  # system0 missing
        r = await c.post("/api/connections/handoff", **handoff_form(meta(handoff_id="../x")))
        assert r.status_code == 400
        r = await c.post(
            "/api/connections/handoff",
            data={"meta": "{}"},
            files={"x": ("x.wav", wav(), "audio/wav")},
        )
        assert r.status_code == 400
    assert ctx.sessions.list_sessions() == []


async def test_an_ogg_recording_is_taken_too(app, ctx, laptop_token, fake_pipewire, monkeypatch):
    from mnemosyne.api.routes import handoff

    decoded = []

    def to_wav(src, dest):
        assert Path(src).read_bytes().startswith(b"OggS")
        decoded.append(src)
        Path(dest).write_bytes(wav())

    monkeypatch.setattr(handoff, "to_wav", to_wav)
    ctx.settings.auto_transcribe = False
    files = {"mic0": b"OggS" + b"\x00" * 60, "system0": wav()}
    async with client(app, laptop_token, peer=TUNNEL) as c:
        r = await c.post("/api/connections/handoff", **handoff_form(meta(), files))
    assert r.status_code == 200, r.text
    await finish(ctx, r.json()["session_id"])
    assert len(decoded) == 1 and not Path(decoded[0]).exists()


# ---- replacing a streamed meeting's audio ----------------------------------------------------


async def _streamed(app, ctx, token: str, transcribe: bool) -> str:
    """A meeting the device streamed into (the browser recorder's routes) and stopped."""
    async with client(app, token, peer=TUNNEL) as c:
        r = await c.post(
            "/api/audio/start-browser", json={"sources": ["mic", "system"], "sample_rate": 16000}
        )
        assert r.status_code == 200, r.text
        sid = r.json()["session_id"]
        r = await c.post(f"/api/audio/stop/{sid}", json={"transcribe": transcribe})
        assert r.status_code == 200, r.text
    await finish(ctx, sid)
    return sid


async def test_a_handoff_replaces_a_streamed_meetings_audio_before_transcription(
    app, ctx, laptop_token, fake_pipewire, fake_engine
):
    ctx.settings.auto_transcribe = False
    sid = await _streamed(app, ctx, laptop_token, transcribe=False)
    before = ctx.sessions.get_session(sid)
    old = [Path(r.path) for r in before.recordings]
    assert old and all(p.exists() for p in old)
    assert ctx.repo.events(sid)[0]["detail"].get("by_device")

    # Another device may not replace it.
    _, other = ctx.pairing.add_desktop("Other laptop", "d" * 64)
    async with client(app, other, peer=TUNNEL) as c:
        r = await c.post("/api/connections/handoff", **handoff_form(meta("r0", session_id=sid)))
        assert r.status_code == 409

    async with client(app, laptop_token, peer=TUNNEL) as c:
        r = await c.post("/api/connections/handoff", **handoff_form(meta("r1", session_id=sid)))
    assert r.status_code == 200, r.text
    assert r.json() == {"session_id": sid, "replaced": True}
    await finish(ctx, sid)
    after = ctx.sessions.get_session(sid)
    assert {r.path for r in after.recordings}.isdisjoint({str(p) for p in old})
    assert sorted((r.part, r.source) for r in after.recordings) == [(0, "mic"), (0, "system")]
    assert not any(p.exists() for p in old)
    assert len(ctx.sessions.list_sessions()) == 1

    # Once replaced, not again (a second hand-off of the same meeting is not a stream's).
    async with client(app, laptop_token, peer=TUNNEL) as c:
        r = await c.post("/api/connections/handoff", **handoff_form(meta("r2", session_id=sid)))
        assert r.status_code == 409


def _with_notes(ctx, sid: str) -> None:
    """Something to keep, so the meeting is a record the sealer seals (notes-only counts)."""
    with shared_notes.unrestricted():
        ctx.sessions.update_notes(sid, "- the stream, as typed live")


async def test_replacing_sealed_audio_seals_its_erasure_first(
    app, ctx, laptop_token, fake_pipewire, fake_engine
):
    ctx.settings.auto_transcribe = False
    sid = await _streamed(app, ctx, laptop_token, transcribe=False)
    _with_notes(ctx, sid)
    streamed = ctx.sessions.get_session(sid)
    old = [Path(r.path) for r in streamed.recordings] + [Path(streamed.audio_file)]
    assert records.seal(ctx, sid, "background") is not None  # what the sealer does when quiet
    sealed = ctx.repo.seals(sid)[-1]["manifest"]["audio"]
    assert {p.name for p in old} <= set(sealed)

    async with client(app, laptop_token, peer=TUNNEL) as c:
        r = await c.post("/api/connections/handoff", **handoff_form(meta("r1", session_id=sid)))
    assert r.status_code == 200, r.text
    seals = ctx.repo.seals(sid)
    assert seals[-1]["reason"] == "audio deleted"  # sealed before the files changed
    assert all(p.exists() for p in old)  # the finish job has not unlinked them yet
    await finish(ctx, sid)
    assert not any(p.exists() for p in old)

    assert records.seal(ctx, sid, "after hand-over") is not None
    check = records.verify(ctx, sid)
    assert check.ok, check.problems
    assert sorted(e.name for e in check.erased) == sorted({p.name for p in old})
    assert all(e.matched for e in check.erased)
    rows = ctx.repo.deletions()
    assert len(rows) == 1 and rows[0]["what"] == "audio" and rows[0]["by"] == "Corey's laptop"
    manifest = ctx.repo.seals(sid)[-1]["manifest"]
    assert set(manifest["erased"]) == {p.name for p in old}
    new = {Path(x.path).name for x in ctx.sessions.get_session(sid).recordings}
    assert new <= set(manifest["audio"]) and new.isdisjoint(manifest["erased"])


async def test_replacing_audio_no_seal_listed_logs_no_erasure(
    app, ctx, laptop_token, fake_pipewire, fake_engine
):
    ctx.settings.auto_transcribe = False
    sid = await _streamed(app, ctx, laptop_token, transcribe=False)
    _with_notes(ctx, sid)  # sealable, but nothing sealed it yet
    assert ctx.repo.seals(sid) == []
    async with client(app, laptop_token, peer=TUNNEL) as c:
        r = await c.post("/api/connections/handoff", **handoff_form(meta("r1", session_id=sid)))
    assert r.status_code == 200, r.text
    await finish(ctx, sid)
    assert ctx.repo.seals(sid) == [] and ctx.repo.deletions() == []
    assert records.seal(ctx, sid, "after hand-over") is not None
    check = records.verify(ctx, sid)
    assert check.ok, check.problems
    assert check.erased == []


async def test_replacing_sealed_audio_that_cannot_be_erased_changes_nothing(
    app, ctx, laptop_token, fake_pipewire, fake_engine, monkeypatch
):
    ctx.settings.auto_transcribe = False
    sid = await _streamed(app, ctx, laptop_token, transcribe=False)
    _with_notes(ctx, sid)
    assert records.seal(ctx, sid, "background") is not None
    old = [Path(r.path) for r in ctx.sessions.get_session(sid).recordings]

    def locked(*args, **kwargs):
        raise ErasureNotSealed("Encryption is locked: unlock Mnemosyne to delete this audio")

    monkeypatch.setattr(records, "seal_erasure", locked)
    async with client(app, laptop_token, peer=TUNNEL) as c:
        r = await c.post("/api/connections/handoff", **handoff_form(meta("r1", session_id=sid)))
        assert r.status_code == 409 and "Encryption is locked" in r.json()["detail"]
        monkeypatch.undo()  # the same hand-off may be tried again once it can be sealed
        r = await c.post("/api/connections/handoff", **handoff_form(meta("r1", session_id=sid)))
        assert r.status_code == 200, r.text
    await finish(ctx, sid)
    assert not any(p.exists() for p in old)


async def test_a_refused_replacement_leaves_the_meeting_as_it_was(
    app, ctx, laptop_token, fake_pipewire, fake_engine, monkeypatch
):
    ctx.settings.auto_transcribe = False
    sid = await _streamed(app, ctx, laptop_token, transcribe=False)
    _with_notes(ctx, sid)
    records.seal(ctx, sid, "background")
    before = ctx.sessions.get_session(sid)
    folder = ctx.settings.recordings_dir / sid
    files = sorted(p.name for p in folder.iterdir())
    seals = len(ctx.repo.seals(sid))

    def locked(*args, **kwargs):
        raise ErasureNotSealed("Encryption is locked")

    monkeypatch.setattr(records, "seal_erasure", locked)
    async with client(app, laptop_token, peer=TUNNEL) as c:
        r = await c.post("/api/connections/handoff", **handoff_form(meta("r1", session_id=sid)))
    assert r.status_code == 409
    after = ctx.sessions.get_session(sid)
    assert after.status == before.status and after.audio_file == before.audio_file
    assert [x.path for x in after.recordings] == [x.path for x in before.recordings]
    assert sorted(p.name for p in folder.iterdir()) == files
    assert len(ctx.repo.seals(sid)) == seals and ctx.repo.deletions() == []
    assert not [e for e in ctx.repo.events(sid) if e["kind"] == "handed_over"]


async def test_no_replacing_once_transcription_started(
    app, ctx, laptop_token, fake_pipewire, fake_engine
):
    ctx.settings.auto_summarize = False
    sid = await _streamed(app, ctx, laptop_token, transcribe=True)
    async with client(app, laptop_token, peer=TUNNEL) as c:
        r = await c.post("/api/connections/handoff", **handoff_form(meta("r", session_id=sid)))
    assert r.status_code == 409
    assert "Transcription" in r.json()["detail"]
    for job in ctx.jobs.list(session_id=sid):
        await ctx.jobs.wait(job.id)


async def test_no_replacing_a_meeting_recorded_here(app, ctx, laptop_token, fake_pipewire):
    sid = ctx.sessions.create_session("Recorded here").id
    async with client(app, laptop_token, peer=TUNNEL) as c:
        r = await c.post("/api/connections/handoff", **handoff_form(meta("r", session_id=sid)))
        assert r.status_code == 409
        r = await c.post("/api/connections/handoff", **handoff_form(meta("r", session_id="nope")))
        assert r.status_code == 404
