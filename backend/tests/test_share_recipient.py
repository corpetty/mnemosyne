"""A meeting someone shared with me, on my desktop (phase 8, task 15; C6, C7, C9): what I may do
there with no Principal (comments as me, ticks, the live notes, on the shared parts only), what
stays refused (rename, item text), deleting my copy, and the `shared_in_*` settings that keep it
out of Ask, the semantic index, the digest and Obsidian."""

from datetime import datetime

import pytest
from starlette.websockets import WebSocketDisconnect

from mnemosyne.models.session import ActionItem, Session, SessionStatus, SummaryData
from mnemosyne.models.transcript import TranscriptSegment
from mnemosyne.services import shared_notes, shared_with_me
from tests import fake_link
from tests.conftest import drain_until_job
from tests.test_shared_notes import frame, settle, wait_for

OWNER = "a" * 64
OWNER_PID = "p:" + OWNER[:16]


@pytest.fixture(autouse=True)
def no_timed_folds(monkeypatch):
    monkeypatch.setattr(shared_notes, "FOLD_DELAY", 1000.0)
    monkeypatch.setattr(shared_notes, "FOLD_EVERY", 1000.0)


@pytest.fixture
def sidecar(tmp_path, monkeypatch):
    return fake_link.install(tmp_path, monkeypatch)


@pytest.fixture
def parts(monkeypatch):
    """The stored parts of the share (what the pulled `shared_meetings` row would say)."""
    stored: dict = {}
    monkeypatch.setattr(shared_with_me, "_stored_parts", lambda app, sid: dict(stored))
    return stored


@pytest.fixture
def me(ctx):
    ctx.settings.local_speaker_name = "Corey"
    return ctx.person.pid


def _contact(ctx):
    ctx.repo.put_contact(
        {
            "person": OWNER,
            "name": "Ada",
            "my_name": "",
            "card": {"name": "Ada", "machines": [], "at": "2026-10-23T10:00:00+00:00"},
            "secret": "s" * 64,
            "created_at": "2026-10-01T00:00:00+00:00",
            "updated_at": "2026-10-01T00:00:00+00:00",
        }
    )


def _shared(ctx, name="Roadmap with Ada", notes="", when=None, owner=OWNER) -> str:
    """A meeting as a pull leaves it: saved, then marked shared_from (the app's own write)."""
    _contact(ctx)
    session = Session(
        name=name,
        status=SessionStatus.COMPLETED,
        created_at=when or datetime(2026, 9, 22, 10),
        notes=notes,
        transcript=[
            TranscriptSegment(
                text="The zeppelin launch moves to spring", speaker="Ada", start=0.0, end=4.0
            ),
            TranscriptSegment(text="Fine by me", speaker="Corey", start=4.5, end=6.0),
        ],
        summary="We moved the zeppelin launch to spring.",
        summary_data=SummaryData(
            decisions=["Launch the zeppelin in spring"],
            action_items=[ActionItem(text="Book the hangar", owner="Corey")],
        ),
    )
    sid = ctx.repo.save(session).id
    with ctx.repo._lock, ctx.repo._conn:
        ctx.repo._conn.execute("UPDATE sessions SET shared_from=? WHERE id=?", (owner, sid))
    return sid


def _comments(sid, rest=""):
    return f"/api/sessions/{sid}/comments{rest}"


def _owner_comment(ctx, sid, text="From Ada") -> str:
    return ctx.repo.add_comment(sid, text=text, author_id=OWNER_PID, author="Ada")


# ---- the meeting itself ------------------------------------------------------------------------


def test_renaming_is_refused_with_the_sharers_name(client, ctx, me, parts):
    sid = _shared(ctx)
    r = client.patch(f"/api/sessions/{sid}", json={"name": "Mine now"})
    assert r.status_code == 403
    assert r.json()["detail"].startswith("Shared by Ada")
    assert ctx.repo.get(sid).name == "Roadmap with Ada"


def test_deleting_removes_only_my_copy(client, ctx, me, parts):
    sid = _shared(ctx)
    assert client.delete(f"/api/sessions/{sid}").status_code == 200
    assert ctx.repo.get(sid) is None
    assert ctx.repo._conn.execute("SELECT COUNT(*) FROM sync_tombstones").fetchone()[0] == 0


# ---- comments ----------------------------------------------------------------------------------


def test_i_comment_as_myself_and_change_only_my_own(client, ctx, me, parts):
    sid = _shared(ctx)
    r = client.post(_comments(sid), json={"text": "Spring works"})
    assert r.status_code == 200, r.text
    mine = r.json()
    assert (mine["author_id"], mine["author"]) == (me, "Corey")
    assert mine["can_edit"] and mine["can_delete"] and mine["can_resolve"]
    r = client.patch(_comments(sid, f"/{mine['id']}"), json={"text": "Spring works for me"})
    assert r.status_code == 200 and r.json()["text"] == "Spring works for me"

    theirs = _owner_comment(ctx, sid)
    root = next(t["root"] for t in client.get(_comments(sid)).json()["threads"]
                if t["root"]["id"] == theirs)  # fmt: skip
    assert not (root["can_edit"] or root["can_delete"] or root["can_resolve"])
    assert client.patch(_comments(sid, f"/{theirs}"), json={"text": "x"}).status_code == 403
    assert client.delete(_comments(sid, f"/{theirs}")).status_code == 403
    assert ctx.repo.get_comment(theirs).text == "From Ada"

    # I may answer their thread; resolving is the owner's or the thread's author's.
    reply = client.post(_comments(sid), json={"text": "Agreed", "parent_id": theirs})
    assert reply.status_code == 200 and reply.json()["author_id"] == me
    assert client.post(_comments(sid, f"/{theirs}/resolve")).status_code == 403
    r = client.post(_comments(sid, f"/{mine['id']}/resolve"))
    assert r.status_code == 200 and r.json()["resolved_by"] == "Corey"
    assert client.delete(_comments(sid, f"/{mine['id']}")).status_code == 200


def test_comments_only_on_the_shared_parts(client, ctx, me, parts):
    sid = _shared(ctx)  # the defaults: no transcript
    r = client.post(_comments(sid), json={"text": "Hm", "target": "segment", "idx": 0})
    assert r.status_code == 403
    assert r.json()["detail"] == "This meeting's transcript is not shared with you"
    parts.update(summary=False)
    r = client.post(_comments(sid), json={"text": "Hm", "target": "body", "exact": "spring"})
    assert r.status_code == 403 and "summary is not shared" in r.json()["detail"]
    parts.update(transcript=True)
    r = client.post(_comments(sid), json={"text": "Hm", "target": "segment", "idx": 0})
    assert r.status_code == 200, r.text
    # A thread on a part that is no longer shared cannot be answered or changed.
    parts.update(transcript=False)
    cid = r.json()["id"]
    assert client.post(_comments(sid), json={"text": "x", "parent_id": cid}).status_code == 403
    assert client.patch(_comments(sid, f"/{cid}"), json={"text": "y"}).status_code == 403


def test_no_author_id_on_a_shared_meeting_is_its_owner_never_me(client, ctx, me, parts):
    sid = _shared(ctx)
    cid = ctx.repo.add_comment(sid, text="Old style", author_id="", author="Ada")
    root = client.get(_comments(sid)).json()["threads"][0]["root"]
    assert root["id"] == cid and root["author_id"] == OWNER_PID
    assert not root["can_edit"]
    assert client.patch(_comments(sid, f"/{cid}"), json={"text": "x"}).status_code == 403


def test_my_own_meetings_keep_the_desktop_rules(client, ctx, me, parts):
    sid = ctx.sessions.create_session("Mine").id
    c = client.post(_comments(sid), json={"text": "Note"}).json()
    assert (c["author_id"], c["author"]) == ("", "Corey")
    cid = ctx.repo.add_comment(sid, text="Synced", author_id="p:" + "b" * 16, author="Bo")
    assert client.post(_comments(sid, f"/{cid}/resolve")).status_code == 200


# ---- action items ------------------------------------------------------------------------------


def test_ticks_need_the_action_items_shared_and_text_edits_are_refused(client, ctx, me, parts):
    sid = _shared(ctx)
    url = f"/api/sessions/{sid}/action-items/0"
    r = client.patch(url, json={"done": True, "follow": True})
    assert r.status_code == 200, r.text
    assert r.json()["done"] and r.json()["follow"] and not r.json()["can_edit"]
    assert ctx.repo.get(sid).summary_data.action_items[0].done
    listed = [
        t for t in client.get("/api/action-items?status=all").json() if t["session_id"] == sid
    ]
    assert listed and not listed[0]["can_edit"]

    r = client.patch(url, json={"text": "Book two hangars"})
    assert r.status_code == 403 and r.json()["detail"].startswith("Shared by Ada")
    assert client.patch(url, json={"due": "2026-11-01"}).status_code == 403
    assert ctx.repo.get(sid).summary_data.action_items[0].text == "Book the hangar"

    parts.update(action_items=False)
    r = client.patch(url, json={"done": False})
    assert r.status_code == 403
    assert r.json()["detail"] == "This meeting's action items are not shared with you"
    assert ctx.repo.get(sid).summary_data.action_items[0].done


# ---- live notes --------------------------------------------------------------------------------


def test_typing_in_the_notes_when_they_are_shared(client, ctx, me, parts, sidecar):
    sid = _shared(ctx)
    with client.websocket_connect(f"/ws/notes/{sid}") as ws:
        hello = ws.receive_json()
        assert (
            hello["you"]["id"] == me and hello["you"]["name"] == "Corey" and not hello["read_only"]
        )
        ws.send_bytes(frame("f1", ["ins", 0, "hi\n"]))
        settle(ws)
        assert [(o["author_id"], o["author"]) for o in ctx.repo.notes_ops(sid)] == [(me, "Corey")]
    # The fold writes Session.notes although the meeting refuses generic writes.
    wait_for(lambda: ctx.repo.get(sid).notes == "hi\n")


def test_notes_not_shared_are_read_only(client, ctx, me, parts, sidecar):
    sid = _shared(ctx)
    parts.update(notes=False)
    with client.websocket_connect(f"/ws/notes/{sid}") as ws:
        assert ws.receive_json()["read_only"]
        ws.send_bytes(frame("f1", ["ins", 0, "x"]))
        with pytest.raises(WebSocketDisconnect) as e:
            ws.receive_bytes()
        assert e.value.code == 4403
    assert ctx.repo.notes_ops(sid) == []
    r = client.post(f"/api/sessions/{sid}/notes", json={"notes": "mine\n"})
    assert (
        r.status_code == 403
        and r.json()["detail"] == "This meeting's notes are not shared with you"
    )


def test_the_document_is_never_seeded_here(client, ctx, me, parts, sidecar):
    sid = _shared(ctx, notes="Ada's notes\n")
    with client.websocket_connect(f"/ws/notes/{sid}") as ws:
        hello = ws.receive_json()
        assert hello["waiting_for_sync"] and hello["read_only"] and not hello["snapshot"]
    assert ctx.repo.notes_ops(sid) == [] and not ctx.repo.has_notes_doc(sid)
    # Nor written around the document that is on its way.
    r = client.post(f"/api/sessions/{sid}/notes", json={"notes": "mine\n"})
    assert r.status_code == 409
    assert ctx.repo.get(sid).notes == "Ada's notes\n"


def test_the_notes_route_goes_through_the_document_as_me(client, ctx, me, parts, sidecar):
    sid = _shared(ctx)
    r = client.post(f"/api/sessions/{sid}/notes", json={"notes": "agenda\n"})
    assert r.status_code == 200, r.text
    assert r.json()["notes"] == "agenda\n"
    assert [(o["author_id"], o["author"]) for o in ctx.repo.notes_ops(sid)] == [(me, "Corey")]


def test_the_notes_route_without_a_sidecar_is_refused(client, ctx, me, parts):
    sid = _shared(ctx)
    r = client.post(f"/api/sessions/{sid}/notes", json={"notes": "agenda\n"})
    assert r.status_code == 409
    assert ctx.repo.get(sid).notes == ""


# ---- shared_in_* -------------------------------------------------------------------------------


def _job(client, path, body):
    with client.websocket_connect("/ws") as ws:
        ws.receive_json()
        r = client.post(path, json=body)
        assert r.status_code == 200, r.text
        drain_until_job(ws, r.json()["id"])
    return client.get(f"/api/jobs/{r.json()['id']}").json()


def test_ask_leaves_shared_meetings_out_unless_asked(client, ctx, me, parts, fake_provider):
    sid = _shared(ctx)
    fake_provider.reply = "Spring [1]."
    job = _job(client, "/api/ask", {"question": "When is the zeppelin launch?", "provider": "fake"})
    assert job["status"] == "completed", job["error"]
    assert job["result"]["citations"] == [] and not fake_provider.calls
    ctx.settings.shared_in_ask = True
    job = _job(client, "/api/ask", {"question": "When is the zeppelin launch?", "provider": "fake"})
    assert [c["session_id"] for c in job["result"]["citations"]] == [sid]


def test_the_semantic_index_skips_them_and_keyword_search_finds_them(client, ctx, me, parts):
    sid = _shared(ctx)
    assert ctx.index.index_session(sid) == 0
    assert ctx.repo.vector_hashes(sid) == {}
    hits = client.get("/api/search", params={"q": "zeppelin", "mode": "keyword"}).json()
    assert [h["session_id"] for h in hits] == [sid]

    r = client.put("/api/settings", json={"shared_in_search": True})
    assert r.status_code == 200 and r.json()["values"]["shared_in_search"] is True
    assert ctx.index.shared and sid in ctx.index._pending
    assert ctx.index.index_session(sid) > 0
    ctx.index.include_shared(False)
    assert ctx.repo.vector_hashes(sid) == {}
    ctx.index.include_shared(True)
    assert ctx.index.index_session(sid) > 0  # its stamp was forgotten with the vectors


def test_the_digest_leaves_them_out_unless_asked(client, ctx, me, parts, fake_provider):
    _shared(ctx)
    fake_provider.reply = "## Overview\nZeppelins."
    body = {"start": "2026-09-21", "end": "2026-09-27", "provider": "fake"}
    job = _job(client, "/api/digests", body)
    assert job["status"] == "failed" and "No summarized meetings" in job["error"]
    ctx.settings.shared_in_digest = True
    job = _job(client, "/api/digests", body)
    assert job["status"] == "completed", job["error"]
    assert "Roadmap with Ada" in job["result"]["markdown"]


def test_obsidian_leaves_them_out_unless_asked(client, ctx, me, parts, tmp_path):
    sid = _shared(ctx)
    vault = tmp_path / "vault"
    vault.mkdir()
    ctx.settings.obsidian_vault_path = str(vault)
    r = client.post(f"/api/sessions/{sid}/export/obsidian")
    assert r.status_code == 409 and "shared with you" in r.json()["detail"]
    job = _job(client, "/api/export/obsidian/all", {})
    assert job["result"]["total"] == 0
    assert not list(vault.rglob("*.md"))

    ctx.settings.shared_in_obsidian = True
    r = client.post(f"/api/sessions/{sid}/export/obsidian")
    assert r.status_code == 200, r.text
    job = _job(client, "/api/export/obsidian/all", {})
    assert job["result"] == {"exported": 1, "failed": 0, "total": 1}
    assert any("Roadmap with Ada" in p.name for p in vault.rglob("*.md"))
