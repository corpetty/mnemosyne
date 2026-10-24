"""The hub takes back what a contact did on a meeting shared with them
(POST /api/share/contributions, services/shares.py `accept_contributions`): signed records,
applied as the contact through the usual rules, idempotent by id."""

import base64
import json
import time
from datetime import datetime

import pytest

from mnemosyne.models.session import ActionItem, Session, SummaryData
from mnemosyne.models.transcript import TranscriptSegment
from mnemosyne.services import shared_notes, shares
from mnemosyne.services.person import PersonKeys
from mnemosyne.services.shares import make_contribution
from tests import fake_link
from tests.fake_link import doc_frame
from tests.test_share_hub import ADMIN, EP_ADA, EP_BOB, Contact, share

ITEM = "a1b2c3d4e5f6"


@pytest.fixture
def hub(ctx, client, tmp_path):
    """As in test_share_hub: an API token, and two contacts with real keys and tokens."""
    ctx.settings.api_token = "s3cret"
    ctx.settings.local_speaker_name = "Corey"
    ctx.ada = Contact(ctx, tmp_path, "Ada", EP_ADA)
    ctx.bob = Contact(ctx, tmp_path, "Bob", EP_BOB)
    return ctx


@pytest.fixture
def sidecar(tmp_path, monkeypatch):
    return fake_link.install(tmp_path, monkeypatch)


@pytest.fixture(autouse=True)
def no_timed_folds(monkeypatch):
    monkeypatch.setattr(shared_notes, "FOLD_DELAY", 1000.0)
    monkeypatch.setattr(shared_notes, "FOLD_EVERY", 1000.0)


def meeting(ctx, notes: str = "") -> str:
    s = Session(
        name="Release sync",
        created_at=datetime(2026, 10, 20, 10, 0),
        summary="We ship on Friday.",
        notes=notes,
        transcript=[TranscriptSegment(text="We ship on Friday.", speaker="Ana", start=0, end=4)],
        summary_data=SummaryData(action_items=[ActionItem(id=ITEM, text="Send the notes")]),
    )
    return ctx.repo.save(s).id


def send(client, who, records, frames=None):
    body = {
        "records": [r.model_dump(mode="json") for r in records],
        "frames": frames or {},
    }
    return client.post("/api/share/contributions", json=body, headers=who.headers)


def results(r) -> list[tuple[bool, str]]:
    assert r.status_code == 200, r.text
    return [(x["ok"], x["reason"]) for x in r.json()["results"]]


def create(who, sid, comment_id="c0ffee000001", **fields):
    payload = {"op": "create", "comment_id": comment_id, "text": "From Ada", **fields}
    return make_contribution(who.keys, "comment", sid, payload)


def op(who, sid, name, comment_id, **extra):
    return make_contribution(
        who.keys, "comment", sid, {"op": name, "comment_id": comment_id, **extra}
    )


def threads(client, sid):
    return client.get(f"/api/sessions/{sid}/comments", headers=ADMIN).json()["threads"]


def history(client, sid):
    return client.get(f"/api/sessions/{sid}/history", headers=ADMIN).json()["events"]


# ---- comments ----------------------------------------------------------------------------


def test_a_signed_comment_lands_as_the_contact(hub, client):
    sid = meeting(hub)
    share(hub, sid, hub.ada)
    rec = create(hub.ada, sid)
    assert results(send(client, hub.ada, [rec])) == [(True, "")]
    [t] = threads(client, sid)
    root = t["root"]
    assert root["id"] == "c0ffee000001" and root["text"] == "From Ada"
    assert (root["author_id"], root["author"]) == ("p:" + hub.ada.person[:16], "Ada")
    lines = [e for e in history(client, sid) if e["kind"] == "commented"]
    assert [e["detail"]["by"] for e in lines] == ["Ada"]
    assert hub.repo.has_signature(rec.id)

    # The same record again: ok, and nothing twice.
    assert results(send(client, hub.ada, [rec])) == [(True, "")]
    assert len(threads(client, sid)) == 1
    assert len([e for e in history(client, sid) if e["kind"] == "commented"]) == 1

    # It travels back to the contact as theirs.
    pulled = client.get("/api/share/changes", headers=hub.ada.headers).json()["sessions"][0]
    assert [(c["id"], c["author"]) for c in pulled["comments"]] == [("c0ffee000001", "Ada")]


def test_tampered_forwarded_and_unshared_records_are_refused(hub, client, tmp_path):
    sid = meeting(hub)
    share(hub, sid, hub.ada)
    tampered = create(hub.ada, sid).model_copy(
        update={"payload": {"op": "create", "comment_id": "c0ffee000001", "text": "Changed"}}
    )
    eve = PersonKeys(tmp_path / "Eve")
    eve_record = make_contribution(
        eve, "comment", sid, {"op": "create", "comment_id": "c0ffee000002", "text": "Eve"}
    )
    bobs = create(hub.bob, sid, comment_id="c0ffee000003")  # Bob's own, valid, sent by Ada
    other = meeting(hub)  # not shared with Ada
    unshared = create(hub.ada, other, comment_id="c0ffee000004")
    got = results(send(client, hub.ada, [tampered, eve_record, bobs, unshared]))
    assert got == [
        (False, "The signature does not verify"),
        (False, "This record is signed by someone else"),
        (False, "This record is signed by someone else"),
        (False, "This meeting is not shared with you"),
    ]
    assert threads(client, sid) == [] and threads(client, other) == []
    assert not hub.repo.has_signature(tampered.id)


def test_a_comment_on_a_part_not_shared_is_refused(hub, client):
    sid = meeting(hub)
    share(hub, sid, hub.ada, summary=False)  # no transcript by default, no summary here
    line = create(hub.ada, sid, target="segment", idx=0)
    body = create(hub.ada, sid, comment_id="c0ffee000002", target="body", exact="ship")
    item = create(hub.ada, sid, comment_id="c0ffee000003", target="item", target_id=ITEM)
    whole = create(hub.ada, sid, comment_id="c0ffee000004")  # the meeting as a whole
    got = results(send(client, hub.ada, [line, body, item, whole]))
    assert got == [
        (False, "This meeting's transcript is not shared with you"),
        (False, "This meeting's summary is not shared with you"),
        (True, ""),
        (True, ""),
    ]
    # A reply follows its thread's part.
    share(hub, sid, hub.ada, summary=False, action_items=False)
    reply = create(hub.ada, sid, comment_id="c0ffee000005", parent_id="c0ffee000003")
    assert results(send(client, hub.ada, [reply])) == [
        (False, "This meeting's action items are not shared with you")
    ]


def test_a_bad_comment_is_refused_and_the_batch_goes_on(hub, client):
    sid = meeting(hub)
    share(hub, sid, hub.ada)
    bad_target = create(hub.ada, sid, target="nope")
    no_text = create(hub.ada, sid, comment_id="c0ffee000002", text=" ")
    bad_id = create(hub.ada, sid, comment_id="not-hex")
    good = create(hub.ada, sid, comment_id="c0ffee000003")
    got = results(send(client, hub.ada, [bad_target, no_text, bad_id, good]))
    assert got == [
        (False, "Unknown target 'nope'"),
        (False, "A comment needs text"),
        (False, "A comment id is 12 hex characters"),
        (True, ""),
    ]


def test_an_owners_comment_cannot_be_changed_by_the_contact(hub, client):
    sid = meeting(hub)
    share(hub, sid, hub.ada)
    r = client.post(f"/api/sessions/{sid}/comments", json={"text": "Mine"}, headers=ADMIN)
    owners = r.json()["id"]
    got = results(
        send(
            client,
            hub.ada,
            [
                op(hub.ada, sid, "edit", owners, text="Ada was here"),
                op(hub.ada, sid, "delete", owners),
                op(hub.ada, sid, "resolve", owners),
                # The owner's id for a new comment of Ada's.
                create(hub.ada, sid, comment_id=owners),
            ],
        )
    )
    assert got == [
        (False, "Only its author can change a comment"),
        (False, "Only its author or the meeting's owner can delete a comment"),
        (False, "Only the meeting's owner or the thread's author can resolve it"),
        (False, "That comment id is taken"),
    ]
    [t] = threads(client, sid)
    assert t["root"]["text"] == "Mine" and t["root"]["resolved_at"] is None


def test_the_contact_edits_resolves_and_deletes_their_own(hub, client):
    sid = meeting(hub)
    share(hub, sid, hub.ada)
    cid = "c0ffee000001"
    assert results(send(client, hub.ada, [create(hub.ada, sid, comment_id=cid)])) == [(True, "")]
    got = results(
        send(
            client,
            hub.ada,
            [
                op(hub.ada, sid, "edit", cid, text="Edited by Ada"),
                op(hub.ada, sid, "resolve", cid),
                op(hub.ada, sid, "reopen", cid),
                op(hub.ada, sid, "resolve", cid),
            ],
        )
    )
    assert got == [(True, "")] * 4
    [t] = threads(client, sid)
    assert t["root"]["text"] == "Edited by Ada" and t["root"]["resolved_by"] == "Ada"
    kinds = [e["kind"] for e in history(client, sid)]
    assert kinds.count("comment_resolved") == 2 and kinds.count("comment_reopened") == 1
    assert results(send(client, hub.ada, [op(hub.ada, sid, "delete", cid)])) == [(True, "")]
    assert threads(client, sid) == []


def test_only_the_thread_author_or_owner_resolves(hub, client):
    sid = meeting(hub)
    share(hub, sid, hub.ada)
    share(hub, sid, hub.bob)
    cid = "c0ffee000001"
    assert results(send(client, hub.bob, [create(hub.bob, sid, comment_id=cid)])) == [(True, "")]
    reply = create(hub.ada, sid, comment_id="c0ffee000002", parent_id=cid, text="Agreed")
    got = results(send(client, hub.ada, [reply, op(hub.ada, sid, "resolve", cid)]))
    assert got == [
        (True, ""),
        (False, "Only the meeting's owner or the thread's author can resolve it"),
    ]
    assert results(send(client, hub.bob, [op(hub.bob, sid, "resolve", cid)])) == [(True, "")]


def test_an_unknown_comment_or_op_is_refused(hub, client):
    sid = meeting(hub)
    share(hub, sid, hub.ada)
    got = results(
        send(
            client,
            hub.ada,
            [op(hub.ada, sid, "edit", "c0ffee00dead", text="x"), op(hub.ada, sid, "pin", "x")],
        )
    )
    assert got == [(False, "Comment not found"), (False, "Unknown comment operation 'pin'")]


# ---- action items ------------------------------------------------------------------------


def flag(who, sid, name="done", value=True, item_id=ITEM):
    return make_contribution(
        who.keys, "item_flag", sid, {"item_id": item_id, "flag": name, "value": value}
    )


def test_ticking_and_starring_an_item(hub, client):
    sid = meeting(hub)
    share(hub, sid, hub.ada)
    got = results(send(client, hub.ada, [flag(hub.ada, sid), flag(hub.ada, sid, "follow")]))
    assert got == [(True, ""), (True, "")]
    item = hub.repo.get(sid).summary_data.action_items[0]
    assert item.done and item.follow
    ticks = [e for e in history(client, sid) if e["kind"] == "task_done"]
    assert [e["detail"]["by"] for e in ticks] == ["Ada"]
    # Reopened.
    assert results(send(client, hub.ada, [flag(hub.ada, sid, value=False)])) == [(True, "")]
    assert not hub.repo.get(sid).summary_data.action_items[0].done
    bad = [flag(hub.ada, sid, item_id="nope"), flag(hub.ada, sid, name="pin")]
    assert results(send(client, hub.ada, bad)) == [
        (False, "No such action item"),
        (False, "Unknown action item flag 'pin'"),
    ]


def test_items_not_shared_cannot_be_ticked(hub, client):
    sid = meeting(hub)
    share(hub, sid, hub.ada, action_items=False)
    assert results(send(client, hub.ada, [flag(hub.ada, sid)])) == [
        (False, "This meeting's action items are not shared with you")
    ]
    assert not hub.repo.get(sid).summary_data.action_items[0].done


# ---- notes frames ------------------------------------------------------------------------


def frames_record(who, sid, *raw: bytes):
    hashes = [shares.frame_hash(f) for f in raw]
    frames = {h: base64.b64encode(f).decode() for h, f in zip(hashes, raw, strict=True)}
    return make_contribution(who.keys, "notes_frames", sid, {"frame_hashes": hashes}), frames


def frame(frame_id: str, *ops) -> bytes:
    return base64.b64decode(doc_frame(frame_id, *ops))


def test_frames_reach_open_editors_and_are_logged_as_the_contact(hub, client, sidecar):
    sid = meeting(hub)
    share(hub, sid, hub.ada)
    raw = frame("f1", ["ins", 0, "Ada's line\n"])
    rec, frames = frames_record(hub.ada, sid, raw)
    with client.websocket_connect(f"/ws/notes/{sid}?token=s3cret") as owner:
        assert owner.receive_json()["type"] == "hello"
        assert results(send(client, hub.ada, [rec], frames)) == [(True, "")]
        assert owner.receive_bytes() == raw
        last = owner.receive_json()
        assert (last["type"], last["by"]) == ("last_edit", "Ada")
    ops = hub.repo.notes_ops(sid)
    assert [(o["frame"], o["author_id"], o["author"]) for o in ops] == [
        (raw, "p:" + hub.ada.person[:16], "Ada")
    ]
    assert hub.repo.has_signature("frame:" + shares.frame_hash(raw))
    # Sent again, as the same or a new record: not logged twice.
    again, _ = frames_record(hub.ada, sid, raw)
    assert results(send(client, hub.ada, [rec, again], frames)) == [(True, ""), (True, "")]
    assert len(hub.repo.notes_ops(sid)) == 1


def test_frames_with_nobody_connected_are_folded(hub, client, sidecar):
    sid = meeting(hub)
    share(hub, sid, hub.ada)
    rec, frames = frames_record(hub.ada, sid, frame("f1", ["ins", 0, "Folded\n"]))
    assert results(send(client, hub.ada, [rec], frames)) == [(True, "")]
    end = time.monotonic() + 5
    while hub.repo.get(sid).notes != "Folded\n" and time.monotonic() < end:
        time.sleep(0.02)
    assert hub.repo.get(sid).notes == "Folded\n"


def test_frames_are_refused_when_notes_are_not_shared(hub, client, sidecar):
    sid = meeting(hub)
    share(hub, sid, hub.ada, notes=False)
    rec, frames = frames_record(hub.ada, sid, frame("f1", ["ins", 0, "x\n"]))
    assert results(send(client, hub.ada, [rec], frames)) == [
        (False, "This meeting's notes are not shared with you")
    ]
    assert hub.repo.notes_ops(sid) == []


def test_frames_that_do_not_match_or_are_missing_are_refused(hub, client, sidecar):
    sid = meeting(hub)
    share(hub, sid, hub.ada)
    raw = frame("f1", ["ins", 0, "x\n"])
    rec, frames = frames_record(hub.ada, sid, raw)
    swapped = {h: base64.b64encode(frame("f2", ["ins", 0, "y\n"])).decode() for h in frames}
    assert results(send(client, hub.ada, [rec], swapped)) == [
        (False, "A notes frame does not match its hash")
    ]
    assert results(send(client, hub.ada, [rec], {})) == [(False, "A notes frame is missing")]
    not_a_frame = b"\x01" + json.dumps({"id": "f3"}).encode()
    bad, bad_frames = frames_record(hub.ada, sid, not_a_frame)
    assert results(send(client, hub.ada, [bad], bad_frames)) == [(False, "Not a notes frame")]
    assert hub.repo.notes_ops(sid) == []
    assert not hub.repo.has_signature(rec.id)
    # Right, it goes.
    assert results(send(client, hub.ada, [rec], frames)) == [(True, "")]


def test_frames_without_a_sidecar_are_refused(hub, client):
    sid = meeting(hub)
    share(hub, sid, hub.ada)
    rec, frames = frames_record(hub.ada, sid, frame("f1", ["ins", 0, "x\n"]))
    assert results(send(client, hub.ada, [rec], frames)) == [
        (False, "Live notes are not available on this computer")
    ]
    assert not hub.repo.has_signature(rec.id)


# ---- the share, the caller, the limits ----------------------------------------------------


def test_a_revoked_share_refuses_everything(hub, client, sidecar):
    sid = meeting(hub)
    share(hub, sid, hub.ada)
    assert hub.repo.revoke_peer_share(sid, hub.ada.person)
    rec, frames = frames_record(hub.ada, sid, frame("f1", ["ins", 0, "x\n"]))
    got = results(send(client, hub.ada, [create(hub.ada, sid), flag(hub.ada, sid), rec], frames))
    assert got == [(False, "This meeting is not shared with you")] * 3
    assert threads(client, sid) == [] and hub.repo.notes_ops(sid) == []
    assert not hub.repo.get(sid).summary_data.action_items[0].done


def test_only_a_contact_may_send(hub, client):
    sid = meeting(hub)
    body = {"records": [], "frames": {}}
    assert client.post("/api/share/contributions", json=body, headers=ADMIN).status_code == 403
    assert client.post("/api/share/contributions", json=body).status_code == 401
    r = client.post("/api/share/contributions", json=body, headers=hub.ada.headers)
    assert r.status_code == 200 and r.json() == {"results": []}
    assert sid


def test_batch_limits(hub, client):
    sid = meeting(hub)
    share(hub, sid, hub.ada)
    many = [create(hub.ada, sid, comment_id=f"c0ffee{i:06x}") for i in range(201)]
    assert send(client, hub.ada, many).status_code == 413
    big = {"0" * 64: base64.b64encode(b"\xde" + b"x" * (2 * 1024 * 1024)).decode()}
    assert send(client, hub.ada, [], big).status_code == 413
    assert threads(client, sid) == []
    assert send(client, hub.ada, many[:200]).status_code == 200


# ---- the notes document a recipient gets -------------------------------------------------


def test_typed_notes_without_a_live_document_are_sent_as_one(hub, client, sidecar):
    sid = meeting(hub, notes="Typed before anyone opened them\n")
    assert not hub.repo.has_notes_doc(sid)
    share(hub, sid, hub.ada)
    [sm] = client.get("/api/share/changes", headers=hub.ada.headers).json()["sessions"]
    assert sm["notes"] == "Typed before anyone opened them\n"
    assert sm["notes_doc"]
    reply = shared_notes._run_doc(
        shared_notes._binary(),
        {
            "op": "fold",
            "notes": shared_notes.notes_origin(sid),
            "snapshot": sm["notes_doc"],
            "frames": [],
        },
    )
    assert reply["markdown"] == sm["notes"]
    # Frames typed on that snapshot come back and merge with it.
    rec, frames = frames_record(hub.ada, sid, frame("f9", ["ins", 0, "Ada: "]))
    assert results(send(client, hub.ada, [rec], frames)) == [(True, "")]
    assert hub.repo.get(sid).notes == "Ada: Typed before anyone opened them\n"
