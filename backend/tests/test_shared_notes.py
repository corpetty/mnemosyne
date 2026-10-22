"""Live shared notes (services/shared_notes.py, api/notes_ws.py): the relay, its log, folding
into Session.notes, access, the notes route through the document, seeding. The sidecar is the
`notes doc` stand-in of tests/fake_link.py (toy frames, see its docstring)."""

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
