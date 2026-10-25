"""Live shared notes (services/shared_notes.py, api/notes_ws.py): the relay, its log, folding
into Session.notes, access, the notes route through the document, seeding. The sidecar is the
`notes doc` stand-in of tests/fake_link.py (toy frames, see its docstring)."""

import asyncio
import base64
import json
import time
from datetime import datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from mnemosyne import access
from mnemosyne.api.app import create_app
from mnemosyne.models.session import Session
from mnemosyne.services import shared_notes, sharing
from tests import fake_link
from tests.fake_link import doc_frame
from tests.test_combine import _meeting, _wait, needs_ffmpeg


@pytest.fixture
def sidecar(tmp_path, monkeypatch):
    return fake_link.install(tmp_path, monkeypatch)


@pytest.fixture(autouse=True)
def no_timed_folds(monkeypatch):
    """Folds only when a test asks (or the last one leaves): no 2 s timer in the middle."""
    monkeypatch.setattr(shared_notes, "FOLD_DELAY", 1000.0)
    monkeypatch.setattr(shared_notes, "FOLD_EVERY", 1000.0)


def frame(frame_id: str, *ops) -> bytes:
    return base64.b64decode(doc_frame(frame_id, *ops))


def meeting(ctx, notes: str = "", origin: str = "") -> str:
    return ctx.repo.save(Session(name="Planning", notes=notes, origin=origin)).id


def url(sid: str) -> str:
    return f"/ws/notes/{sid}"


def settle(ws) -> None:
    """Everything sent before this has been handled by the server."""
    ws.send_text(json.dumps({"type": "ping"}))
    assert ws.receive_json() == {"type": "pong"}


def snapshot_frames(snapshot: bytes) -> list[str]:
    return [f["id"] for f in json.loads(snapshot)["frames"]]


def wait_for(check, timeout: float = 5.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        value = check()
        if value:
            return value
        time.sleep(0.02)
    raise AssertionError("timed out")


def events(ctx) -> list[dict]:
    seen: list[dict] = []
    publish = ctx.bus.publish

    def record(event):
        seen.append(event)
        publish(event)

    ctx.bus.publish = record
    return seen


# ---- protocol --------------------------------------------------------------------------


def test_hello_snapshot_and_backlog(client, ctx, sidecar):
    sid = meeting(ctx)
    with client.websocket_connect(url(sid)) as a:
        hello = a.receive_json()
        assert hello == {
            "type": "hello",
            "notes": shared_notes.notes_origin(sid),
            "you": {"id": "", "name": "Me"},
            "read_only": False,
            "present": ["Me"],
            "last_edit": None,
            "snapshot": False,
            "backlog": 0,
        }
        assert len(hello["notes"]) == 64
        a.send_bytes(frame("f1", ["ins", 0, "hello\n"]))
        a.send_bytes(frame("f2", ["ins", 6, "world\n"]))
        settle(a)
        assert shared_notes.fold_blocking(ctx, sid)
        a.send_bytes(frame("f3", ["ins", 0, "# "]))
        settle(a)

        with client.websocket_connect(url(sid)) as b:
            hello = b.receive_json()
            assert hello["snapshot"] is True and hello["backlog"] == 1
            assert hello["last_edit"]["by"] == "Me"
            assert snapshot_frames(b.receive_bytes()) == ["f1", "f2"]
            assert b.receive_bytes() == frame("f3", ["ins", 0, "# "])
            settle(b)
            assert a.receive_json()["type"] == "presence"  # b joined


def test_relay_without_echo_and_artifacts_logged(client, ctx, sidecar):
    sid = meeting(ctx)
    with client.websocket_connect(url(sid)) as a:
        a.receive_json()
        with client.websocket_connect(url(sid)) as b:
            b.receive_json()
            assert a.receive_json() == {"type": "presence", "present": ["Me"], "typing": []}
            delta = frame("f1", ["ins", 0, "hi\n"])
            artifact = bytes([0xAF]) + b"https://example.test/a-long-link"
            a.send_bytes(delta)
            a.send_bytes(artifact)
            settle(a)  # pong first: a got neither of its own frames back
            assert b.receive_bytes() == delta
            assert b.receive_json()["type"] == "last_edit"
            assert b.receive_bytes() == artifact
            ops = ctx.repo.notes_ops(sid)
            assert [o["frame"] for o in ops] == [delta, artifact]
            assert [(o["author"], o["folded"]) for o in ops] == [("Me", 0), ("Me", 0)]
    # The last one out folded: the artifact is in the snapshot too.
    wait_for(lambda: ctx.repo.get(sid).notes == "hi\n")
    assert not ctx.repo.has_unfolded_notes(sid)
    snap = json.loads(ctx.repo.notes_doc(sid)["snapshot"])
    assert len(snap["artifacts"]) == 1


@pytest.mark.parametrize(
    "bad,code",
    [(b"\x01not a frame", 4400), (b"", 4400), (bytes([0xDE]) + b"x" * 200, 1009)],
)
def test_bad_frames_are_refused(client, ctx, sidecar, monkeypatch, bad, code):
    monkeypatch.setattr(shared_notes, "MAX_FRAME", 100)
    sid = meeting(ctx)
    with client.websocket_connect(url(sid)) as a:
        a.receive_json()
        a.send_bytes(bad)
        with pytest.raises(WebSocketDisconnect) as e:
            a.receive_bytes()
        assert e.value.code == code
    assert ctx.repo.notes_ops(sid) == []


# ---- folding ---------------------------------------------------------------------------


def test_fold_writes_notes_searchable_with_an_event(client, ctx, sidecar):
    sid = meeting(ctx)
    seen = events(ctx)
    with client.websocket_connect(url(sid)) as a:
        a.receive_json()
        a.send_bytes(frame("f1", ["ins", 0, "zebrafish budget\n"]))
        settle(a)
        assert shared_notes.fold_blocking(ctx, sid)
        assert ctx.repo.get(sid).notes == "zebrafish budget\n"
        hits = client.get("/api/search", params={"q": "zebrafish", "mode": "keyword"}).json()
        assert [h["session_id"] for h in hits] == [sid]
        wait_for(lambda: {"type": "notes", "session_id": sid} in seen)

        # Nothing unfolded: nothing to do. Unchanged markdown: no write.
        assert not shared_notes.fold_blocking(ctx, sid)
        before = ctx.repo.sync_state(sid)
        a.send_bytes(bytes([0xAF]) + b"an artifact")
        settle(a)
        n = len(seen)
        assert not shared_notes.fold_blocking(ctx, sid)
        assert not ctx.repo.has_unfolded_notes(sid)
        # The snapshot changed though the markdown did not: one new version for library sync
        # (or formatting and comment marks would never travel), and no more than one.
        device, revision = next(iter(before[1].items()))
        assert ctx.repo.sync_state(sid) == (before[0], {device: revision + 1})
        assert not shared_notes.fold_blocking(ctx, sid)
        assert ctx.repo.sync_state(sid)[1] == {device: revision + 1}
        time.sleep(0.05)
        assert not [e for e in seen[n:] if e.get("type") == "notes"]


def test_folds_after_the_last_frame(client, ctx, sidecar, monkeypatch):
    monkeypatch.setattr(shared_notes, "FOLD_DELAY", 0.05)
    sid = meeting(ctx)
    with client.websocket_connect(url(sid)) as a:
        a.receive_json()
        a.send_bytes(frame("f1", ["ins", 0, "soon\n"]))
        settle(a)
        wait_for(lambda: ctx.repo.get(sid).notes == "soon\n")


def test_summarize_folds_first(client, ctx, sidecar, fake_provider):
    from mnemosyne.models.transcript import TranscriptSegment

    sid = ctx.repo.save(
        Session(name="M", transcript=[TranscriptSegment(text="hi", speaker="A", start=0, end=1)])
    ).id
    with client.websocket_connect(url(sid)) as a:
        a.receive_json()
        a.send_bytes(frame("f1", ["ins", 0, "typed live\n"]))
        settle(a)
        job = client.post(f"/api/sessions/{sid}/summarize", json={"provider": "fake"}).json()
        done = wait_for(
            lambda: (
                (j := client.get(f"/api/jobs/{job['id']}").json())["status"]
                in ("completed", "failed", "cancelled")
                and j
            )
        )
        assert done["status"] == "completed", done
        assert ctx.repo.get(sid).notes == "typed live\n"


# ---- the notes route, seeding, no sidecar ----------------------------------------------


def test_notes_route_goes_through_the_document(client, ctx, sidecar):
    sid = meeting(ctx)
    with client.websocket_connect(url(sid)) as a:
        a.receive_json()
        r = client.post(f"/api/sessions/{sid}/notes", json={"notes": "from the api\n"})
        assert r.status_code == 200 and r.json()["notes"] == "from the api\n"
        got = json.loads(a.receive_bytes()[1:])
        assert got["ops"] == [["set", "from the api\n"]]
        assert a.receive_json()["by"] == "api"
    assert ctx.repo.get(sid).notes == "from the api\n"
    assert [o["author"] for o in ctx.repo.notes_ops(sid)] == ["api"]
    assert not ctx.repo.has_unfolded_notes(sid)


def test_notes_route_never_writes_around_a_document(client, ctx, sidecar, monkeypatch):
    sid = meeting(ctx)
    assert client.post(f"/api/sessions/{sid}/notes", json={"notes": "kept\n"}).status_code == 200
    assert ctx.repo.has_notes_doc(sid)
    monkeypatch.setenv("FAKE_NOTES_DOC_FAIL", "1")  # set_text fails from now on
    r = client.post(f"/api/sessions/{sid}/notes", json={"notes": "lost?\n"})
    assert r.status_code == 503
    assert r.json()["detail"] == "The notes could not be saved; try again"
    assert ctx.repo.get(sid).notes == "kept\n"
    assert [o["author"] for o in ctx.repo.notes_ops(sid)] == ["api"]


def test_notes_route_writes_directly_without_a_sidecar(client, ctx):
    sid = meeting(ctx, notes="before")
    r = client.post(f"/api/sessions/{sid}/notes", json={"notes": "after"})
    assert r.status_code == 200 and r.json()["notes"] == "after"
    assert ctx.repo.get(sid).notes == "after" and not ctx.repo.has_notes_doc(sid)


def test_the_document_catches_up_with_a_direct_write(client, ctx, sidecar, monkeypatch):
    sid = meeting(ctx)
    client.post(f"/api/sessions/{sid}/notes", json={"notes": "first\n"})
    # The sidecar goes missing: the route writes around the document.
    monkeypatch.setenv("MNEMOSYNE_LINK_BIN", str(sidecar.path.parent / "gone"))
    r = client.post(f"/api/sessions/{sid}/notes", json={"notes": "second line\n"})
    assert r.status_code == 200 and ctx.repo.get(sid).notes == "second line\n"
    assert len(ctx.repo.notes_ops(sid)) == 1
    # Back: the first join brings the document up to date before it is served.
    monkeypatch.setenv("MNEMOSYNE_LINK_BIN", str(sidecar.path))
    with client.websocket_connect(url(sid)) as a:
        hello = a.receive_json()
        assert hello["snapshot"] and hello["backlog"] == 0
        assert len(snapshot_frames(a.receive_bytes())) == 2
        assert [o["author"] for o in ctx.repo.notes_ops(sid)] == ["api", "api"]
        a.send_bytes(frame("f1", ["ins", 0, "> "]))
        settle(a)
        assert shared_notes.fold_blocking(ctx, sid)
        assert ctx.repo.get(sid).notes == "> second line\n"  # kept, not the older text
    assert ctx.repo.notes_doc(sid)["markdown"] == "> second line\n"


def test_a_fold_catches_up_without_a_join(client, ctx, sidecar, monkeypatch):
    sid = meeting(ctx)
    client.post(f"/api/sessions/{sid}/notes", json={"notes": "first\n"})
    ctx.repo.update_fields(sid, notes="written around it\n")  # e.g. sync's text merge
    shared_notes.fold_blocking(ctx, sid)
    assert ctx.repo.get(sid).notes == "written around it\n"
    assert ctx.repo.notes_doc(sid)["markdown"] == "written around it\n"
    assert not ctx.repo.has_unfolded_notes(sid) and ctx.repo.notes_drift(sid) is None


def test_a_fold_with_unchanged_notes_does_no_set_text(client, ctx, sidecar):
    sid = meeting(ctx, notes="seeded\n")
    with client.websocket_connect(url(sid)) as a:
        a.receive_json()
        a.receive_bytes()
        before = [r["op"] for r in sidecar.doc_requests]
        a.send_bytes(frame("f1", ["ins", 0, "# "]))
        settle(a)
        assert shared_notes.fold_blocking(ctx, sid)
    with client.websocket_connect(url(sid)) as b:  # a join with nothing to catch up
        b.receive_json()
    wait_for(lambda: not ctx.repo.has_unfolded_notes(sid))
    assert "set_text" not in [r["op"] for r in sidecar.doc_requests][len(before) :]
    assert ctx.repo.get(sid).notes == "# seeded\n"


def test_seeded_from_notes_only_where_it_was_recorded(client, ctx, sidecar):
    here = meeting(ctx, notes="written before\n")
    with client.websocket_connect(url(here)) as a:
        hello = a.receive_json()
        assert (hello["snapshot"], hello["backlog"], hello["read_only"]) == (True, 0, False)
        assert len(snapshot_frames(a.receive_bytes())) == 1
    assert [o["author"] for o in ctx.repo.notes_ops(here)] == ["seed"]
    assert ctx.repo.get(here).notes == "written before\n"

    elsewhere = meeting(ctx, notes="from the other computer\n", origin="otherdevice1")
    with client.websocket_connect(url(elsewhere)) as a:
        hello = a.receive_json()
        assert hello["read_only"] and hello["waiting_for_sync"] and not hello["snapshot"]
        a.send_bytes(frame("f1", ["ins", 0, "x"]))
        with pytest.raises(WebSocketDisconnect) as e:
            a.receive_bytes()
        assert e.value.code == 4403
    r = client.post(f"/api/sessions/{elsewhere}/notes", json={"notes": "edited\n"})
    assert r.json()["notes"] == "edited\n"
    assert ctx.repo.notes_ops(elsewhere) == [] and not ctx.repo.has_notes_doc(elsewhere)


def test_without_a_sidecar(client, ctx):
    sid = meeting(ctx)
    with client.websocket_connect(url(sid)) as a:
        assert a.receive_json() == {"type": "hello", "unavailable": True}
        with pytest.raises(WebSocketDisconnect) as e:
            a.receive_json()
        assert e.value.code == 1000
    r = client.post(f"/api/sessions/{sid}/notes", json={"notes": "plain *as given*"})
    assert r.status_code == 200 and ctx.repo.get(sid).notes == "plain *as given*"
    assert ctx.repo.notes_ops(sid) == []


def test_a_failing_sidecar_is_unavailable(client, ctx, sidecar, monkeypatch):
    monkeypatch.setenv("FAKE_NOTES_DOC_FAIL", "1")
    sid = meeting(ctx)
    with client.websocket_connect(url(sid)) as a:
        assert a.receive_json() == {"type": "hello", "unavailable": True}


def test_unknown_meeting_and_delete(client, ctx, sidecar):
    with client.websocket_connect(url("nosuchmeeting")) as a:
        with pytest.raises(WebSocketDisconnect) as e:
            a.receive_json()
        assert e.value.code == 4404
    sid = meeting(ctx)
    with client.websocket_connect(url(sid)) as a:
        a.receive_json()
        a.send_bytes(frame("f1", ["ins", 0, "gone soon\n"]))
        settle(a)
    wait_for(lambda: ctx.repo.get(sid).notes == "gone soon\n")
    with client.websocket_connect(url(sid)) as a:
        a.receive_json()
        a.receive_bytes()  # the snapshot
        assert client.delete(f"/api/sessions/{sid}").status_code == 200
        assert ctx.repo.notes_ops(sid) == [] and ctx.repo.notes_doc(sid) is None
        a.send_bytes(frame("f2", ["ins", 0, "too late"]))
        with pytest.raises(WebSocketDisconnect) as e:
            a.receive_bytes()
        assert e.value.code == 4404


def test_token_required(settings, keystore, sidecar):
    settings.api_token = "secret"
    app = create_app(settings, keystore=keystore)
    sid = meeting(app.state.ctx)
    with TestClient(app) as client:
        with pytest.raises(WebSocketDisconnect) as e:
            with client.websocket_connect(url(sid)) as a:
                a.receive_json()
        assert e.value.code == 4401
        with client.websocket_connect(url(sid) + "?token=secret") as a:
            assert a.receive_json()["read_only"] is False


# ---- a team ----------------------------------------------------------------------------


@pytest.fixture
def team(settings, keystore, sidecar):
    settings.team_mode = True
    app = create_app(settings, keystore=keystore)
    ctx = app.state.ctx
    people = {}
    for name, role in [
        ("Ann Lee", "member"),
        ("Bob Stone", "member"),
        ("Cat", "member"),
        ("Rita Vance", "reviewer"),
    ]:
        user = ctx.users.add(name, f"{name.split()[0].lower()}@team.test", role)
        code, _ = ctx.users.invite(user.id)
        people[name.split()[0]] = (user, ctx.users.redeem(code, "test")[1])
    with TestClient(app) as client:
        yield client, ctx, people


def _team_meeting(ctx, user) -> str:
    reset = access.current.set(access.Principal(user.id, user.name, user.role))
    try:
        return ctx.repo.save(Session(name="Planning")).id
    finally:
        access.current.reset(reset)


def test_team_who_edits_and_who_reads(team):
    client, ctx, people = team
    ann, bob = people["Ann"], people["Bob"]
    sid = _team_meeting(ctx, ann[0])
    sharing.set_shares(ctx, sid, {bob[0].id})

    def connect(who):
        return client.websocket_connect(f"{url(sid)}?token={people[who][1]}")

    with connect("Cat") as c:  # not shared with Cat: as if there were nothing
        with pytest.raises(WebSocketDisconnect) as e:
            c.receive_json()
        assert e.value.code == 4404

    with connect("Ann") as a:
        hello_a = a.receive_json()
        assert hello_a["you"] == {"id": ann[0].id, "name": "Ann Lee"}
        assert not hello_a["read_only"]
        with connect("Bob") as b:
            hello_b = b.receive_json()
            assert not hello_b["read_only"] and hello_b["present"] == ["Ann Lee", "Bob Stone"]
            assert a.receive_json()["present"] == ["Ann Lee", "Bob Stone"]

            b.send_bytes(frame("f1", ["ins", 0, "Bob's line\n"]))
            assert a.receive_bytes() == frame("f1", ["ins", 0, "Bob's line\n"])
            last = a.receive_json()
            assert (last["type"], last["by"]) == ("last_edit", "Bob Stone")
            assert ctx.repo.notes_doc(sid)["last_by"] == "Bob Stone"
            b.send_text(json.dumps({"type": "presence", "typing": True}))
            assert a.receive_json() == {
                "type": "presence",
                "present": ["Ann Lee", "Bob Stone"],
                "typing": ["Bob Stone"],
            }
            assert [o["author_id"] for o in ctx.repo.notes_ops(sid)] == [bob[0].id]

            with connect("Rita") as r:
                hello_r = r.receive_json()
                assert hello_r["read_only"] and hello_r["backlog"] == 1
                assert r.receive_bytes() == frame("f1", ["ins", 0, "Bob's line\n"])
                r.send_text(json.dumps({"type": "presence", "typing": True}))  # ignored
                r.send_bytes(frame("f2", ["ins", 0, "Rita was here\n"]))
                with pytest.raises(WebSocketDisconnect) as e:
                    r.receive_bytes()
                assert e.value.code == 4403
            assert len(ctx.repo.notes_ops(sid)) == 1
            presence = a.receive_json()  # Rita joined (and left)
            assert presence["typing"] == ["Bob Stone"]

    # The notes route follows the same rule.
    headers = {"Authorization": f"Bearer {people['Rita'][1]}"}
    r = client.post(f"/api/sessions/{sid}/notes", json={"notes": "x"}, headers=headers)
    assert r.status_code == 403
    headers = {"Authorization": f"Bearer {bob[1]}"}
    r = client.post(
        f"/api/sessions/{sid}/notes", json={"notes": "Bob wrote this\n"}, headers=headers
    )
    assert r.status_code == 200 and r.json()["notes"] == "Bob wrote this\n"


def test_shared_with_everyone_edits(team):
    client, ctx, people = team
    sid = _team_meeting(ctx, people["Ann"][0])
    sharing.set_shares(ctx, sid, {sharing.EVERYONE})
    with client.websocket_connect(f"{url(sid)}?token={people['Rita'][1]}") as r:
        assert r.receive_json()["read_only"] is False


@needs_ffmpeg
def test_combining_keeps_the_live_notes(client, ctx, sidecar):
    t0 = datetime(2026, 9, 28, 10, 0)
    later = _meeting(
        ctx, "Planning", 1.0, t0 + timedelta(minutes=10), [("Ana", 0.2, "hi")], {},
        notes="Planning notes",
    )  # fmt: skip
    earlier = _meeting(
        ctx, "Dropped", 1.0, t0, [("Bob", 0.2, "yes")], {}, notes="Before the drop"
    )  # fmt: skip
    with client.websocket_connect(url(later)) as a:
        a.receive_json()
        a.receive_bytes()  # seeded from "Planning notes"
        a.send_bytes(frame("f1", ["ins", 14, "\nlive line"]))  # not folded yet
        settle(a)
        job = client.post(f"/api/sessions/{later}/combine", json={"other_id": earlier}).json()
        assert _wait(client, job["id"])["status"] == "completed"
        update = json.loads(a.receive_bytes()[1:])  # the combined text, as a frame
        assert update["ops"][0][0] == "set"
    notes = ctx.repo.get(later).notes
    assert notes == "Planning notes\nlive line\n\n## From “Dropped”\n\nBefore the drop"
    assert [o["author"] for o in ctx.repo.notes_ops(later)] == ["seed", "Me", "api"]


@needs_ffmpeg
def test_combining_fails_rather_than_writing_around_the_document(client, ctx, sidecar, monkeypatch):
    t0 = datetime(2026, 9, 28, 10, 0)
    later = _meeting(ctx, "Planning", 1.0, t0, [("Ana", 0.2, "hi")], {}, notes="Planning notes")
    earlier = _meeting(ctx, "Dropped", 1.0, t0, [("Bob", 0.2, "yes")], {}, notes="Before")
    assert shared_notes.set_markdown_blocking(ctx, later, "Planning notes\n")  # a document
    monkeypatch.setenv("FAKE_NOTES_DOC_FAIL", "1")
    job = client.post(f"/api/sessions/{later}/combine", json={"other_id": earlier}).json()
    done = _wait(client, job["id"])
    assert done["status"] == "failed" and "nothing was combined" in done["error"]
    assert ctx.repo.get(later).notes == "Planning notes\n"
    # The notes come first: nothing else changed.
    assert ctx.repo.get(earlier).notes == "Before"
    assert [r.part for r in ctx.repo.get(later).recordings] == [0]
    assert [x.text for x in ctx.repo.get(later).transcript] == ["hi"]


# ---- hidden members (the guest bridge's tap on the room, phase 9) -----------------------------


class FakeWs:
    """A WebSocket that only records what the member's sender writes."""

    def __init__(self) -> None:
        self.sent: list = []

    async def send_json(self, data) -> None:
        self.sent.append(data)

    async def send_bytes(self, data) -> None:
        self.sent.append(bytes(data))

    async def close(self, code=1000) -> None:
        self.sent.append(("close", code))


def tap(name: str = "Bridge") -> shared_notes.Member:
    return shared_notes.Member(ws=None, id="bridge", name=name, can_edit=False)


def drain(member: shared_notes.Member) -> list:
    out = []
    while not member.out.empty():
        out.append(member.out.get_nowait())
    return out


async def pending(ctx) -> None:
    """Folds and other tasks the hub started have finished."""
    while ctx.notes_hub.tasks:
        await asyncio.gather(*list(ctx.notes_hub.tasks))


@pytest.mark.anyio
async def test_a_hidden_member_is_not_present_and_gets_every_frame(client, ctx, sidecar):
    sid = meeting(ctx)
    ws = FakeWs()
    ann = await shared_notes.join(ctx, sid, ws, "u1", "Ann", True)
    assert ann is not None
    bridge = tap()
    shared_notes.attach(ctx, sid, bridge)
    room = ctx.notes_hub.rooms[sid]
    assert bridge.hidden and bridge in room.members
    bridge.typing = True
    assert shared_notes._presence(room) == {"type": "presence", "present": ["Ann"], "typing": []}
    assert drain(bridge) == []  # no hello, no presence, no sender
    assert bridge.sender is None

    # Typed by Ann: relayed to the tap, not echoed to her; the tap hears no JSON.
    f1 = frame("f1", ["ins", 0, "hi\n"])
    assert shared_notes.receive_frame(ctx, sid, ann, f1) is None
    assert drain(bridge) == [("bytes", f1)]
    await asyncio.sleep(0.05)
    assert f1 not in ws.sent  # never echoed to the one who typed it

    # A visible member's presence changes do not reach the tap either.
    shared_notes.receive_text(ctx, sid, ann, json.dumps({"type": "presence", "typing": True}))
    assert drain(bridge) == []
    other = await shared_notes.join(ctx, sid, FakeWs(), "u2", "Bob", True)
    assert other is not None and drain(bridge) == []

    # Every path ends in _deliver: set_markdown goes through the sidecar and the loop.
    assert await shared_notes.set_markdown(ctx, sid, "hi\nthere\n")
    await asyncio.sleep(0.05)
    frames = [d for k, d in drain(bridge) if k == "bytes"]
    assert frames, "set_markdown reached the tap"
    assert frames[0] in ws.sent  # and the visible members alike
    for m in (ann, other):
        await shared_notes.leave(ctx, sid, m)
    shared_notes.detach(ctx, sid, bridge)


@pytest.mark.anyio
async def test_frames_injected_by_the_tap_are_not_delivered_back(client, ctx, sidecar):
    sid = meeting(ctx)
    ann = await shared_notes.join(ctx, sid, FakeWs(), "u1", "Ann", True)
    bridge = tap()
    shared_notes.attach(ctx, sid, bridge)
    drain(ann)
    f1 = frame("f1", ["ins", 0, "from a guest\n"])
    (seq,) = shared_notes.accept_frames(
        ctx, sid, [f1], "guest:abc", "Ana (guest)", skip=bridge, can_edit=True
    )
    assert drain(bridge) == []
    assert ("bytes", f1) in drain(ann)
    assert [(o["seq"], o["author_id"], o["author"]) for o in ctx.repo.notes_ops(sid)] == [
        (seq, "guest:abc", "Ana (guest)")
    ]
    # Without `skip` it would have been delivered.
    f2 = frame("f2", ["ins", 0, "x"])
    shared_notes.accept_frames(ctx, sid, [f2], "guest:abc", "Ana (guest)", can_edit=True)
    assert drain(bridge) == [("bytes", f2)]
    await shared_notes.leave(ctx, sid, ann)
    shared_notes.detach(ctx, sid, bridge)
    await pending(ctx)


@pytest.mark.anyio
async def test_attach_starts_after_the_frames_already_logged(client, ctx, sidecar):
    sid = meeting(ctx)
    old = frame("f1", ["ins", 0, "old\n"])
    ctx.repo.append_notes_ops(sid, [old], "", "Me", True)
    bridge = tap()
    shared_notes.attach(ctx, sid, bridge)
    assert bridge.seen_seq == 1
    shared_notes._deliver(ctx, sid, [(1, old)], None)  # already in its catch-up
    assert drain(bridge) == []
    new = frame("f2", ["ins", 0, "new\n"])
    shared_notes._deliver(ctx, sid, [(2, new)], None)
    assert drain(bridge) == [("bytes", new)]
    shared_notes.detach(ctx, sid, bridge)
    await pending(ctx)


@pytest.mark.anyio
async def test_detaching_the_last_member_folds_like_leave(client, ctx, sidecar):
    sid = meeting(ctx)
    bridge = tap()
    shared_notes.attach(ctx, sid, bridge)
    shared_notes.accept_frames(
        ctx, sid, [frame("f1", ["ins", 0, "kept\n"])], "guest:abc", "Ana (guest)", can_edit=True
    )
    assert ctx.repo.has_unfolded_notes(sid)
    shared_notes.detach(ctx, sid, bridge)
    assert sid not in ctx.notes_hub.rooms
    await pending(ctx)
    assert ctx.repo.get(sid).notes == "kept\n" and not ctx.repo.has_unfolded_notes(sid)
    shared_notes.detach(ctx, sid, bridge)  # again: nothing


@pytest.mark.anyio
async def test_visible_and_hidden_members_leave_in_either_order(client, ctx, sidecar):
    sid = meeting(ctx)
    ann = await shared_notes.join(ctx, sid, FakeWs(), "u1", "Ann", True)
    bridge = tap()
    shared_notes.attach(ctx, sid, bridge)
    bob = await shared_notes.join(ctx, sid, FakeWs(), "u2", "Bob", True)
    assert bob is not None
    shared_notes.accept_frames(
        ctx, sid, [frame("f1", ["ins", 0, "typed\n"])], "u1", "Ann", skip=ann, can_edit=True
    )
    drain(bridge)
    # The tap leaving while people are there changes nothing for them.
    shared_notes.detach(ctx, sid, bridge)
    assert sid in ctx.notes_hub.rooms and not ctx.notes_hub.tasks
    shared_notes.attach(ctx, sid, bridge)
    await shared_notes.leave(ctx, sid, ann)
    assert ctx.notes_hub.rooms[sid].members == [bob, bridge]
    # The last visible member out folds, though the tap stays (and the room with it).
    await shared_notes.leave(ctx, sid, bob)
    assert sid in ctx.notes_hub.rooms and ctx.notes_hub.rooms[sid].members == [bridge]
    await pending(ctx)
    assert ctx.repo.get(sid).notes == "typed\n"
    shared_notes.detach(ctx, sid, bridge)
    assert sid not in ctx.notes_hub.rooms
    await pending(ctx)


@pytest.mark.anyio
async def test_broadcast_json_reaches_visible_members_only(client, ctx, sidecar):
    sid = meeting(ctx)
    shared_notes.broadcast_json(ctx, sid, {"type": "guests"})  # no room: nothing
    ws_a, ws_b = FakeWs(), FakeWs()
    ann = await shared_notes.join(ctx, sid, ws_a, "u1", "Ann", True)
    bob = await shared_notes.join(ctx, sid, ws_b, "u2", "Bob", False)
    assert ann is not None and bob is not None
    bridge = tap()
    shared_notes.attach(ctx, sid, bridge)
    await asyncio.sleep(0.05)
    for ws in (ws_a, ws_b):
        ws.sent.clear()
    msg = {"type": "guests", "present": ["Ana"], "typing": []}
    shared_notes.broadcast_json(ctx, sid, msg)
    await asyncio.sleep(0.05)
    assert ws_a.sent == [msg] and ws_b.sent == [msg]
    assert drain(bridge) == []
    for m in (ann, bob):
        await shared_notes.leave(ctx, sid, m)
    shared_notes.detach(ctx, sid, bridge)
    await pending(ctx)
