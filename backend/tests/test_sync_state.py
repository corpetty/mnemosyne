"""Library sync: a meeting as one portable document, and writing one into another library."""

from datetime import datetime, timedelta

import pytest

from mnemosyne.models.session import (
    ActionItem,
    AgendaItem,
    Asset,
    ExternalNotes,
    Recording,
    Session,
    SessionStatus,
    SummaryData,
    TranscriptSegment,
)
from mnemosyne.models.sync_session import SyncAsset, SyncEvent, SyncSession, event_travels
from mnemosyne.models.transcript import WordSegment
from mnemosyne.services.sync_state import export_session, same_meeting, write_session
from mnemosyne.storage.sqlite import SessionRepository


@pytest.fixture
def repo(tmp_path):
    r = SessionRepository(tmp_path / "a" / "db.sqlite")
    r.device_id = "dev-a"
    yield r
    r.close()


@pytest.fixture
def other(tmp_path):
    r = SessionRepository(tmp_path / "b" / "db.sqlite")
    r.device_id = "dev-b"
    yield r
    r.close()


def _meeting(repo) -> str:
    """A meeting recorded on `repo` with every kind of thing that travels (and some that do
    not)."""
    s = repo.save(
        Session(
            name="Planning",
            status=SessionStatus.COMPLETED,
            audio_file="/a/mixed.ogg",
            summary="We planned.",
            summary_data=SummaryData(
                topics=["plan"], action_items=[ActionItem(id="a1", text="ship it")]
            ),
            notes="my notes",
            participants=["Ann", "Bo"],
            attendees=["Ann", "Bo", "Cy"],
            local_only=True,
            speakers_reviewed=True,
            agenda=[AgendaItem(text="Scope", covered=True)],
            meeting_type="standup",
            transcript=[
                TranscriptSegment(
                    text="hello there",
                    speaker="Ann",
                    start=0.0,
                    end=1.5,
                    words=[
                        WordSegment(word="hello", start=0.0, end=0.5, score=0.9),
                        WordSegment(word="there", start=0.6, end=1.5, score=0.8),
                    ],
                ),
                TranscriptSegment(text="hi", speaker="Bo", start=1.5, end=2.0),
            ],
        )
    )
    repo.add_recordings(
        s.id,
        [
            Recording(
                id="r1",
                source="mic",
                device_id=3,
                device_name="Mic",
                path="/a/mic.ogg",
                part=0,
                offset=0.0,
            ),
            Recording(
                id="r2",
                source="system",
                device_id=4,
                device_name="Out",
                path="/a/sys.ogg",
                part=1,
                offset=60.0,
            ),
        ],
    )
    repo.add_bookmark(s.id, 1, 12.5, "decision")
    repo.add_external_notes(s.id, ExternalNotes(source="Gemini", text="notes", filename="g.txt"))
    repo.add_live_flag(s.id, "mic", 3.0, 4.0, "yeah", part=1)
    root = repo.add_comment(
        s.id,
        target="segment",
        anchor={"part": 0, "start": 0.0, "end": 1.5},
        quote="hello there",
        text="Is this right?",
        author_id="u1",
        author="Ann",
        mentions=["u2"],
    )
    repo.add_comment(s.id, parent_id=root, text="Yes", author="Bo")
    repo.set_comment_resolved(root, datetime(2026, 10, 20, 12, 0, 0), "u2")
    repo.set_session_embeddings(s.id, {"Ann": [0.1, 0.2], "Bo": [0.3, 0.4]})
    link = repo.add_asset(Asset(id="lnk1", kind="link", title="Doc", url="https://x.test/doc"))
    repo.attach_asset(s.id, link.id)
    f = repo.add_asset(
        Asset(id="fil1", kind="file", title="Spec", filename="spec.md", size=12),
        path="spec-fil1.md",
        text="the spec text",
    )
    repo.attach_asset(s.id, f.id)
    repo.add_event(s.id, "part_saved", 0, {"seconds": 61})
    repo.add_event(s.id, "transcribed", None, {"engine": "x"})
    for kind in ("viewed", "played", "exported", "recover_failed", "recover_empty"):
        repo.add_event(s.id, kind)
    for kind in ("interrupted", "capture_problem", "capture_ok", "hubspot_failed", "hubspot_ok"):
        repo.add_event(s.id, kind)
    return s.id


def _comparable(doc):
    """What must be equal after a round trip."""
    d = doc.model_dump(mode="json")
    for key in ("origin", "origin_audio", "vv", "recordings"):
        d.pop(key)
    return d


def _count(repo, table, sid):
    return repo._conn.execute(
        f"SELECT count(*) FROM {table} WHERE session_id=?", (sid,)
    ).fetchone()[0]


# ---- export -----------------------------------------------------------------


def test_export_has_what_travels(repo):
    sid = _meeting(repo)
    doc = export_session(repo, sid, "dev-a")
    assert doc.origin == "dev-a"  # '' here is dev-a to the receiver
    assert doc.origin_audio == "/a/mixed.ogg"
    assert doc.name == "Planning" and doc.notes == "my notes"
    assert doc.summary_data.action_items[0].text == "ship it"
    assert doc.participants == ["Ann", "Bo"] and doc.attendees == ["Ann", "Bo", "Cy"]
    assert doc.local_only and doc.speakers_reviewed
    assert doc.agenda[0].covered and doc.meeting_type == "standup"
    assert [s.text for s in doc.transcript] == ["hello there", "hi"]
    assert doc.transcript[0].words[1].word == "there"
    assert sorted((r.id, r.part, r.offset, r.path) for r in doc.recordings) == [
        ("r1", 0, 0.0, "/a/mic.ogg"),
        ("r2", 1, 60.0, "/a/sys.ogg"),
    ]
    assert [(b.part, b.seconds, b.note) for b in doc.bookmarks] == [(1, 12.5, "decision")]
    assert doc.external_notes[0].source == "Gemini"
    assert [(f.part, f.text) for f in doc.live_flags] == [(1, "yeah")]
    assert doc.speaker_embeddings == {"Ann": [0.1, 0.2], "Bo": [0.3, 0.4]}
    root, reply = doc.comments
    assert (root.text, root.target, root.author_id, root.mentions) == (
        "Is this right?",
        "segment",
        "u1",
        ["u2"],
    )
    assert root.anchor == {"part": 0, "start": 0.0, "end": 1.5} and root.quote == "hello there"
    assert root.resolved_at == "2026-10-20T12:00:00" and root.resolved_by == "u2"
    assert (reply.parent_id, reply.text, reply.deleted, reply.resolved_at) == (
        root.id,
        "Yes",
        False,
        None,
    )
    assets = {a.id: a for a in doc.assets}
    assert assets["lnk1"].url == "https://x.test/doc"
    assert assets["fil1"].text == "the spec text" and assets["fil1"].filename == "spec.md"
    assert doc.vv == {"dev-a": doc.vv["dev-a"]} and doc.vv["dev-a"] > 0


def test_export_leaves_out_what_stays(repo):
    sid = _meeting(repo)
    repo.set_attendee_emails(sid, {"Ann": "ann@example.com"})
    repo.set_legal_hold(sid, "case 1")
    dumped = export_session(repo, sid, "dev-a").model_dump_json()
    for secret in ("ann@example.com", "case 1", "spec-fil1.md"):
        assert secret not in dumped
    fields = set(export_session(repo, sid, "dev-a").model_dump())
    assert not fields & {"status", "audio_file", "legal_hold", "crm", "versions", "seals"}


def test_never_sent_event_kinds_are_excluded(repo):
    sid = _meeting(repo)
    kinds = {e.kind for e in export_session(repo, sid, "dev-a").events}
    assert kinds == {"part_saved", "transcribed"}
    for kind in ("viewed", "played", "exported", "recover_x", "interrupted", "capture_ok"):
        assert not event_travels(kind)
    assert event_travels("summary_edited") and event_travels("part_saved")


def test_export_of_unknown_meeting_is_none(repo):
    assert export_session(repo, "nope", "dev-a") is None


def test_export_of_a_synced_meeting_keeps_its_origin(repo, other):
    sid = _meeting(repo)
    write_session(
        other, export_session(repo, sid, "dev-a"), local_device_id="dev-b", vv={}, bump=False
    )
    assert export_session(other, sid, "dev-b").origin == "dev-a"


# ---- write ------------------------------------------------------------------


def test_round_trip_into_a_second_library(repo, other):
    sid = _meeting(repo)
    doc = export_session(repo, sid, "dev-a")
    assert write_session(other, doc, local_device_id="dev-b", vv=doc.vv, bump=False) == sid
    again = export_session(other, sid, "dev-b")
    assert _comparable(again) == _comparable(doc)
    assert again.origin == "dev-a" and again.vv == doc.vv
    assert again.updated_at == doc.updated_at and again.created_at == doc.created_at
    # Recordings keep the origin's rows; its mixed file is not ours.
    assert [r.path for r in again.recordings] == [r.path for r in doc.recordings]
    assert again.origin_audio is None


def test_comments_round_trip_and_a_write_replaces_them(repo, other):
    sid = _meeting(repo)
    doc = export_session(repo, sid, "dev-a")
    write_session(other, doc, local_device_id="dev-b", vv=doc.vv, bump=False)
    assert other.comment_rows(sid) == repo.comment_rows(sid)
    assert _count(other, "comments", sid) == 2
    doc.comments = doc.comments[:1]  # the peer removed the reply
    write_session(other, doc, local_device_id="dev-b", vv=doc.vv, bump=False)
    assert [c["text"] for c in other.comment_rows(sid)] == ["Is this right?"]


def test_a_document_without_comments_parses():
    doc = SyncSession.model_validate(
        {
            "id": "abc123abc123",
            "created_at": "2026-10-14T09:00:00",
            "updated_at": "2026-10-14T09:00:00",
            "origin": "dev-a",
        }
    )
    assert doc.comments == []


def test_non_origin_write_has_no_audio_and_is_completed(repo, other):
    sid = _meeting(repo)
    doc = export_session(repo, sid, "dev-a")
    write_session(other, doc, local_device_id="dev-b", vv=doc.vv, bump=False)
    got = other.get(sid)
    assert got.audio_file is None
    assert got.status == SessionStatus.COMPLETED
    assert got.origin == "dev-a"
    assert got.updated_at == doc.updated_at

    # Without a transcript it is merely created.
    empty = doc.model_copy(update={"id": "emptyone", "transcript": []})
    write_session(other, empty, local_device_id="dev-b", vv={"dev-a": 1}, bump=False)
    assert other.get("emptyone").status == SessionStatus.CREATED


def test_second_write_replaces_children_and_leaves_one_row(repo, other):
    sid = _meeting(repo)
    doc = export_session(repo, sid, "dev-a")
    write_session(other, doc, local_device_id="dev-b", vv=doc.vv, bump=False)
    newer = doc.model_copy(
        update={
            "name": "Renamed",
            "transcript": doc.transcript[:1],
            "bookmarks": [],
            "live_flags": [],
            "external_notes": [],
            "speaker_embeddings": {"Ann": [9.0]},
            "assets": doc.assets[:1],
        }
    )
    write_session(other, newer, local_device_id="dev-b", vv=doc.vv, bump=False)
    got = other.get(sid)
    assert got.name == "Renamed" and len(got.transcript) == 1
    assert other.get_session_embeddings(sid) == {"Ann": [9.0]}
    assert [_count(other, t, sid) for t in ("bookmarks", "live_flags", "external_notes")] == [0] * 3
    assert _count(other, "session_assets", sid) == 1
    assert other._conn.execute("SELECT count(*) FROM sessions").fetchone()[0] == 1


def test_origin_mapping_both_ways(repo, other):
    sid = _meeting(repo)
    doc = export_session(repo, sid, "dev-a")
    write_session(other, doc, local_device_id="dev-b", vv=doc.vv, bump=False)
    assert other.sync_state(sid)[0] == "dev-a"
    # B sends it back to A: the origin is A's own id, so A stores ''.
    back = export_session(other, sid, "dev-b")
    assert back.origin == "dev-a"
    write_session(repo, back, local_device_id="dev-a", vv=back.vv, bump=False)
    assert repo.sync_state(sid)[0] == ""
    assert repo.get(sid).origin == ""


def test_write_back_to_origin_keeps_audio_status_and_recordings(repo, other):
    sid = _meeting(repo)
    repo.update_fields(sid, status=SessionStatus.ERROR)
    doc = export_session(repo, sid, "dev-a")
    write_session(other, doc, local_device_id="dev-b", vv=doc.vv, bump=False)
    back = export_session(other, sid, "dev-b").model_copy(update={"notes": "edited elsewhere"})
    # The peer's copy has other paths in its rows: ours must stay (encryption rewrites ours).
    back.recordings[0].path = "/b/never.ogg"
    write_session(repo, back, local_device_id="dev-a", vv=back.vv, bump=False)
    got = repo.get(sid)
    assert got.notes == "edited elsewhere"
    assert got.audio_file == "/a/mixed.ogg"
    assert got.status == SessionStatus.ERROR
    assert sorted(r.path for r in got.recordings) == ["/a/mic.ogg", "/a/sys.ogg"]


def test_events_union_by_uid_without_duplicates(repo, other):
    sid = _meeting(repo)
    doc = export_session(repo, sid, "dev-a")
    write_session(other, doc, local_device_id="dev-b", vv=doc.vv, bump=False)
    write_session(other, doc, local_device_id="dev-b", vv=doc.vv, bump=False)
    uids = [e.uid for e in export_session(other, sid, "dev-b").events]
    assert sorted(uids) == sorted(e.uid for e in doc.events) and len(set(uids)) == len(uids) == 2
    # An event only the receiver has survives, a new one from the sender is added.
    other.add_event(sid, "summary_edited", None, {"by": "b"})
    extra = doc.model_copy(
        update={
            "events": [
                *doc.events,
                SyncEvent(uid="new-uid", at="2026-01-01T00:00:00", kind="imported"),
            ]
        }
    )
    write_session(other, extra, local_device_id="dev-b", vv=doc.vv, bump=False)
    kinds = sorted(e.kind for e in export_session(other, sid, "dev-b").events)
    assert kinds == ["imported", "part_saved", "summary_edited", "transcribed"]


def test_never_sent_events_arriving_are_dropped(repo, other):
    sid = _meeting(repo)
    doc = export_session(repo, sid, "dev-a")
    bad = SyncEvent(uid="bad1", at="2026-01-01T00:00:00", kind="viewed")
    write_session(
        other,
        doc.model_copy(update={"events": [*doc.events, bad]}),
        local_device_id="dev-b",
        vv=doc.vv,
        bump=False,
    )
    assert "viewed" not in {e.kind for e in export_session(other, sid, "dev-b").events}
    assert (
        other._conn.execute("SELECT count(*) FROM session_events WHERE kind='viewed'").fetchone()[0]
        == 0
    )


def test_link_assets_are_deduped_by_url(repo, other):
    sid = _meeting(repo)
    # The receiver already knows the link under another id.
    mine = other.add_asset(Asset(id="mine1", kind="link", title="Mine", url="https://x.test/doc"))
    doc = export_session(repo, sid, "dev-a")
    write_session(other, doc, local_device_id="dev-b", vv=doc.vv, bump=False)
    links = other._conn.execute("SELECT id FROM assets WHERE kind='link'").fetchall()
    assert [r["id"] for r in links] == [mine.id]
    assert {a.id for a in other.get(sid).assets} == {"mine1", "fil1"}
    # Writing again adds nothing.
    write_session(other, doc, local_device_id="dev-b", vv=doc.vv, bump=False)
    assert other._conn.execute("SELECT count(*) FROM assets").fetchone()[0] == 2
    assert _count(other, "session_assets", sid) == 2


def test_file_assets_come_without_bytes_and_ids_do_not_collide(repo, other):
    sid = _meeting(repo)
    other.add_asset(Asset(id="fil1", kind="file", title="Other", filename="o.txt"), path="o.txt")
    doc = export_session(repo, sid, "dev-a")
    write_session(other, doc, local_device_id="dev-b", vv=doc.vv, bump=False)
    # Its own fil1 stays untouched; the incoming file got a new id, no path, but its text.
    assert other.asset_file("fil1") == ("o.txt", None)
    new = [a for a in other.get(sid).assets if a.kind == "file"]
    assert len(new) == 1 and new[0].id != "fil1" and new[0].has_text
    assert other.asset_file(new[0].id) == (None, "the spec text")
    assert isinstance(doc.assets[0], SyncAsset)


def test_bump_false_keeps_vv_and_advances_seq(repo, other):
    sid = _meeting(repo)
    doc = export_session(repo, sid, "dev-a")
    before = other.current_sync_seq()
    vv = {"dev-a": 7, "dev-c": 2}
    write_session(other, doc, local_device_id="dev-b", vv=vv, bump=False)
    assert other.sync_state(sid) == ("dev-a", vv)
    seq = other._conn.execute("SELECT sync_seq FROM sessions WHERE id=?", (sid,)).fetchone()[0]
    assert seq == before + 1 == other.current_sync_seq()
    changes, _, _ = other.changes_since(before)
    assert [c[0] for c in changes] == [sid] and changes[0][2] == vv
    # And again: the counter moves, the vector does not.
    write_session(other, doc, local_device_id="dev-b", vv=vv, bump=False)
    assert other.current_sync_seq() == before + 2
    assert other.sync_state(sid)[1] == vv


def test_bump_true_counts_a_revision_of_ours(repo, other):
    sid = _meeting(repo)
    doc = export_session(repo, sid, "dev-a")
    write_session(other, doc, local_device_id="dev-b", vv={"dev-a": 3}, bump=True)
    assert other.sync_state(sid)[1] == {"dev-a": 3, "dev-b": 1}
    write_session(other, doc, local_device_id="dev-b", vv={"dev-a": 3, "dev-b": 1}, bump=True)
    assert other.sync_state(sid)[1] == {"dev-a": 3, "dev-b": 2}
    with pytest.raises(ValueError):
        write_session(other, doc, local_device_id="dev-z", vv={}, bump=True)


def test_write_fires_no_new_session_callbacks(repo, other):
    sid = _meeting(repo)
    seen = []
    other.on_new_session = lambda *a: seen.append(a)
    doc = export_session(repo, sid, "dev-a")
    write_session(other, doc, local_device_id="dev-b", vv=doc.vv, bump=False)
    write_session(other, doc, local_device_id="dev-b", vv=doc.vv, bump=True)
    assert seen == []


def test_write_ignores_access_checks(repo, other):
    sid = _meeting(repo)
    other.save(Session(id=sid, name="Theirs", owner_id="someone"))
    doc = export_session(repo, sid, "dev-a")
    assert doc.owner_id == ""
    # No Forbidden/Hidden whatever the caller's role: the apply path is not a request.
    write_session(other, doc, local_device_id="dev-b", vv=doc.vv, bump=False)
    assert other.get(sid).name == "Planning"


def test_search_follows_a_written_meeting(repo, other):
    sid = _meeting(repo)
    doc = export_session(repo, sid, "dev-a")
    write_session(other, doc, local_device_id="dev-b", vv=doc.vv, bump=False)
    assert [h.session_id for h in other.search("planning")] == [sid]
    write_session(
        other,
        doc.model_copy(update={"name": "Budget review", "transcript": doc.transcript[:1]}),
        local_device_id="dev-b",
        vv=doc.vv,
        bump=False,
    )
    assert other.search("planning") == []
    assert [h.session_id for h in other.search("budget")] == [sid]
    assert [h.session_id for h in other.search("hello")] == [sid]


# ---- clash helper -----------------------------------------------------------


def test_same_meeting_within_two_seconds():
    t = datetime(2026, 10, 14, 9, 0, 0)
    assert same_meeting(t, t)
    assert same_meeting(t, t + timedelta(seconds=2))
    assert same_meeting(t + timedelta(seconds=-2), t)
    assert not same_meeting(t, t + timedelta(seconds=2.5))
    assert not same_meeting(t, t - timedelta(hours=1))
