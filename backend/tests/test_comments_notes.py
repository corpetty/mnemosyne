"""Comments on the live shared notes (services/comments.py, "notes" target): the server makes
the thread and names the mark, the client marks the range in the live document, the fold
reports where the mark is. Placed at read time from the ranges stored with the fold (both the
fold and the sync union store them). The sidecar is the `notes doc` stand-in of
tests/fake_link.py, whose toy `["comment", id, start, end]` op marks a range."""

import base64

import pytest

from mnemosyne.models.session import Session
from mnemosyne.services import comments, shared_notes
from tests import fake_link
from tests.fake_link import doc_frame
from tests.test_shared_notes import _team_meeting, no_timed_folds, settle, team  # noqa: F401
from tests.test_sync_notes_doc import Node, doc_frames, shared_doc, write_doc  # noqa: F401
from tests.test_sync_service import nodes, pull  # noqa: F401


@pytest.fixture
def sidecar(tmp_path, monkeypatch):
    return fake_link.install(tmp_path, monkeypatch)


def frame(frame_id: str, *ops) -> bytes:
    return base64.b64decode(doc_frame(frame_id, *ops))


def _meeting(ctx) -> str:
    return ctx.repo.save(Session(name="Planning")).id


def _url(sid, rest=""):
    return f"/api/sessions/{sid}/comments{rest}"


def _fold(ctx, sid, *frames: bytes) -> None:
    ctx.repo.append_notes_ops(sid, list(frames), "", "Me", True)
    shared_notes.fold_blocking(ctx, sid)
    assert not ctx.repo.has_unfolded_notes(sid)


def _post_notes(client, sid, quote="launch plan", **extra):
    r = client.post(_url(sid), json={"text": "Why?", "target": "notes", "exact": quote, **extra})
    return r


def _root(client, sid, cid):
    for t in client.get(_url(sid)).json()["threads"]:
        if t["root"]["id"] == cid:
            return t["root"]
    raise AssertionError(f"no thread {cid}")


def test_creating_a_thread_on_the_notes_names_its_mark(client, ctx, sidecar):
    sid = _meeting(ctx)
    r = _post_notes(client, sid)
    assert r.status_code == 200, r.text  # the API answers 200 for every comment
    c = r.json()
    assert c["target"] == "notes" and c["target_id"] == ""
    assert c["anchor"] == {"mark": f"comment:{c['id']}"}
    assert c["quote"] == "launch plan"
    # Not marked yet: shown detached, with its quote.
    assert c["detached"] and c["notes_range"] is None and not c["changed"]
    assert _root(client, sid, c["id"])["quote"] == "launch plan"
    reply = client.post(_url(sid), json={"text": "Because", "parent_id": c["id"]})
    assert reply.status_code == 200 and reply.json()["parent_id"] == c["id"]


def test_a_notes_thread_needs_the_selected_text(client, ctx, sidecar):
    sid = _meeting(ctx)
    assert _post_notes(client, sid, quote="  ").status_code == 400


def test_a_notes_thread_is_refused_without_a_sidecar(client, ctx):
    sid = _meeting(ctx)
    r = _post_notes(client, sid)
    assert r.status_code == 409
    assert r.json()["detail"] == "The notes are not shared on this server"
    assert client.get(_url(sid)).json()["threads"] == []
    # Other comments are not affected.
    assert client.post(_url(sid), json={"text": "Hi"}).status_code == 200


def test_after_the_fold_the_thread_is_attached_to_its_range(client, ctx, sidecar):
    sid = _meeting(ctx)
    cid = _post_notes(client, sid).json()["id"]
    text = "Our launch plan is set\n"
    _fold(ctx, sid, frame("f1", ["ins", 0, text], ["comment", cid, 4, 15]))
    root = _root(client, sid, cid)
    assert root["notes_range"] == [4, 15] and not root["detached"] and not root["changed"]
    assert text[4:15] == "launch plan"
    marks = ctx.repo.notes_marks(sid)
    assert marks == (text, {cid: [4, 15]})
    # Resolve and reopen leave the anchor alone.
    assert client.post(_url(sid, f"/{cid}/resolve")).status_code == 200
    assert _root(client, sid, cid)["notes_range"] == [4, 15]
    # The range follows the next fold.
    _fold(ctx, sid, frame("f2", ["ins", 0, "Hey! "], ["comment", cid, 9, 20]))
    assert _root(client, sid, cid)["notes_range"] == [9, 20]


def test_a_cell_separator_reads_as_a_tab(client, ctx, sidecar):
    sid = _meeting(ctx)
    cid = _post_notes(client, sid, quote="a\tb").json()["id"]
    _fold(ctx, sid, frame("f1", ["ins", 0, "a\x1fb\n"], ["comment", cid, 0, 3]))
    assert not _root(client, sid, cid)["changed"]


def test_a_changed_quote_is_flagged(client, ctx, sidecar):
    sid = _meeting(ctx)
    cid = _post_notes(client, sid).json()["id"]
    _fold(
        ctx, sid, frame("f1", ["ins", 0, "Our launch schedule is set\n"], ["comment", cid, 4, 19])
    )
    root = _root(client, sid, cid)
    assert root["notes_range"] == [4, 19] and root["changed"] and not root["detached"]
    # Whitespace and case do not count as a change.
    other = _post_notes(client, sid, quote="IS  Set").json()["id"]
    _fold(ctx, sid, frame("f2", ["comment", other, 20, 26]))
    assert not _root(client, sid, other)["changed"]


def test_a_deleted_range_is_detached(client, ctx, sidecar):
    sid = _meeting(ctx)
    cid = _post_notes(client, sid).json()["id"]
    _fold(ctx, sid, frame("f1", ["ins", 0, "launch plan\n"], ["comment", cid, 0, 11]))
    assert not _root(client, sid, cid)["detached"]
    _fold(ctx, sid, frame("f2", ["del", 0, 11], ["comment", cid, None, None]))
    root = _root(client, sid, cid)
    assert root["detached"] and root["notes_range"] is None and root["quote"] == "launch plan"
    # A thread with no mark in the document at all is detached too.
    lone = _post_notes(client, sid).json()["id"]
    assert _root(client, sid, lone)["detached"]


def test_ranges_are_unknown_before_any_fold_stored_them(client, ctx, sidecar):
    sid = _meeting(ctx)
    cid = _post_notes(client, sid).json()["id"]
    ctx.repo.store_notes_fold(sid, b"{}", 0, "x\n")  # no plain, no ranges
    assert ctx.repo.notes_marks(sid) is None
    assert _root(client, sid, cid)["detached"]


def test_search_finds_a_notes_comment_by_its_words_and_quote(client, ctx, sidecar):
    sid = _meeting(ctx)
    cid = _post_notes(client, sid, quote="zeppelin budget").json()["id"]
    for query in ("Why", "zeppelin"):
        hits = client.get("/api/search", params={"q": query, "mode": "keyword"}).json()
        (hit,) = [h for h in hits if h["session_id"] == sid]
        (c,) = hit["comments"]
        assert (c["id"], c["target"], c["label"]) == (cid, "notes", "Notes")
        assert c["quote"] == "zeppelin budget"
    assert "[[zeppelin]]" in c["snippet"]


@pytest.mark.anyio
async def test_mcp_lists_a_notes_comment_with_its_quote(client, ctx, sidecar):
    from mnemosyne.mcp_server import _open_comments

    sid = _meeting(ctx)
    _post_notes(client, sid, quote="launch plan")

    class Backend:
        async def get(self, path):
            return client.get(path).json()

    (line,) = await _open_comments(Backend(), sid)
    assert line.endswith("(on Notes: “launch plan”)")


# ---- the server puts the mark (readers cannot author it) --------------------------------------


def test_the_server_marks_the_range_and_everyone_connected_gets_the_frame(client, ctx, sidecar):
    sid = _meeting(ctx)
    with client.websocket_connect(f"/ws/notes/{sid}") as ws:
        ws.receive_json()
        ws.send_bytes(frame("f1", ["ins", 0, "Our launch plan is set\n"]))
        settle(ws)
        # The toy "element ids" are positions: "launch plan" is characters 4..14.
        r = _post_notes(client, sid, notes_ids=["4", "14"])
        assert r.status_code == 200, r.text
        c = r.json()
        # Attached on the way back: the fold stored the range.
        assert c["notes_range"] == [4, 15] and not c["detached"] and not c["changed"]
        mark = ws.receive_bytes()
        assert mark == frame(f"mark-{c['id']}", ["comment", c["id"], 4, 15])
    assert not ctx.repo.has_unfolded_notes(sid)
    assert ctx.repo.notes_marks(sid)[1] == {c["id"]: [4, 15]}
    (request,) = [x for x in sidecar.doc_requests if x["op"] == "mark_comment"]
    assert (request["first"], request["last"], request["comment"]) == ("4", "14", c["id"])
    assert request["notes"] == shared_notes.notes_origin(sid)
    assert _root(client, sid, c["id"])["notes_range"] == [4, 15]
    # A reply carries no ids and makes no mark.
    reply = client.post(_url(sid), json={"text": "Because", "parent_id": c["id"]})
    assert reply.status_code == 200
    assert len([x for x in sidecar.doc_requests if x["op"] == "mark_comment"]) == 1


def test_bad_or_missing_ids_leave_the_thread_detached(client, ctx, sidecar, monkeypatch):
    sid = _meeting(ctx)
    _fold(ctx, sid, frame("f1", ["ins", 0, "Our launch plan is set\n"]))
    before = len(ctx.repo.notes_ops(sid))
    for ids in (["500", "501"], ["14", "4"], ["x", "y"], ["4"], ["", ""], []):
        r = _post_notes(client, sid, notes_ids=ids)
        assert r.status_code == 200, (ids, r.text)
        c = r.json()
        assert c["detached"] and c["notes_range"] is None and c["quote"] == "launch plan"
        assert _root(client, sid, c["id"])["detached"]
    assert len(ctx.repo.notes_ops(sid)) == before, "nothing was logged"
    # A sidecar that fails mid-way does not fail the comment either.
    monkeypatch.setenv("FAKE_NOTES_DOC_FAIL", "1")
    r = _post_notes(client, sid, notes_ids=["4", "14"])
    assert r.status_code == 200 and r.json()["detached"]


def test_a_reader_can_comment_on_the_notes(team):  # noqa: F811
    client, ctx, people = team
    ann, rita = people["Ann"], people["Rita"]
    sid = _team_meeting(ctx, ann[0])
    ann_h = {"Authorization": f"Bearer {ann[1]}"}
    rita_h = {"Authorization": f"Bearer {rita[1]}"}
    with client.websocket_connect(f"/ws/notes/{sid}?token={ann[1]}") as a:
        assert a.receive_json()["read_only"] is False
        a.send_bytes(frame("f1", ["ins", 0, "Our launch plan is set\n"]))
        settle(a)
        with client.websocket_connect(f"/ws/notes/{sid}?token={rita[1]}") as r:
            hello = r.receive_json()
            assert hello["read_only"] is True
            r.receive_bytes()  # the backlog
            a.receive_json()  # Rita joined
            res = client.post(
                _url(sid),
                json={
                    "text": "Why?",
                    "target": "notes",
                    "exact": "launch plan",
                    "notes_ids": ["4", "14"],
                },
                headers=rita_h,
            )
            assert res.status_code == 200, res.text
            c = res.json()
            assert c["author"] == "Rita Vance" and c["notes_range"] == [4, 15]
            mark = frame(f"mark-{c['id']}", ["comment", c["id"], 4, 15])
            # Ann (an editor) and Rita herself (read-only) both get the mark like any edit.
            assert a.receive_bytes() == mark
            assert r.receive_bytes() == mark
            # Rita may not write the notes themselves.
            r.send_bytes(frame("f2", ["ins", 0, "x"]))
            with pytest.raises(Exception) as e:
                r.receive_bytes()
            assert getattr(e.value, "code", None) == 4403
    ops = ctx.repo.notes_ops(sid)
    assert [(o["author"], o["author_id"]) for o in ops] == [
        ("Ann Lee", ann[0].id),
        ("Rita Vance", rita[0].id),
    ]
    # The owner sees the thread, attached.
    threads = client.get(_url(sid), headers=ann_h).json()["threads"]
    assert [t["root"]["notes_range"] for t in threads] == [[4, 15]]


# ---- the sync paths store the ranges too ------------------------------------------------------


@pytest.mark.anyio
async def test_a_union_stores_the_merged_documents_ranges(nodes, sidecar):  # noqa: F811
    a, b = nodes
    sid = await shared_doc(a, b, notes="base text\n")
    # B had the document without a mark; A marks "base" and B types: the union has the mark.
    write_doc(a, sid, frame("fa", ["comment", "c1", 0, 4]))
    write_doc(b, sid, frame("fb", ["ins", 10, "from B\n"]))
    st = await pull(b, a)
    assert st.state == "done"
    assert sorted(doc_frames(b, sid)) == ["f0", "fa", "fb"]
    plain, ranges = b.repo.notes_marks(sid)
    assert plain == b.repo.get(sid).notes == "base text\nfrom B\n"
    assert ranges == {"c1": [0, 4]}


@pytest.mark.anyio
async def test_an_adopted_document_stores_its_ranges(nodes, sidecar):  # noqa: F811
    a, b = nodes
    sid = await shared_doc(a, b, notes="base text\n")
    assert b.repo.notes_marks(sid) == ("base text\n", {})
    write_doc(a, sid, frame("fa", ["comment", "c1", 5, 9]))
    await pull(b, a)
    assert b.repo.notes_marks(sid) == ("base text\n", {"c1": [5, 9]})
    # A comment travelling in the meeting is placed by those ranges.
    cid = a.repo.add_comment(
        sid, text="x", target="notes", anchor={"mark": "comment:c1"}, quote="text", id="c1"
    )
    await pull(b, a)
    meeting = comments._Meeting.of(b.ctx, b.repo.get(sid))
    placed = meeting.view(b.repo.get_comment(cid))
    assert placed.notes_range == [5, 9] and not placed.changed
