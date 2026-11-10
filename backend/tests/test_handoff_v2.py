"""Hand-off v2 and moves (docs/plans/2026-11-10-team-members-in-the-app.md 1.3 (b), 1.5, M10,
M11, R3, R4): a team member's app records a team meeting here, does the work (transcript,
summary, notes) and MOVES the finished meeting to the hosting computer, where it is the member's.

The host is this process's `app` hosting a team (Corey the owner, Sam a member whose app is a
MEMBER device). The member is a second app whose TeamMember is pointed at the host through
httpx.ASGITransport behind `Tunnelled`, as the sidecar's dial port would reach it (no iroh). The
notes documents run on the stand-in sidecar (tests/fake_link.py). No audio is decoded or played:
the Ogg files are a header, the host's decoder and mixer are the conftest fakes."""

from __future__ import annotations

import asyncio
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from mnemosyne import access
from mnemosyne.api.app import create_app
from mnemosyne.api.routes import handoff as handoff_route
from mnemosyne.config import Settings
from mnemosyne.models.session import Recording, Session, SessionStatus, SummaryData
from mnemosyne.models.transcript import TranscriptSegment
from mnemosyne.services import history, notes_media, parts, shared_notes
from mnemosyne.services.sync_state import export_session
from mnemosyne.services.team_member import Membership
from mnemosyne.services.team_move import MoveError
from mnemosyne.services.tunnel_listener import Tunnelled
from mnemosyne.storage.crypto import MemoryKeyStore
from tests.test_handoff import wav
from tests.test_notes_media import webp
from tests.test_sync_notes_doc import doc_frames, frame
from tests.test_sync_service import meeting, nodes, pull, sidecar  # noqa: F401  (fixtures)

pytestmark = pytest.mark.anyio

LOCAL = "local-token"  # the member computer's own API token
SAM_EP = "c4" * 32
HOST_EP = "b7" * 32
OGG = b"OggS" + b"\x00" * 60
CONSENT_409 = "The team asks how the people in the meeting agreed to be recorded"


# ---- the two computers ------------------------------------------------------------------------


def make_host(ctx) -> dict:
    """`ctx` hosts a team: Corey the owner, Sam a member with an app (MEMBER device)."""
    corey = ctx.users.add("Corey", "", "admin")
    sam = ctx.users.add("Sam", "", "member")
    ctx.settings.team_mode = True
    ctx.settings.team_owner_id = corey.id
    ctx.settings.live_transcription = False
    ctx.settings.copilot = False
    ctx.follow_hosting()
    _, token = ctx.pairing.add_member("Sam's Mac", SAM_EP, sam.id)
    return {"corey": corey.id, "sam": sam.id, "token": token}


class LoseAnswers(httpx.AsyncBaseTransport):
    """The host's transport, losing the answer to the next `lose` hand-offs after the host did
    the work (a connection that dropped on the way back)."""

    def __init__(self, inner: httpx.AsyncBaseTransport):
        self.inner = inner
        self.lose = 0
        self.handoffs: list[dict] = []

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        response = await self.inner.handle_async_request(request)
        if request.url.path == "/api/connections/handoff":
            await response.aread()
            self.handoffs.append({"status": response.status_code})
            if self.lose:
                self.lose -= 1
                raise httpx.ReadError("the answer was lost", request=request)
        return response


def connect(member_app, host_app, token: str) -> LoseAnswers:
    """What a join leaves behind, with the host reached in this process."""
    tm = member_app.state.ctx.team_member
    tm.store.save(
        Membership(
            host_name="Corey's desktop",
            host_ticket="endpointunused",
            host_endpoint=HOST_EP,
            token=token,
            user={"id": "", "name": "Sam", "role": "member"},
            joined_at="2026-11-10T10:00:00+00:00",
        )
    )
    tm.listen = "127.0.0.1:8008"
    transport = LoseAnswers(httpx.ASGITransport(app=Tunnelled(host_app)))
    tm._client = httpx.AsyncClient(transport=transport, trust_env=False)
    return transport


@pytest.fixture
def host(ctx):
    return make_host(ctx)


@pytest.fixture
def member_app(tmp_path):
    settings = Settings(
        data_dir=tmp_path / "member" / "data",
        backup_dir=str(tmp_path / "member" / "backups"),
        use_our_servers=False,
        api_token=LOCAL,
        live_transcription=False,
        copilot=False,
        auto_transcribe=False,
    )
    app = create_app(settings, keystore=MemoryKeyStore())
    app.state.ctx.team_moves.kick = lambda: None  # the tests run the tries (one test: the worker)
    yield app


@pytest.fixture
def mctx(member_app):
    return member_app.state.ctx


@pytest.fixture
def wire(app, member_app, host):
    return connect(member_app, app, host["token"])


@pytest.fixture
def fakes(monkeypatch, fake_pipewire):
    """The host decodes nothing (each Ogg becomes a short WAV) and measures each part as 3 s."""
    monkeypatch.setattr(handoff_route, "to_wav", lambda src, dest: Path(dest).write_bytes(wav()))
    monkeypatch.setattr(parts, "audio_seconds", lambda path, key: 3.0)

    def join(first, second, out, key):
        Path(out).write_bytes(Path(first).read_bytes() + Path(second).read_bytes())
        return out

    monkeypatch.setattr(parts, "join", join)


def local_meeting(mctx, name="Client kickoff", parts_=(0,), **kw) -> str:
    """A meeting recorded and transcribed on the member's computer, its sources as Ogg files."""
    folder = mctx.settings.recordings_dir / "pending"
    folder.mkdir(parents=True, exist_ok=True)
    recordings = []
    for p in parts_:
        for source in ("mic", "system"):
            path = folder / f"{name[:6]}-{source}{p}.ogg"
            path.write_bytes(OGG)
            recordings.append(
                Recording(
                    source=source,
                    device_id=1 if source == "mic" else 2,
                    device_name="MacBook microphone" if source == "mic" else "Call audio",
                    path=str(path),
                    part=p,
                    offset=3.0 * p,
                )
            )
    mixed = folder / f"{name[:6]}-mixed.ogg"
    mixed.write_bytes(OGG)
    fields = dict(
        name=name,
        status=SessionStatus.COMPLETED,
        audio_file=str(mixed),
        recordings=recordings,
        summary="We agreed on the scope.",
        summary_data=SummaryData(title=name, decisions=["Scope: phase one"]),
        transcript=[
            TranscriptSegment(text="hello there", speaker="Sam", start=0.0, end=1.0),
            TranscriptSegment(text="part two", speaker="Ana", start=3.5, end=4.0),
        ],
    )
    fields.update(kw)
    return mctx.repo.save(Session(**fields)).id


def consent(mctx, sid: str) -> None:
    history.log(mctx, sid, "consent", 0, consent="all_parties", by="Sam")


def owner_of(ctx, sid: str) -> str:
    with ctx.repo._lock:
        return ctx.repo._conn.execute(
            "SELECT owner_id FROM sessions WHERE id=?", (sid,)
        ).fetchone()[0]


async def finished(ctx, sid: str) -> None:
    for job in ctx.jobs.list(session_id=sid):
        done = await ctx.jobs.wait(job.id)
        assert done.status == "completed", done.error


def member_client(member_app, token: str | None = LOCAL) -> httpx.AsyncClient:
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=member_app),
        base_url="http://127.0.0.1:8008",
        headers=headers,
    )


def host_meetings(ctx) -> list[Session]:
    with access.internally():
        return ctx.sessions.list_sessions()


# ---- a move: everything arrives, the member's -------------------------------------------------


async def test_a_moved_meeting_arrives_whole_and_is_the_members(
    app,
    ctx,
    mctx,
    host,
    wire,
    fakes,
    sidecar,  # noqa: F811
):
    ctx.settings.auto_transcribe = True  # never for a finished meeting
    sid = local_meeting(mctx, parts_=(0, 1))
    consent(mctx, sid)
    mctx.repo.add_bookmark(sid, 1, 0.5, "the budget")
    mctx.repo.add_comment(sid, text="Check this", target="segment", author="Sam")
    mctx.repo.append_notes_ops(sid, [frame("f0", ["set", "Plan\n"])], "", "Sam", True)
    shared_notes.fold_blocking(mctx, sid)
    image = webp(pad=b"whiteboard")
    notes_media.store(mctx, sid, hashlib.sha256(image).hexdigest(), image, by="Sam")
    local_events = {e.uid for e in export_session(mctx.repo, sid, "x").events}

    mctx.team_moves.queue(sid)
    host_id = await mctx.team_moves.attempt(sid)
    assert host_id and host_id != sid
    await finished(ctx, host_id)

    with access.internally():
        got = ctx.sessions.get_session(host_id)
    assert owner_of(ctx, host_id) == host["sam"]
    assert got.origin == "" and got.shared_from == ""
    assert got.name == "Client kickoff"
    assert [s.text for s in got.transcript] == ["hello there", "part two"]
    assert got.summary == "We agreed on the scope."
    assert got.summary_data.decisions == ["Scope: phase one"]
    assert got.status == SessionStatus.COMPLETED
    assert not [j for j in ctx.jobs.list(session_id=host_id) if j.kind == "transcribe"]
    # Its parts are on the same timeline (R3): the host joined the same files in order.
    assert sorted((r.part, r.offset, r.source) for r in got.recordings) == [
        (0, 0.0, "mic"),
        (0, 0.0, "system"),
        (1, 3.0, "mic"),
        (1, 3.0, "system"),
    ]
    doc = export_session(ctx.repo, host_id, "x")
    assert [(b.part, b.seconds, b.note) for b in doc.bookmarks] == [(1, 0.5, "the budget")]
    # The notes document, comments, the image (bytes too) and the member's history.
    assert doc_frames(SimpleNamespace(repo=ctx.repo), host_id) == ["f0"]
    assert got.notes == "Plan\n"
    with access.internally():
        assert [c.text for c in ctx.repo.list_comments(host_id)] == ["Check this"]
    sha = hashlib.sha256(image).hexdigest()
    assert notes_media.read(ctx, host_id, sha) == image
    events = ctx.repo.events(host_id)
    arrived = {e.uid for e in export_session(ctx.repo, host_id, "x").events}
    assert local_events and local_events <= arrived
    handed = next(e for e in events if e["kind"] == "handed_over")
    assert handed["detail"]["transcribed"] is True and handed["detail"]["parts"] == 2
    # The member's copy is gone; the move is remembered.
    assert mctx.sessions.get_session(sid) is None
    assert not mctx.team_moves.store.pending
    assert [(m.session_id, m.host_session_id) for m in mctx.team_moves.moved()] == [(sid, host_id)]
    # Sam reads it through the host as the owner of it.
    async with _host_client(app, host["token"]) as sam:
        r = await sam.get(f"/api/sessions/{host_id}")
        assert r.status_code == 200 and r.json()["owner_id"] == host["sam"]


async def test_a_meeting_without_audio_moves_too(ctx, mctx, host, wire):
    sid = mctx.repo.save(Session(name="Notes only", notes="- call Ana\n")).id
    mctx.team_moves.queue(sid)
    host_id = await mctx.team_moves.attempt(sid)
    assert host_id
    with access.internally():
        got = ctx.sessions.get_session(host_id)
    assert got.notes == "- call Ana\n" and got.recordings == [] and not got.audio_file
    assert got.status == SessionStatus.CREATED
    assert not ctx.jobs.list(session_id=host_id)


# ---- consent ----------------------------------------------------------------------------------


async def test_without_consent_the_team_refuses_until_it_is_added(
    ctx, mctx, member_app, host, wire, fakes
):
    ctx.settings.require_consent = True
    sid = local_meeting(mctx)
    mctx.team_moves.queue(sid)
    assert await mctx.team_moves.attempt(sid) is None
    pending = mctx.team_moves.store.pending[sid]
    assert pending.last_error == CONSENT_409 and pending.attempts == 1
    assert mctx.sessions.get_session(sid) is not None  # kept: the host has nothing
    assert len(host_meetings(ctx)) == 0

    async with member_client(member_app) as c:
        listed = (await c.get("/api/joined/moves")).json()
        assert [p["session_id"] for p in listed["pending"]] == [sid]
        assert listed["pending"][0]["last_error"] == CONSENT_409
        r = await c.post(f"/api/joined/moves/{sid}/consent", json={"consent": "all_parties"})
        assert r.status_code == 200, r.text
        assert r.json()["attempts"] == 0 and r.json()["last_error"] == ""
    assert [e["detail"]["consent"] for e in mctx.repo.events(sid) if e["kind"] == "consent"] == [
        "all_parties"
    ]
    host_id = await mctx.team_moves.attempt(sid)
    assert host_id and mctx.sessions.get_session(sid) is None
    consented = [e for e in ctx.repo.events(host_id) if e["kind"] == "consent"]
    assert len(consented) == 1 and consented[0]["detail"]["consent"] == "all_parties"


# ---- retries: one meeting on the host, the local copy gone only after the answer --------------


async def test_a_lost_answer_is_retried_into_the_same_meeting(ctx, mctx, host, wire, fakes):
    sid = local_meeting(mctx)
    mctx.team_moves.queue(sid)
    wire.lose = 1
    assert await mctx.team_moves.attempt(sid) is None
    mctx.team_member._probe = None  # the failure was seen: the next try asks again
    pending = mctx.team_moves.store.pending[sid]
    assert pending.attempts == 1 and "not reachable" in pending.last_error
    assert mctx.sessions.get_session(sid) is not None  # no answer: kept
    first = host_meetings(ctx)
    assert len(first) == 1  # the host did it

    pending.next_at = 0.0
    host_id = await mctx.team_moves.attempt(sid)
    assert host_id == first[0].id
    assert len(host_meetings(ctx)) == 1
    assert [h["status"] for h in wire.handoffs] == [200, 200]
    assert mctx.sessions.get_session(sid) is None
    assert [e["kind"] for e in ctx.repo.events(host_id)].count("handed_over") == 1


async def test_a_copy_the_records_keep_here_stays_and_a_retry_finds_the_same_meeting(
    ctx, mctx, host, wire, fakes
):
    sid = local_meeting(mctx)
    mctx.repo.set_legal_hold(sid, "Litigation")
    mctx.team_moves.queue(sid)
    assert await mctx.team_moves.attempt(sid) is None
    pending = mctx.team_moves.store.pending[sid]
    assert "kept here" in pending.last_error and "legal hold" in pending.last_error
    assert mctx.sessions.get_session(sid) is not None
    host_id = pending.host_session_id
    assert host_id and [s.id for s in host_meetings(ctx)] == [host_id]

    mctx.repo.set_legal_hold(sid, "")
    pending.next_at = 0.0
    assert await mctx.team_moves.attempt(sid) == host_id
    assert len(host_meetings(ctx)) == 1 and mctx.sessions.get_session(sid) is None


async def test_a_meeting_still_being_worked_on_waits(ctx, mctx, host, wire, fakes):
    sid = local_meeting(mctx, transcript=[])
    mctx.team_moves.queue(sid)
    assert await mctx.team_moves.attempt(sid) is None
    pending = mctx.team_moves.store.pending[sid]
    assert pending.last_error == "Waiting for the transcription" and pending.attempts == 0
    assert host_meetings(ctx) == []


async def test_the_worker_moves_what_waits(ctx, mctx, host, wire, fakes):
    from mnemosyne.services.team_move import TeamMoves

    del mctx.team_moves.kick  # the real kick: the worker runs
    assert isinstance(mctx.team_moves, TeamMoves)
    sid = local_meeting(mctx)
    mctx.team_moves.queue(sid)
    for _ in range(500):
        if not mctx.team_moves.store.pending:
            break
        await asyncio.sleep(0.01)
    await mctx.team_moves.stop()
    assert not mctx.team_moves.store.pending and mctx.sessions.get_session(sid) is None
    assert len(host_meetings(ctx)) == 1


# ---- what the host refuses ----------------------------------------------------------------------


def _v2_form(mctx, sid: str, media: dict[str, bytes] | None = None, **extra) -> dict:
    doc = export_session(mctx.repo, sid, mctx.sync_peers.self_info().id)
    meta = {
        "handoff_id": f"x-{sid}",
        "title": "x",
        "sources": [
            {"source": "mic", "part": 0, "sample_rate": 48000, "file": "mic0"},
        ],
        "transcribed": True,
        "session": json.loads(doc.model_dump_json()),
    } | extra
    files = [("mic0", ("mic0.ogg", OGG, "audio/ogg"))]
    files += [
        (f"media:{sha}", (sha, data, "application/octet-stream"))
        for sha, data in (media or {}).items()
    ]
    return {"data": {"meta": json.dumps(meta)}, "files": files}


def _host_client(app, token: str) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=Tunnelled(app)),
        base_url="http://127.0.0.1:8008",
        headers={"Authorization": f"Bearer {token}"},
    )


async def test_an_image_is_checked_before_anything_is_stored(
    app, ctx, mctx, member_app, host, fakes
):
    sid = local_meeting(mctx)
    good = webp(pad=b"ok")
    notes_media.store(mctx, sid, hashlib.sha256(good).hexdigest(), good)
    # A row whose bytes are not an image (a GIF), named by their own hash: refused by magic.
    bad = b"GIF89a" + b"\x00" * 40
    bad_sha = hashlib.sha256(bad).hexdigest()
    with mctx.repo._lock, mctx.repo._conn:
        mctx.repo._conn.execute(
            "INSERT INTO notes_media (session_id, sha256, ext, width, height, size, created_at,"
            " last_seen_at, by) VALUES (?,?,?,?,?,?,?,?,?)",
            (sid, bad_sha, "webp", 1, 1, len(bad), "2026-11-10", "2026-11-10", ""),
        )
    good_sha = hashlib.sha256(good).hexdigest()
    async with _host_client(app, host["token"]) as c:
        r = await c.post(
            "/api/connections/handoff", **_v2_form(mctx, sid, {good_sha: good, bad_sha: bad})
        )
        assert r.status_code == 400 and "WebP and JPEG" in r.json()["detail"]
        # Bytes that do not match their hash.
        r = await c.post("/api/connections/handoff", **_v2_form(mctx, sid, {good_sha: good + b"x"}))
        assert r.status_code == 400 and "hash" in r.json()["detail"]
        # A part no row names.
        r = await c.post(
            "/api/connections/handoff", **_v2_form(mctx, sid, {"0" * 64: good}, handoff_id="y")
        )
        assert r.status_code == 400
    assert host_meetings(ctx) == []
    assert not (ctx.settings.data_dir / "notes_media").exists() or not any(
        (ctx.settings.data_dir / "notes_media").rglob("*.webp")
    )
    # The member's own move leaves the damaged image out and goes through.
    connect(member_app, app, host["token"])
    mctx.team_moves.queue(sid)
    host_id = await mctx.team_moves.attempt(sid)
    assert host_id
    assert notes_media.read(ctx, host_id, good_sha) == good


async def test_a_finished_meeting_is_never_a_replace_nor_from_the_owners_laptop(
    app, ctx, mctx, host, fakes
):
    sid = local_meeting(mctx)
    async with _host_client(app, host["token"]) as c:
        r = await c.post("/api/connections/handoff", **_v2_form(mctx, sid, session_id="abc"))
        assert r.status_code == 409
    _, laptop = ctx.pairing.add_desktop("Corey's laptop", "e" * 64)
    async with _host_client(app, laptop) as c:
        r = await c.post("/api/connections/handoff", **_v2_form(mctx, sid))
        assert r.status_code == 403
        # A document without `transcribed`, or `transcribed` without one: 400.
        form = _v2_form(mctx, sid)
        meta = json.loads(form["data"]["meta"])
        meta["transcribed"] = False
        form["data"]["meta"] = json.dumps(meta)
        assert (await c.post("/api/connections/handoff", **form)).status_code == 400
    assert host_meetings(ctx) == []


async def test_the_documents_origin_owner_and_contact_shares_are_ignored(
    app, ctx, mctx, host, fakes
):
    sid = local_meeting(mctx)
    form = _v2_form(mctx, sid)
    meta = json.loads(form["data"]["meta"])
    meta["session"] |= {"origin": "elsewhere", "owner_id": host["corey"]}
    meta["session"]["peer_shares"] = [
        {"person": "p" * 16, "parts": {}, "by": "Sam", "at": "2026-11-10", "updated_at": ""}
    ]
    form["data"]["meta"] = json.dumps(meta)
    async with _host_client(app, host["token"]) as c:
        r = await c.post("/api/connections/handoff", **form)
    assert r.status_code == 200, r.text
    host_id = r.json()["session_id"]
    await finished(ctx, host_id)
    with access.internally():
        got = ctx.sessions.get_session(host_id)
    assert got.origin == "" and owner_of(ctx, host_id) == host["sam"]
    with ctx.repo._lock:
        shares = ctx.repo._conn.execute(
            "SELECT COUNT(*) FROM peer_shares WHERE session_id=?", (host_id,)
        ).fetchone()[0]
    assert shares == 0


# ---- M11: only queued meetings of this computer's user, never one from elsewhere -------------


async def test_what_may_not_be_queued(mctx, member_app, host, wire):
    elsewhere = local_meeting(mctx, name="Recorded on the desktop")
    shared = local_meeting(mctx, name="Ada's")
    with mctx.repo._lock, mctx.repo._conn:
        mctx.repo._conn.execute("UPDATE sessions SET origin=? WHERE id=?", ("d" * 12, elsewhere))
        mctx.repo._conn.execute("UPDATE sessions SET shared_from='ada' WHERE id=?", (shared,))
    theirs = local_meeting(mctx, name="Someone else's", owner_id="u-other")
    for sid in (elsewhere, shared, theirs):
        with pytest.raises(MoveError) as e:
            mctx.team_moves.queue(sid)
        assert e.value.status == 409
    async with member_client(member_app) as c:
        r = await c.post(f"/api/joined/moves/{elsewhere}")
        assert r.status_code == 409 and "another computer" in r.json()["detail"]
        assert (await c.post("/api/joined/moves/nope")).status_code == 404
    assert not mctx.team_moves.store.pending


async def test_nothing_is_queued_while_not_in_a_team(mctx, member_app):
    sid = local_meeting(mctx)
    async with member_client(member_app) as c:
        r = await c.post(f"/api/joined/moves/{sid}")
        assert r.status_code == 409 and "not in a team" in r.json()["detail"]
        r = await c.post("/api/audio/start", json={"device_ids": [1], "for_team": True})
        assert r.status_code == 409
    assert not mctx.team_moves.store.pending


async def test_only_queued_meetings_are_sent(ctx, mctx, member_app, host, wire, fakes):
    queued = local_meeting(mctx, name="For the team")
    mine = local_meeting(mctx, name="My own")
    async with member_client(member_app) as c:
        r = await c.post(f"/api/joined/moves/{queued}")
        assert r.status_code == 200, r.text
        status = (await c.get("/api/joined")).json()
        assert status["pending_moves"] == 1
    for p in list(mctx.team_moves.store.pending):
        await mctx.team_moves.attempt(p)
    assert [s.name for s in host_meetings(ctx)] == ["For the team"]
    assert mctx.sessions.get_session(mine) is not None
    assert len(wire.handoffs) == 1
    # A meeting taken off the list stays here.
    later = local_meeting(mctx, name="Changed my mind")
    async with member_client(member_app) as c:
        assert (await c.post(f"/api/joined/moves/{later}")).status_code == 200
        r = await c.delete(f"/api/joined/moves/{later}")
        assert r.status_code == 200 and r.json()["pending"] == []
        assert (await c.delete(f"/api/joined/moves/{later}")).status_code == 404


async def test_the_moves_routes_are_this_computers_only(mctx, member_app, host, wire):
    sid = local_meeting(mctx)
    tunnelled = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=Tunnelled(member_app)),
        base_url="http://127.0.0.1:8008",
        headers={"Authorization": f"Bearer {LOCAL}"},
    )
    async with tunnelled as c:
        assert (await c.get("/api/joined/moves")).status_code == 403
        assert (await c.post(f"/api/joined/moves/{sid}")).status_code == 403
    async with member_client(member_app, token=None) as c:
        assert (await c.get("/api/joined/moves")).status_code == 401


async def test_recording_for_the_team_only_marks_the_new_meeting(
    mctx, member_app, host, wire, fake_pipewire
):
    async with member_client(member_app) as c:
        r = await c.post("/api/audio/start", json={"device_ids": [1, 2], "for_team": True})
        assert r.status_code == 200, r.text
        sid = r.json()["session_id"]
        assert list(mctx.team_moves.store.pending) == [sid]
        # Recording waits: nothing is sent while it records.
        assert await mctx.team_moves.attempt(sid) is None
        assert "recording" in mctx.team_moves.store.pending[sid].last_error
        r = await c.post(f"/api/audio/stop/{sid}", json={"transcribe": False})
        assert r.status_code == 200
        await finished(mctx, sid)
        # A recording not for the team is not marked.
        r = await c.post("/api/audio/start", json={"device_ids": [1]})
        other = r.json()["session_id"]
        await c.post(f"/api/audio/stop/{other}", json={"transcribe": False})
        await finished(mctx, other)
    assert list(mctx.team_moves.store.pending) == [sid]


# ---- M10: a sync peer of the host's owner never receives a moved meeting ----------------------


async def test_a_sync_peer_of_the_owner_never_receives_a_moved_meeting(
    nodes,  # noqa: F811
    tmp_path,
    fakes,
):
    a, b = nodes
    ids = make_host(a.ctx)
    member = create_app(
        Settings(
            data_dir=tmp_path / "member" / "data",
            backup_dir=str(tmp_path / "member" / "backups"),
            use_our_servers=False,
            api_token=LOCAL,
        ),
        keystore=MemoryKeyStore(),
    )
    m = member.state.ctx
    m.team_moves.kick = lambda: None
    connect(member, a.app, ids["token"])
    mine = meeting(a, name="Owner's planning")
    sid = local_meeting(m, name="Member's client call")
    m.team_moves.queue(sid)
    host_id = await m.team_moves.attempt(sid)
    assert host_id and owner_of(a.ctx, host_id) == ids["sam"]
    await finished(a.ctx, host_id)

    st = await pull(b, a)
    assert st.state == "done"
    assert b.repo.get(mine) is not None and b.repo.get(host_id) is None
    async with b.sync.client_for(b.peer(a)) as c:
        text = (await c.get("/api/sync/changes", params={"limit": 200})).text
    assert host_id not in text and "Member's client call" not in text
    await m.jobs.shutdown()
    m.repo.close()
