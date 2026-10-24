"""The signed projection of one share (services/shares.py, models/shared_meeting.py)."""

import base64
from datetime import datetime

import pytest

from mnemosyne.models.session import ActionItem, Chapter, Session, SummaryData
from mnemosyne.models.shared_meeting import SharedMeeting, ShareParts
from mnemosyne.models.transcript import TranscriptSegment
from mnemosyne.services import shares
from mnemosyne.services.person import PersonKeys

ADA = "a" * 64
BOB = "b" * 64
ACCOUNT = "my account number is 123456789012"
SECRET_NOTE = "the account is 987654321098 for notes"
SNAPSHOT = b"\x01notes snapshot bytes"

ALL_OFF = dict.fromkeys(
    (
        "summary",
        "decisions",
        "questions",
        "action_items",
        "chapters",
        "topics",
        "attendees",
        "notes",
        "transcript",
        "hide_identifiers",
    ),
    False,
)


def _meeting(ctx) -> str:
    s = Session(
        name="Release sync",
        created_at=datetime(2026, 10, 20, 10, 0),
        attendees=["Ana Lopez", "Bo Chen"],
        participants=["Ana Lopez"],
        audio_file="/data/audio/secret.wav",
        transcript=[
            TranscriptSegment(text="We ship on Friday.", speaker="Ana Lopez", start=0.0, end=4.0),
            TranscriptSegment(text=f"Sure, {ACCOUNT}.", speaker="Bo Chen", start=4.5, end=8.0),
        ],
        summary=f"We ship on Friday. Bo said {ACCOUNT}, mail bo@example.com.",
        notes=f"Private scratch: {SECRET_NOTE}",
        summary_data=SummaryData(
            title="Release",
            source_hash="abc",
            provider="openai",
            model="gpt",
            followup="Hi Bo, draft",
            topics=["Release"],
            decisions=["Ship on Friday"],
            decision_ids=["dec1"],
            decision_at=[1.0],
            decision_links={"dec1": "obj1"},
            open_questions=[f"Who has {ACCOUNT}?"],
            question_ids=["que1"],
            question_at=[5.0],
            action_items=[
                ActionItem(
                    id="act1",
                    text=f"Send {ACCOUNT}",
                    owner="Bo Chen",
                    done=True,
                    follow=True,
                    task_id="task1",
                    issue_url="https://github.com/o/r/issues/1",
                    at=6.0,
                )
            ],
            chapters=[Chapter(start=0.0, title="Intro")],
        ),
    )
    sid = ctx.repo.save(s).id
    ctx.repo.store_notes_fold(sid, SNAPSHOT, 0, s.notes)
    return sid


def _comments(ctx, sid) -> dict[str, str]:
    """One thread per target (and a reply on the segment one); ids by target."""
    ids = {}
    for target, tid in [
        ("segment", ""),
        ("notes", ""),
        ("body", ""),
        ("decision", "dec1"),
        ("question", "que1"),
        ("item", "act1"),
        ("", ""),
    ]:
        ids[target or "meeting"] = ctx.repo.add_comment(
            sid, target=target, target_id=tid, text=f"on {target or 'meeting'}", author="Ada"
        )
    ids["reply"] = ctx.repo.add_comment(
        sid, parent_id=ids["segment"], text="reply on the segment", author="Bob"
    )
    return ids


def _share(ctx, sid, person=ADA, **parts):
    ctx.repo.put_peer_share(sid, person, parts, by="me")


def _contact(ctx, person, name):
    ctx.repo.put_contact(
        {
            "person": person,
            "name": name,
            "my_name": "nickname",
            "card": {"name": name},
            "secret": "00" * 32,
            "created_at": "2026-10-23T10:00:00+00:00",
            "updated_at": "2026-10-23T10:00:00+00:00",
        }
    )


@pytest.fixture
def mine(ctx):
    ctx.settings.local_speaker_name = "Corey"
    return ctx


def _build(ctx, sid, person=ADA) -> SharedMeeting:
    return shares.build_shared_meeting(ctx, sid, person)


# ---- the parts ------------------------------------------------------------------------------


def test_defaults(mine):
    sid = _meeting(mine)
    _share(mine, sid)
    sm = _build(mine, sid)
    assert sm.parts == ShareParts()
    assert (sm.parts.transcript, sm.parts.hide_identifiers) == (False, False)
    assert sm.id == sid and sm.name == "Release sync"
    assert sm.owner.person == mine.person.public_hex and sm.owner.name == "Corey"
    assert sm.summary.startswith("We ship on Friday")
    d = sm.summary_data
    assert d.title == "Release" and d.topics == ["Release"]
    assert (d.decisions, d.decision_ids, d.decision_at) == (["Ship on Friday"], ["dec1"], [1.0])
    assert d.decision_links == {"dec1": "obj1"}
    assert (d.open_questions, d.question_ids, d.question_at)[1:] == (["que1"], [5.0])
    (item,) = d.action_items
    assert (item.id, item.task_id, item.done, item.follow, item.owner, item.at) == (
        "act1",
        "task1",
        True,
        True,
        "Bo Chen",
        6.0,
    )
    assert [c.title for c in d.chapters] == ["Intro"]
    assert sm.attendees == ["Ana Lopez", "Bo Chen"] and sm.participants == ["Ana Lopez"]
    assert sm.transcript == []
    assert sm.notes.startswith("Private scratch")
    assert base64.b64decode(sm.notes_doc) == SNAPSHOT
    # Things that are not a part never travel.
    assert (d.source_hash, d.provider, d.model, d.followup) == ("", "", "", "")
    assert item.issue_url is None


def test_each_part_off_removes_exactly_its_data(mine):
    sid = _meeting(mine)
    _comments(mine, sid)
    full = dict.fromkeys(ALL_OFF, True) | {"transcript": True, "hide_identifiers": False}
    _share(mine, sid, **full)
    base = _build(mine, sid)
    assert base.transcript and base.summary and base.comments

    def without(part):
        _share(mine, sid, **(full | {part: False}))
        return _build(mine, sid)

    sm = without("summary")
    assert sm.summary == "" and sm.summary_data.title == ""
    assert sm.summary_data.decisions == base.summary_data.decisions
    assert sm.transcript == base.transcript and sm.notes == base.notes

    sm = without("decisions")
    d = sm.summary_data
    assert (d.decisions, d.decision_ids, d.decision_at, d.decision_links) == ([], [], [], {})
    assert d.open_questions and d.action_items and d.chapters and d.topics

    sm = without("questions")
    d = sm.summary_data
    assert (d.open_questions, d.question_ids, d.question_at) == ([], [], [])
    assert d.decisions and d.action_items

    sm = without("action_items")
    assert sm.summary_data.action_items == [] and sm.summary_data.decisions

    sm = without("chapters")
    assert sm.summary_data.chapters == [] and sm.summary_data.topics

    sm = without("topics")
    assert sm.summary_data.topics == [] and sm.summary_data.chapters

    sm = without("attendees")
    assert (sm.attendees, sm.participants) == ([], [])
    assert sm.summary and sm.transcript

    sm = without("transcript")
    assert sm.transcript == [] and sm.notes


def test_everything_off_shares_only_the_title_and_date(mine):
    sid = _meeting(mine)
    _comments(mine, sid)
    _share(mine, sid, **ALL_OFF)
    sm = _build(mine, sid)
    assert sm.name == "Release sync" and sm.created_at == datetime(2026, 10, 20, 10, 0)
    assert (sm.summary, sm.notes, sm.notes_doc, sm.transcript) == ("", "", "", [])
    assert (sm.attendees, sm.participants) == ([], [])
    d = sm.summary_data
    assert not (d.decisions or d.open_questions or d.action_items or d.chapters or d.topics)
    assert [c.target for c in sm.comments] == [""]  # the meeting as a whole
    assert [c.text for c in sm.comments] == ["on meeting"]


def test_transcript_off_leaves_no_segment_comments(mine):
    sid = _meeting(mine)
    ids = _comments(mine, sid)
    _share(mine, sid, transcript=False)
    got = {c.id for c in _build(mine, sid).comments}
    assert ids["segment"] not in got and ids["reply"] not in got
    assert {ids[k] for k in ("notes", "body", "decision", "question", "item", "meeting")} <= got
    _share(mine, sid, transcript=True)
    got = {c.id for c in _build(mine, sid).comments}
    assert {ids["segment"], ids["reply"]} <= got


def test_notes_off_leaves_no_notes_and_no_notes_comments(mine):
    sid = _meeting(mine)
    ids = _comments(mine, sid)
    _share(mine, sid, notes=False)
    sm = _build(mine, sid)
    assert (sm.notes, sm.notes_doc) == ("", "")
    assert ids["notes"] not in {c.id for c in sm.comments}
    assert ids["body"] in {c.id for c in sm.comments}


def test_comments_follow_the_other_parts(mine):
    sid = _meeting(mine)
    ids = _comments(mine, sid)
    for part, key in [
        ("summary", "body"),
        ("decisions", "decision"),
        ("questions", "question"),
        ("action_items", "item"),
    ]:
        _share(mine, sid, **{part: False})
        got = {c.id for c in _build(mine, sid).comments}
        assert ids[key] not in got
        # the transcript is off by default: no segment comment, no reply, and this one
        assert len(got) == len(ids) - 3
    _share(mine, sid)
    assert {c.id for c in _build(mine, sid).comments} >= {
        ids[k] for k in ("body", "decision", "question", "item", "notes", "meeting")
    }


def test_owner_comments_go_out_under_the_owners_identity(mine):
    sid = _meeting(mine)
    contact = "p:" + ADA[:16]
    root = mine.repo.add_comment(sid, target="body", text="mine", author="Me", mentions=["u1"])
    reply = mine.repo.add_comment(sid, parent_id=root, text="my reply", author="")
    theirs = mine.repo.add_comment(
        sid, target="body", text="theirs", author_id=contact, author="Ada", mentions=[contact, "u2"]
    )
    their_reply = mine.repo.add_comment(
        sid, parent_id=theirs, text="r", author_id=contact, author="Ada"
    )
    mine.repo.set_comment_resolved(root, datetime.now(), "Corey")
    mine.repo.set_comment_resolved(theirs, datetime.now(), "Ada")
    _share(mine, sid)
    by_id = {c.id: c for c in _build(mine, sid).comments}
    for cid in (root, reply):
        assert (by_id[cid].author_id, by_id[cid].author) == (mine.person.pid, "Corey")
    for cid in (theirs, their_reply):
        assert (by_id[cid].author_id, by_id[cid].author) == (contact, "Ada")
    assert by_id[root].mentions == [] and by_id[theirs].mentions == [contact]
    assert by_id[root].resolved_by == "Corey" and by_id[theirs].resolved_by == "Ada"
    # The stored rows are untouched.
    assert mine.repo.get_comment(root).author_id == ""


def test_comments_travel_as_stored(mine):
    sid = _meeting(mine)
    ids = _comments(mine, sid)
    _share(mine, sid, transcript=True)
    by_id = {c.id: c for c in _build(mine, sid).comments}
    stored = {c["id"]: c for c in mine.repo.comment_rows(sid)}
    for cid in by_id:
        for field in ("parent_id", "target", "target_id", "text", "created_at"):
            assert getattr(by_id[cid], field) == stored[cid][field]
    assert by_id[ids["reply"]].parent_id == ids["segment"]


# ---- redaction ------------------------------------------------------------------------------


def test_hide_identifiers_redacts_the_text_but_not_notes_or_comments(mine):
    sid = _meeting(mine)
    ids = _comments(mine, sid)
    mine.repo.add_comment(sid, target="body", text=f"also {ACCOUNT}", author="Ada")
    _share(mine, sid, transcript=True, hide_identifiers=True)
    sm = _build(mine, sid)
    assert "123456789012" not in sm.summary and "bo@example.com" not in sm.summary
    assert "[EMAIL]" in sm.summary and "We ship on Friday" in sm.summary
    d = sm.summary_data
    assert "123456789012" not in d.open_questions[0]
    assert "123456789012" not in d.action_items[0].text
    assert d.action_items[0].id == "act1" and d.question_ids == ["que1"]
    assert "123456789012" not in sm.transcript[1].text
    assert sm.transcript[0].text == "We ship on Friday."
    # Notes and comments are as written (C13).
    assert "987654321098" in sm.notes
    assert any("123456789012" in c.text for c in sm.comments)
    assert ids["body"] in {c.id for c in sm.comments}
    # Off: nothing is redacted.
    _share(mine, sid, transcript=True)
    sm = _build(mine, sid)
    assert "123456789012" in sm.summary and "123456789012" in sm.transcript[1].text


# ---- who it is shared with ---------------------------------------------------------------------


def test_shared_with_lists_everyone_with_a_live_share(mine):
    sid = _meeting(mine)
    _contact(mine, ADA, "Ada")
    _contact(mine, BOB, "Bob")
    _share(mine, sid, person=ADA)
    assert _build(mine, sid).shared_with == ["Ada"]
    _share(mine, sid, person=BOB)
    assert sorted(_build(mine, sid).shared_with) == ["Ada", "Bob"]
    mine.repo.revoke_peer_share(sid, BOB)
    assert _build(mine, sid).shared_with == ["Ada"]


def test_a_share_with_an_unknown_contact_still_builds(mine):
    sid = _meeting(mine)
    _share(mine, sid)
    assert _build(mine, sid).shared_with == ["p:" + ADA[:16]]


def test_other_shares_parts_are_not_in_it(mine):
    sid = _meeting(mine)
    _share(mine, sid, person=ADA, transcript=False)
    _share(mine, sid, person=BOB, transcript=True)
    assert _build(mine, sid, ADA).transcript == []
    assert _build(mine, sid, BOB).transcript


# ---- what can never be in it -------------------------------------------------------------------

FORBIDDEN = (
    "audio_file",
    "recording",
    "publication",
    "key",
    "token",
    "embedding",
    "voice",
    "history",
)


def _property_names(schema) -> set[str]:
    names: set[str] = set()

    def walk(node):
        if isinstance(node, dict):
            for k, v in node.items():
                if k == "properties" and isinstance(v, dict):
                    names.update(v)
                walk(v)
        elif isinstance(node, list):
            for v in node:
                walk(v)

    walk(schema)
    return names


def test_the_schema_has_no_field_for_what_never_travels():
    names = _property_names(SharedMeeting.model_json_schema())
    assert {"summary", "notes_doc", "shared_with", "digest", "sig", "text", "task_id"} <= names
    bad = {n for n in names if any(f in n.lower() for f in FORBIDDEN)}
    assert not bad, bad


def test_the_output_holds_none_of_the_private_data(mine):
    sid = _meeting(mine)
    _comments(mine, sid)
    _share(mine, sid, transcript=True)
    dumped = _build(mine, sid).model_dump_json()
    for secret in ("/data/audio", "secret.wav", "gpt", "openai", "Hi Bo, draft", "issues/1"):
        assert secret not in dumped


# ---- signing -------------------------------------------------------------------------------------


def _signed(mine) -> tuple[SharedMeeting, str]:
    sid = _meeting(mine)
    _comments(mine, sid)
    _share(mine, sid, transcript=True)
    return _build(mine, sid), mine.person.public_hex


def test_round_trip_verifies(mine):
    sm, owner = _signed(mine)
    assert len(sm.digest) == 64 and len(sm.sig) == 128
    assert shares.verify_shared_meeting(sm, owner)
    assert shares.verify_shared_meeting(SharedMeeting.model_validate(sm.model_dump()), owner)
    assert shares.verify_shared_meeting(
        SharedMeeting.model_validate_json(sm.model_dump_json()), owner
    )
    assert shares.verify_shared_meeting(sm.model_dump(mode="json"), owner)
    expected = shares.digest_of(sm)
    assert sm.digest == expected
    assert PersonKeys.verify(
        owner, sm.sig, PersonKeys.canonical(sm.model_dump(mode="json", exclude={"sig"}))
    )


def test_tampering_fails_verification(mine):
    sm, owner = _signed(mine)
    base = sm.model_dump(mode="json")

    def tampered(edit) -> dict:
        d = SharedMeeting.model_validate(base).model_dump(mode="json")
        edit(d)
        return d

    cases = {
        "name": lambda d: d.update(name="Other"),
        "summary": lambda d: d.update(summary="x"),
        "item text": lambda d: d["summary_data"]["action_items"][0].update(text="Do nothing"),
        "item done": lambda d: d["summary_data"]["action_items"][0].update(done=False),
        "segment": lambda d: d["transcript"][0].update(text="no"),
        "notes": lambda d: d.update(notes="x"),
        "comment": lambda d: d["comments"][0].update(text="x"),
        "shared_with": lambda d: d["shared_with"].append("Eve"),
        "parts": lambda d: d["parts"].update(transcript=False),
        "owner.person": lambda d: d["owner"].update(person="c" * 64),
        "owner.name": lambda d: d["owner"].update(name="Eve"),
    }
    assert shares.verify_shared_meeting(base, owner)
    for label, edit in cases.items():
        assert not shares.verify_shared_meeting(tampered(edit), owner), label

    # A re-hashed forgery still fails: the signature does not cover the new digest.
    d = tampered(cases["name"])
    forged = SharedMeeting.model_validate(d)
    d["digest"] = shares.digest_of(forged)
    assert not shares.verify_shared_meeting(d, owner)
    # Another signer's key, or a different expected owner.
    assert not shares.verify_shared_meeting(base, ADA)


def test_a_signature_by_another_key_fails(mine, tmp_path):
    sm, owner = _signed(mine)
    thief = PersonKeys(tmp_path / "thief")
    forged = sm.model_copy(update={"sig": ""})
    forged.sig = thief.sign(PersonKeys.canonical(forged.model_dump(mode="json", exclude={"sig"})))
    assert not shares.verify_shared_meeting(forged, owner)
    assert not shares.verify_shared_meeting(forged, thief.public_hex)  # it names another owner


def test_verification_never_raises(mine):
    sm, owner = _signed(mine)
    for junk in (None, {}, [], "x", 5, {"id": 1}, sm.model_dump(mode="json") | {"owner": None}):
        assert shares.verify_shared_meeting(junk, owner) is False
    assert shares.verify_shared_meeting(sm, "not hex") is False


# ---- no share ----------------------------------------------------------------------


def test_no_share_is_not_found(mine):
    sid = _meeting(mine)
    with pytest.raises(shares.ShareNotFound):
        _build(mine, sid)
    _share(mine, sid, person=BOB)
    with pytest.raises(shares.ShareNotFound):
        _build(mine, sid, ADA)
    with pytest.raises(shares.ShareNotFound):
        _build(mine, "nosuchmeeting")


def test_a_revoked_share_is_not_found_until_shared_again(mine):
    sid = _meeting(mine)
    _share(mine, sid)
    _build(mine, sid)
    assert mine.repo.revoke_peer_share(sid, ADA)
    with pytest.raises(shares.ShareNotFound):
        _build(mine, sid)
    _share(mine, sid)
    assert _build(mine, sid).id == sid


def test_stored_parts_are_read_leniently():
    p = ShareParts.from_stored({"summary": False, "quotes": [1, 2], "transcript": "yes", "x": 1})
    assert p.summary is False and p.transcript is False and p.notes is True
    assert ShareParts.from_stored(None) == ShareParts()
    assert ShareParts.from_stored({}) == ShareParts()
