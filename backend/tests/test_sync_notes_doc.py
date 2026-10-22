"""Library sync of the live notes document (services/sync_notes.py, phase 6 T7): two apps in this
process pulling from each other through `httpx.ASGITransport` (the pattern of test_sync_service),
the shared notes sidecar being the `notes doc` stand-in of tests/fake_link.py (toy frames)."""

import base64
import json
import threading

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from mnemosyne.services import shared_notes
from tests.fake_link import doc_frame
from tests.test_sync_service import Node, meeting, nodes, pair, pull, sidecar, vv  # noqa: F401


def frame(frame_id: str, *ops) -> bytes:
    return base64.b64decode(doc_frame(frame_id, *ops))


def write_doc(node: Node, sid: str, *frames: bytes) -> None:
    """Frames typed on `node`, folded now (so Session.notes and the meeting's version follow)."""
    node.repo.append_notes_ops(sid, list(frames), "", "Me", True)
    shared_notes.fold_blocking(node.ctx, sid)


def doc_frames(node: Node, sid: str) -> list[str]:
    snapshot = node.repo.notes_doc(sid)["snapshot"]
    return [f["id"] for f in json.loads(snapshot)["frames"]]


def recv(ws, kind: str, timeout: float = 5.0):
    """`ws.receive_<kind>()` that fails the test when nothing arrives, instead of blocking."""
    box: dict = {}

    def run():
        try:
            box["value"] = getattr(ws, f"receive_{kind}")()
        except BaseException as e:  # noqa: BLE001 - handed to the test thread
            box["error"] = e

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    thread.join(timeout)
    if thread.is_alive():
        raise AssertionError(f"no {kind} message from the socket within {timeout} s")
    if "error" in box:
        raise box["error"]
    return box["value"]


def merge_requests(stand_in) -> int:
    return sum(1 for r in stand_in.doc_requests if r["op"] == "merge")


async def shared_doc(a: Node, b: Node, notes: str = "base\n") -> str:
    """A meeting recorded on A with a document, copied to B (which adopts it)."""
    sid = meeting(a, notes="")
    write_doc(a, sid, frame("f0", ["set", notes]))
    await pull(b, a)
    assert doc_frames(b, sid) == ["f0"]
    assert b.repo.get(sid).notes == notes
    return sid


# ---- both sides have a document ---------------------------------------------------------------


@pytest.mark.anyio
async def test_concurrent_edits_union_on_both_sides(nodes, sidecar):  # noqa: F811
    a, b = nodes
    sid = await shared_doc(a, b)
    write_doc(a, sid, frame("fa", ["ins", 5, "from A\n"]))
    write_doc(b, sid, frame("fb", ["ins", 5, "from B\n"]))

    st = await pull(b, a)  # concurrent: the documents are merged, not the text
    assert (st.merged, st.state) == (1, "done")
    assert sorted(doc_frames(b, sid)) == ["f0", "fa", "fb"]
    notes = b.repo.get(sid).notes
    assert "from A\n" in notes and "from B\n" in notes
    assert merge_requests(sidecar) == 1
    # The meeting's text is the document's render: nothing left to catch up.
    assert b.repo.notes_drift(sid) is None
    # Frames that came from A are logged as its sync, already folded.
    synced = [o for o in b.repo.notes_ops(sid) if o["author"] == "sync (A)"]
    assert [(o["folded"], o["author_id"]) for o in synced] == [(1, "")]
    assert not b.repo.has_unfolded_notes(sid)

    await pull(a, b)  # B's merge reaches A, which already has what it lacked
    assert sorted(doc_frames(a, sid)) == ["f0", "fa", "fb"]
    assert a.repo.get(sid).notes == notes
    assert a.repo.notes_doc(sid)["snapshot"] == b.repo.notes_doc(sid)["snapshot"]
    # Converged: another round either way changes nothing.
    before = (vv(a, sid), vv(b, sid))
    await pull(a, b)
    await pull(b, a)
    assert (vv(a, sid), vv(b, sid)) == before


@pytest.mark.anyio
async def test_unchanged_documents_are_not_merged(nodes, sidecar):  # noqa: F811
    a, b = nodes
    sid = await shared_doc(a, b)
    a.repo.update_fields(sid, name="A's title")
    b.repo.update_fields(sid, participants=["Zed"])
    before = merge_requests(sidecar)
    st = await pull(b, a)  # concurrent, but the documents are the same
    assert st.merged == 1 and b.repo.get(sid).name == "A's title"
    assert merge_requests(sidecar) == before
    assert doc_frames(b, sid) == ["f0"]
    assert b.repo.get(sid).notes == "base\n"


@pytest.mark.anyio
async def test_a_union_that_fails_holds_the_cursor_and_is_retried(
    nodes,  # noqa: F811
    sidecar,  # noqa: F811
    monkeypatch,
):
    a, b = nodes
    sid = await shared_doc(a, b)
    write_doc(a, sid, frame("fa", ["ins", 5, "from A\n"]))
    write_doc(b, sid, frame("fb", ["ins", 5, "from B\n"]))
    ((_, seq, _),) = [c for c in a.repo.changes_since(0)[0] if c[0] == sid]
    later = meeting(a, name="Later", notes="")  # changed after it, so in a later page
    b.sync.page_size = 1

    monkeypatch.setenv("FAKE_NOTES_DOC_FAIL", "1")  # the sidecar fails the merge
    st = await pull(b, a)
    assert (st.state, st.waiting, st.merged, st.received) == ("done", 1, 0, 1)
    assert b.repo.get(later) is not None  # the rest of the library syncs meanwhile
    assert doc_frames(b, sid) == ["f0", "fb"]  # still diverged
    assert vv(b, sid) != vv(a, sid)
    assert b.peer(a).since == seq - 1  # not past the meeting that had to wait
    async with b.client() as c:
        (peer,) = (await c.get("/api/sync/status")).json()["peers"]
    assert peer["waiting"] == 1

    st = await pull(b, a)  # still failing: still waiting, nothing lost, the rest goes on
    assert (st.waiting, b.peer(a).since) == (1, seq - 1)
    a.repo.update_fields(later, name="Later, renamed")
    await pull(b, a)
    assert b.repo.get(later).name == "Later, renamed"
    assert b.peer(a).since == seq - 1

    monkeypatch.delenv("FAKE_NOTES_DOC_FAIL")
    st = await pull(b, a)
    assert (st.state, st.waiting, st.merged) == ("done", 0, 1)
    assert sorted(doc_frames(b, sid)) == ["f0", "fa", "fb"]
    assert b.peer(a).since >= seq
    await pull(a, b)
    assert a.repo.notes_doc(sid)["snapshot"] == b.repo.notes_doc(sid)["snapshot"]


# ---- only one side has a document -------------------------------------------------------------


@pytest.mark.anyio
async def test_a_document_made_on_a_is_adopted_by_b(nodes, sidecar):  # noqa: F811
    a, b = nodes
    sid = meeting(a, notes="Hello\n")
    await pull(b, a)
    # Recorded elsewhere, text, no document: waiting for it.
    assert shared_notes.needs_doc_from_sync(b.ctx, sid)
    assert shared_notes.seed_blocking(b.ctx, sid) == []

    assert shared_notes.seed_blocking(a.ctx, sid)  # the same text: only the document is new
    assert a.repo.get(sid).notes == "Hello\n"
    st = await pull(b, a)  # and it still travels
    assert st.updated == 1
    assert not shared_notes.needs_doc_from_sync(b.ctx, sid)
    assert a.repo.notes_doc(sid)["snapshot"] == b.repo.notes_doc(sid)["snapshot"]
    assert b.repo.notes_drift(sid) is None
    assert vv(b, sid) == vv(a, sid)


@pytest.mark.anyio
async def test_text_edits_here_and_a_document_there_end_in_both_documents(
    nodes,  # noqa: F811
    sidecar,  # noqa: F811
):
    a, b = nodes
    sid = meeting(a, notes="Hello\n")
    await pull(b, a)
    shared_notes.seed_blocking(a.ctx, sid)
    write_doc(a, sid, frame("fa", ["ins", 6, "A edit\n"]))
    b.repo.update_fields(sid, notes="Hello\nB edit\n")  # text only: B has no document

    await pull(b, a)  # adopt theirs, merge the text, the document catches up
    merged = b.repo.get(sid).notes
    assert "A edit\n" in merged and "B edit\n" in merged
    assert b.repo.has_notes_doc(sid)
    assert b.repo.notes_drift(sid) == merged  # waiting to be written into the adopted document
    await pull(a, b)  # A asks B: B folds first, which catches the document up
    assert b.repo.notes_drift(sid) is None
    for node in (a, b):
        assert "A edit\n" in node.repo.notes_doc(sid)["markdown"]
        assert "B edit\n" in node.repo.notes_doc(sid)["markdown"]
        assert node.repo.get(sid).notes == merged
    assert a.repo.notes_doc(sid)["snapshot"] == b.repo.notes_doc(sid)["snapshot"]


@pytest.mark.anyio
async def test_a_meeting_with_only_our_document_merges_its_text(nodes, sidecar):  # noqa: F811
    a, b = nodes
    sid = await shared_doc(a, b)
    # B's copy loses its document (as if it never had one there).
    with b.repo._lock, b.repo._conn:
        b.repo._conn.execute("DELETE FROM shared_notes WHERE session_id=?", (sid,))
        b.repo._conn.execute("DELETE FROM shared_notes_ops WHERE session_id=?", (sid,))
    write_doc(a, sid, frame("fa", ["ins", 5, "from A\n"]))
    b.repo.update_fields(sid, notes="base\nfrom B\n")

    await pull(a, b)  # theirs has no document: the text is merged as text
    merged = a.repo.get(sid).notes
    assert "from A\n" in merged and "from B\n" in merged
    assert doc_frames(a, sid) == ["f0", "fa"]
    assert a.repo.notes_drift(sid) == merged
    shared_notes.fold_blocking(a.ctx, sid)  # the catch-up writes B's text into the document
    assert a.repo.notes_drift(sid) is None
    assert "from B\n" in a.repo.notes_doc(sid)["markdown"]
    assert a.repo.get(sid).notes == merged


# ---- formatting-only changes ------------------------------------------------------------------


@pytest.mark.anyio
async def test_a_change_that_leaves_the_markdown_alone_still_travels(nodes, sidecar):  # noqa: F811
    a, b = nodes
    sid = await shared_doc(a, b)
    before = vv(a, sid)
    # A comment mark: the snapshot changes, the markdown does not.
    write_doc(a, sid, frame("c1", ["comment", "thread-1", 0, 4]))
    assert a.repo.get(sid).notes == "base\n"
    assert doc_frames(a, sid) == ["f0", "c1"]
    (me,) = before
    assert vv(a, sid) == {me: before[me] + 1}  # one new version, and not for the text
    # A fold that changes nothing is not a new version.
    shared_notes.fold_blocking(a.ctx, sid)
    assert vv(a, sid) == {me: before[me] + 1}

    st = await pull(b, a)
    assert st.updated == 1
    assert doc_frames(b, sid) == ["f0", "c1"]
    assert b.repo.get(sid).notes == "base\n"
    assert vv(b, sid) == vv(a, sid)


# ---- connected editors ------------------------------------------------------------------------


def test_what_a_merge_brings_reaches_connected_editors(tmp_path, monkeypatch, sidecar):  # noqa: F811
    import asyncio

    monkeypatch.setenv("MNEMOSYNE_CONFIG_FILE", str(tmp_path / "config.toml"))
    a, b = Node(tmp_path, "A"), Node(tmp_path, "B")
    pair(a, b)
    for n in (a, b):
        n.sync.start()
    try:
        with TestClient(a.app) as client:
            sid = meeting(a, notes="")
            write_doc(a, sid, frame("f0", ["set", "base\n"]))
            asyncio.run(pull(b, a))

            with client.websocket_connect(f"/ws/notes/{sid}?token=admin-A") as ws:
                hello = recv(ws, "json")
                assert hello["snapshot"] is True and hello["backlog"] == 0
                assert [f["id"] for f in json.loads(recv(ws, "bytes"))["frames"]] == ["f0"]

                write_doc(b, sid, frame("fb", ["ins", 5, "from B\n"]))
                client.portal.call(pull, a, b)
                got = recv(ws, "bytes")
                assert got == frame("fb", ["ins", 5, "from B\n"])
                assert a.repo.get(sid).notes == "base\nfrom B\n"
                logged = [o for o in a.repo.notes_ops(sid) if o["frame"] == got]
                assert [(o["author"], o["author_id"], o["folded"]) for o in logged] == [
                    ("sync (B)", "", 1)
                ]
                # Typing here goes on from there, and a late joiner sees one consistent state.
                ws.send_bytes(frame("fa", ["ins", 0, "# "]))
                ws.send_text(json.dumps({"type": "ping"}))
                assert recv(ws, "json") == {"type": "pong"}
                with client.websocket_connect(f"/ws/notes/{sid}?token=admin-A") as late:
                    hello = recv(late, "json")
                    assert hello["snapshot"] is True and hello["backlog"] == 1
                    assert sorted(f["id"] for f in json.loads(recv(late, "bytes"))["frames"]) == [
                        "f0",
                        "fb",
                    ]
                    assert recv(late, "bytes") == frame("fa", ["ins", 0, "# "])
    finally:
        for n in (a, b):
            n.repo.close()


def test_a_document_that_arrives_where_editors_wait_tells_them_to_reload(
    tmp_path,
    monkeypatch,
    sidecar,  # noqa: F811
):
    import asyncio

    monkeypatch.setenv("MNEMOSYNE_CONFIG_FILE", str(tmp_path / "config.toml"))
    a, b = Node(tmp_path, "A"), Node(tmp_path, "B")
    pair(a, b)
    for n in (a, b):
        n.sync.start()
    try:
        sid = meeting(a, notes="Hello\n")
        asyncio.run(pull(b, a))  # B has the text, not the document
        with TestClient(b.app) as client:
            with client.websocket_connect(f"/ws/notes/{sid}?token=admin-B") as ws:
                hello = recv(ws, "json")
                assert hello["waiting_for_sync"] is True and hello["read_only"] is True
                assert hello["snapshot"] is False
                shared_notes.seed_blocking(a.ctx, sid)
                client.portal.call(pull, b, a)
                assert recv(ws, "json") == {"type": "reload"}
                with pytest.raises(WebSocketDisconnect) as closed:  # closed normally
                    recv(ws, "json")
                assert closed.value.code == 1000
            with client.websocket_connect(f"/ws/notes/{sid}?token=admin-B") as again:
                hello = recv(again, "json")
                assert hello["snapshot"] is True and hello["read_only"] is False
                assert "waiting_for_sync" not in hello
    finally:
        for n in (a, b):
            n.repo.close()


# ---- deletes ----------------------------------------------------------------------------------


@pytest.mark.anyio
async def test_deleting_a_meeting_with_a_document_leaves_a_tombstone(nodes, sidecar):  # noqa: F811
    a, b = nodes
    sid = await shared_doc(a, b)
    async with a.client() as c:
        assert (await c.delete(f"/api/sessions/{sid}")).status_code == 200
    assert a.repo.has_tombstone(sid) and not a.repo.has_notes_doc(sid)
    st = await pull(b, a)
    assert st.deleted == 1
    assert b.repo.get(sid) is None and b.repo.has_tombstone(sid)
    assert not b.repo.has_notes_doc(sid) and b.repo.notes_doc(sid) is None
    assert (await pull(b, a)).deleted == 0
