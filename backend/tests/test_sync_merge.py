"""Library sync: the three-way merge of a meeting edited on two machines
(services/sync_merge.py). The text merges run through the sidecar: the stand-in of
tests/fake_link.py here, the real mnemosyne-link in a few tests at the end."""

from datetime import date, datetime, timedelta

import pytest

from mnemosyne.export.note_merge import MergeError, merge_many
from mnemosyne.models.live_flag import LiveFlag
from mnemosyne.models.session import (
    ActionItem,
    AgendaItem,
    Chapter,
    ExternalNotes,
    Recording,
    Session,
    SessionStatus,
    SummaryData,
    TranscriptSegment,
)
from mnemosyne.models.sync_session import (
    SyncAsset,
    SyncBookmark,
    SyncComment,
    SyncEvent,
    SyncSession,
)
from mnemosyne.services import sync_merge
from mnemosyne.services.link import find_binary
from mnemosyne.services.summary_doc import ensure_doc
from mnemosyne.services.sync_merge import apply, merge, vv_max, vv_relation
from mnemosyne.services.sync_state import export_session, write_session
from mnemosyne.storage.sqlite import SessionRepository
from tests import fake_link

T0 = datetime(2026, 10, 14, 9, 0, 0)
BUDGET = "Send the budget report to finance"
DECK = "Prepare the slide deck for the board meeting"
VENDOR = "Call the vendor about the delayed shipment"


@pytest.fixture
def sidecar(tmp_path, monkeypatch):
    return fake_link.install(tmp_path, monkeypatch)


@pytest.fixture
def binary(sidecar) -> str:
    return str(sidecar.path)


def _data(**kw) -> SummaryData:
    fields = dict(
        title="Plan",
        style="meeting",
        topics=["Budget", "Hiring"],
        action_items=[
            ActionItem(id="a1", text=BUDGET, owner="Ana"),
            ActionItem(id="a2", text=DECK),
        ],
        decisions=["We ship on Friday"],
        decision_ids=["d1"],
        decision_at=[1.0],
        open_questions=["When is the launch?"],
        question_ids=["q1"],
        question_at=[2.0],
        chapters=[Chapter(start=0, title="Start")],
    )
    fields.update(kw)
    return SummaryData(**fields)


def _doc(**kw) -> SyncSession:
    fields = dict(
        id="abc123abc123",
        created_at=T0,
        updated_at=T0,
        name="Planning",
        summary="Intro.\n\nPlan.\n",
        summary_data=_data(),
        notes="Intro.\nPlan.\n",
        participants=["Ann", "Bo"],
        attendees=["Ann", "Bo"],
        agenda=[AgendaItem(text="Scope"), AgendaItem(text="Budget")],
        transcript=[TranscriptSegment(text="hello there", speaker="Ann", start=0.0, end=1.0)],
        recordings=[
            Recording(id="r1", source="mic", device_id=3, device_name="Mic", path="/a/mic.ogg")
        ],
        origin_audio="/a/mixed.ogg",
        bookmarks=[SyncBookmark(id="b1", part=0, seconds=5.0, note="Budget talk", created_at=T0)],
        external_notes=[ExternalNotes(id="n1", source="Gemini", text="notes", added_at=T0)],
        live_flags=[LiveFlag(id="f1", source="mic", start=1.0, end=2.0, text="um", created_at=T0)],
        speaker_embeddings={"Ann": [0.1, 0.2]},
        assets=[
            SyncAsset(
                id="l1",
                kind="link",
                title="Doc",
                url="https://x.test/d",
                created_at=T0,
                added_at=T0,
            )
        ],
        events=[SyncEvent(uid="e1", at=T0.isoformat(), kind="transcribed")],
        origin="dev-a",
        vv={"dev-a": 1},
    )
    fields.update(kw)
    return SyncSession(**fields)


def _ours(base: SyncSession, change=None, minutes=1, **kw) -> SyncSession:
    """Our version: a copy of `base` changed by `change` (and `kw`), one revision of ours on."""
    out = base.model_copy(deep=True, update=kw)
    out.updated_at = base.updated_at + timedelta(minutes=minutes)
    out.vv = {**base.vv, "dev-a": base.vv.get("dev-a", 0) + 1}
    if change is not None:
        change(out)
    return out


def _theirs(base: SyncSession, change=None, minutes=2, **kw) -> SyncSession:
    out = base.model_copy(deep=True, update=kw)
    out.updated_at = base.updated_at + timedelta(minutes=minutes)
    out.vv = {**base.vv, "dev-b": base.vv.get("dev-b", 0) + 1}
    if change is not None:
        change(out)
    return out


def _items(doc: SyncSession) -> list[str]:
    return [a.text for a in doc.summary_data.action_items]


def _item(doc: SyncSession, item_id: str) -> ActionItem | None:
    return next((a for a in doc.summary_data.action_items if a.id == item_id), None)


def _comment(cid: str, text="Check this", **kw) -> SyncComment:
    fields = dict(
        id=cid,
        target="segment",
        anchor={"part": 0, "start": 1.0, "end": 2.0},
        text=text,
        author_id="u1",
        author="Ann",
        created_at=T0.isoformat(),
        updated_at=T0.isoformat(),
    )
    fields.update(kw)
    return SyncComment(**fields)


def _merge(base, ours, theirs, binary, ours_is_origin=True):
    return merge(base, ours, theirs, ours_is_origin=ours_is_origin, binary=binary)


# ---- version vectors ----------------------------------------------------------------------


def test_vv_relation_and_max():
    assert vv_relation({"a": 1}, {"a": 1}) == "equal"
    assert vv_relation({"a": 2}, {"a": 1}) == "dominates"
    assert vv_relation({"a": 1}, {"a": 1, "b": 1}) == "dominated"
    assert vv_relation({"a": 2}, {"a": 1, "b": 1}) == "concurrent"
    assert vv_relation({}, {}) == "equal"
    assert vv_max({"a": 2, "c": 1}, {"a": 1, "b": 3}) == {"a": 2, "b": 3, "c": 1}


# ---- texts --------------------------------------------------------------------------------


def test_texts_both_changed_merge_in_one_sidecar_run(sidecar, binary):
    base = _doc()

    def mine(d):
        d.summary = "Intro.\n\nMy A line.\n\nPlan.\n"
        d.notes = "Intro.\nA note.\nPlan.\n"
        d.summary_data.action_items[0].text = "Send the final budget report to finance"
        d.summary_data.decisions[0] = "We ship on Friday morning"
        d.bookmarks[0].note = "Budget talk, long"

    def yours(d):
        d.summary = "Intro.\n\nPlan.\n\nB line.\n"
        d.notes = "Intro.\nPlan.\nB note.\n"
        d.summary_data.action_items[0].text = BUDGET + " by Friday"
        d.summary_data.decisions[0] = "We really ship on Friday"
        d.bookmarks[0].note = "The budget talk"

    out, report = _merge(base, _ours(base, mine), _theirs(base, yours), binary)
    assert out.summary == "Intro.\n\nMy A line.\n\nPlan.\n\nB line.\n"
    assert out.notes == "Intro.\nA note.\nPlan.\nB note.\n"
    assert _items(out)[0] == "Send the final budget report to finance by Friday"
    assert out.summary_data.decisions == ["We really ship on Friday morning"]
    assert out.bookmarks[0].note == "The budget talk, long"
    assert len(sidecar.requests) == 1 and len(sidecar.requests[0]) == 5
    assert all(set(r) == {"base", "app", "vault"} for r in sidecar.requests[0])
    assert report["both"] == 5 and len(report["both_texts"]) == 5
    assert out.vv == {"dev-a": 2, "dev-b": 1}
    assert out.updated_at > base.updated_at + timedelta(minutes=2)
    assert out.id == base.id and out.created_at == base.created_at


def test_a_text_changed_on_one_side_is_taken_without_the_sidecar(sidecar, binary):
    base = _doc()
    ours = _ours(base, lambda d: setattr(d, "name", "Renamed"))
    theirs = _theirs(base, lambda d: setattr(d, "notes", "Theirs.\n"))
    out, report = _merge(base, ours, theirs, binary)
    assert out.notes == "Theirs.\n" and out.name == "Renamed"
    assert sidecar.requests == []
    assert report["both"] == 0 and report["kept"] == 1 and report["both_texts"] == []
    # And the other way round, when theirs kept the base.
    out, report = _merge(base, theirs, ours, binary)
    assert out.notes == "Theirs.\n" and out.name == "Renamed"
    assert sidecar.requests == []


def test_identical_edits_on_both_sides_are_kept_once(sidecar, binary):
    base = _doc()

    def same(d):
        d.notes = "Intro.\nSame line.\nPlan.\n"
        d.summary = "Intro.\n\nSame.\n\nPlan.\n"
        d.summary_data.action_items[0].text = BUDGET + " today"
        d.summary_data.action_items[0].done = True

    out, report = _merge(base, _ours(base, same), _theirs(base, same), binary)
    assert out.notes == "Intro.\nSame line.\nPlan.\n"
    assert out.summary == "Intro.\n\nSame.\n\nPlan.\n"
    assert _items(out)[0] == BUDGET + " today" and out.summary_data.action_items[0].done
    assert sidecar.requests == [] and report["both"] == 0


def test_an_item_added_on_both_sides_with_the_same_wording_is_one(sidecar, binary):
    base = _doc()
    ours = _ours(
        base, lambda d: d.summary_data.action_items.append(ActionItem(id="x1", text=VENDOR))
    )
    theirs = _theirs(
        base,
        lambda d: d.summary_data.action_items.append(
            ActionItem(id="y1", text=VENDOR + ".", owner="Bo")
        ),
    )
    out, report = _merge(base, ours, theirs, binary)
    assert _items(out) == [BUDGET, DECK, VENDOR + "."]  # one block; theirs is newer
    assert [a.id for a in out.summary_data.action_items] == ["a1", "a2", "x1"]
    assert report["fields"] == ["action_items x1: theirs"]
    assert out.summary_data.action_items[2].owner == "Bo"  # set on one side only


# ---- scalars ------------------------------------------------------------------------------


def test_scalars_one_side_wins_both_sides_newer_wins(binary):
    base = _doc()

    def mine(d):
        d.name = "Ours"
        d.meeting_type = "standup"
        d.summary_data.title = "Ours title"
        d.summary_data.action_items[0].owner = "Bo"

    def yours(d):
        d.local_only = True
        d.meeting_type = "review"
        d.summary_data.title = "Theirs title"
        d.summary_data.action_items[0].owner = "Cy"
        d.summary_data.action_items[0].due = date(2026, 10, 20)
        d.summary_data.decision_at[0] = 9.0

    ours, theirs = _ours(base, mine, minutes=5), _theirs(base, yours, minutes=2)
    out, report = _merge(base, ours, theirs, binary)
    assert out.name == "Ours" and out.local_only
    assert out.meeting_type == "standup"  # both changed: ours is newer
    assert out.summary_data.title == "Ours title"
    item = _item(out, "a1")
    assert item.owner == "Bo" and item.due == date(2026, 10, 20)
    assert out.summary_data.decision_at == [9.0]
    assert sorted(report["fields"]) == [
        "action_items a1 owner: ours",
        "meeting_type: ours",
        "summary title: ours",
    ]
    assert report["both"] == 3
    # Theirs newer: the other way.
    out, report = _merge(
        base, _ours(base, mine, minutes=1), _theirs(base, yours, minutes=3), binary
    )
    assert out.meeting_type == "review" and out.summary_data.title == "Theirs title"
    assert _item(out, "a1").owner == "Cy"
    assert "meeting_type: theirs" in report["fields"]


def test_tick_and_untick(binary):
    base = _doc()
    # Ticked there, untouched here: ticked.
    out, report = _merge(
        base, _ours(base), _theirs(base, lambda d: setattr(_item(d, "a1"), "done", True)), binary
    )
    assert _item(out, "a1").done and report["kept"] == 1
    # Ticked earlier and synced; unticked here while they left it: open again.
    ticked = _doc()
    _item(ticked, "a1").done = True
    out, _ = _merge(
        ticked,
        _ours(ticked, lambda d: setattr(_item(d, "a1"), "done", False)),
        _theirs(ticked, lambda d: setattr(_item(d, "a1"), "follow", True)),
        binary,
    )
    assert not _item(out, "a1").done and _item(out, "a1").follow
    # Ticked here while they changed the owner: both stay.
    out, _ = _merge(
        base,
        _ours(base, lambda d: setattr(_item(d, "a1"), "done", True)),
        _theirs(base, lambda d: setattr(_item(d, "a1"), "owner", "Cy")),
        binary,
    )
    assert _item(out, "a1").done and _item(out, "a1").owner == "Cy"


def test_the_task_link_merges_like_a_scalar(binary):
    base = _doc()
    # Linked there, untouched here: linked.
    out, report = _merge(
        base,
        _ours(base),
        _theirs(base, lambda d: setattr(_item(d, "a1"), "task_id", "aaaaaaaaaaaa")),
        binary,
    )
    assert _item(out, "a1").task_id == "aaaaaaaaaaaa" and report["kept"] == 1
    # Linked here, theirs changed something else: the link stays.
    out, _ = _merge(
        base,
        _ours(base, lambda d: setattr(_item(d, "a1"), "task_id", "aaaaaaaaaaaa")),
        _theirs(base, lambda d: setattr(_item(d, "a1"), "owner", "Cy")),
        binary,
    )
    assert _item(out, "a1").task_id == "aaaaaaaaaaaa" and _item(out, "a1").owner == "Cy"

    # Linked to different tasks on both sides: the newer side's.
    def mine(d):
        _item(d, "a1").task_id = "aaaaaaaaaaaa"

    def yours(d):
        _item(d, "a1").task_id = "bbbbbbbbbbbb"

    out, report = _merge(
        base, _ours(base, mine, minutes=5), _theirs(base, yours, minutes=2), binary
    )
    assert _item(out, "a1").task_id == "aaaaaaaaaaaa"
    assert "action_items a1 task_id: ours" in report["fields"]
    out, _ = _merge(base, _ours(base, mine, minutes=1), _theirs(base, yours, minutes=3), binary)
    assert _item(out, "a1").task_id == "bbbbbbbbbbbb"
    # Unlinked there (the link was in the base): unlinked.
    linked = _doc()
    _item(linked, "a1").task_id = "aaaaaaaaaaaa"
    out, _ = _merge(
        linked,
        _ours(linked),
        _theirs(linked, lambda d: setattr(_item(d, "a1"), "task_id", "")),
        binary,
    )
    assert _item(out, "a1").task_id == ""

    # The same item added on both sides, linked on one: the link is taken.
    def add(d, task_id=""):
        d.summary_data.action_items.append(ActionItem(id="n1", text=VENDOR, task_id=task_id))
        d.summary_data.action_items[-1].id = "n1"

    out, _ = _merge(base, _ours(base, add), _theirs(base, lambda d: add(d, "cccccccccccc")), binary)
    assert _item(out, "n1").task_id == "cccccccccccc"


def test_decision_links_merge_per_key_and_are_pruned(binary):
    base = _doc()
    base.summary_data.decisions = ["We ship on Friday", "Skip the beta"]
    base.summary_data.decision_ids = ["d1", "d2"]
    base.summary_data.decision_at = [1.0, 2.0]
    base.summary_data.decision_links = {"d1": "x1"}

    def mine(d):
        d.summary_data.decision_links["d2"] = "x2"

    def yours(d):
        d.summary_data.decision_links["d1"] = "y1"  # relinked there

    out, _ = _merge(base, _ours(base, mine), _theirs(base, yours), binary)
    assert out.summary_data.decision_links == {"d1": "y1", "d2": "x2"}

    # Unlinked there: gone. Linked on one side to a decision the other deleted untouched: gone.
    def unlink(d):
        d.summary_data.decision_links = {}

    out, _ = _merge(base, _ours(base), _theirs(base, unlink), binary)
    assert out.summary_data.decision_links == {}

    def delete(d):
        d.summary_data.decisions = ["We ship on Friday"]
        d.summary_data.decision_ids = ["d1"]
        d.summary_data.decision_at = [1.0]

    def link_d2(d):
        d.summary_data.decision_links["d2"] = "x2"

    out, _ = _merge(base, _ours(base, delete), _theirs(base, link_d2), binary)
    assert out.summary_data.decision_ids == ["d1"]
    assert out.summary_data.decision_links == {"d1": "x1"}  # no key for a gone decision


# ---- items by id --------------------------------------------------------------------------


def test_delete_vs_untouched_is_gone(binary):
    base = _doc()
    ours = _ours(base, lambda d: d.summary_data.action_items.pop(1))
    out, report = _merge(base, ours, _theirs(base), binary)
    assert _items(out) == [BUDGET]
    # Deleted there, untouched here.
    theirs = _theirs(base, lambda d: d.summary_data.action_items.pop(0))
    out, report = _merge(base, _ours(base), theirs, binary)
    assert _items(out) == [DECK] and report["kept"] == 1
    # A decision deleted there.
    theirs = _theirs(
        base,
        lambda d: (
            d.summary_data.decisions.clear(),
            d.summary_data.decision_ids.clear(),
            d.summary_data.decision_at.clear(),
        ),
    )
    out, _ = _merge(base, _ours(base), theirs, binary)
    assert out.summary_data.decisions == [] and out.summary_data.decision_ids == []
    assert out.summary_data.decision_at == []


def test_delete_vs_edited_is_kept(binary):
    base = _doc()
    # Deleted there, reworded here.
    ours = _ours(base, lambda d: setattr(_item(d, "a2"), "text", DECK + " by Monday"))
    theirs = _theirs(base, lambda d: d.summary_data.action_items.pop(1))
    out, _ = _merge(base, ours, theirs, binary)
    assert _items(out) == [BUDGET, DECK + " by Monday"]
    # Deleted here, ticked there (a field edit counts).
    ours = _ours(base, lambda d: d.summary_data.action_items.pop(1))
    theirs = _theirs(base, lambda d: setattr(_item(d, "a2"), "done", True))
    out, _ = _merge(base, ours, theirs, binary)
    assert _items(out) == [BUDGET, DECK] and _item(out, "a2").done
    # Deleted here, the open question reworded there.
    ours = _ours(
        base,
        lambda d: (
            d.summary_data.open_questions.clear(),
            d.summary_data.question_ids.clear(),
            d.summary_data.question_at.clear(),
        ),
    )
    theirs = _theirs(
        base, lambda d: d.summary_data.open_questions.__setitem__(0, "When is the launch, really?")
    )
    out, _ = _merge(base, ours, theirs, binary)
    assert out.summary_data.open_questions == ["When is the launch, really?"]
    assert out.summary_data.question_ids == ["q1"] and out.summary_data.question_at == [2.0]


def test_additions_on_either_side_and_their_order(binary):
    base = _doc()

    def mine(d):
        d.summary_data.action_items.reverse()  # a2, a1
        d.summary_data.action_items.append(ActionItem(id="x1", text="Book the room"))

    def yours(d):
        d.summary_data.action_items.insert(1, ActionItem(id="y1", text=VENDOR))  # a1, y1, a2
        d.summary_data.decisions.append("We hire two people")
        d.summary_data.decision_ids.append("d2")
        d.summary_data.decision_at.append(7.0)

    out, report = _merge(base, _ours(base, mine), _theirs(base, yours), binary)
    assert [a.id for a in out.summary_data.action_items] == ["a2", "a1", "y1", "x1"]
    assert out.summary_data.decisions == ["We ship on Friday", "We hire two people"]
    assert out.summary_data.decision_ids == ["d1", "d2"]
    assert out.summary_data.decision_at == [1.0, 7.0]
    assert report["kept"] == 2


def test_a_summary_made_on_one_side_only_is_taken_whole(binary):
    base = _doc(summary="", summary_data=None)
    theirs = _theirs(base, lambda d: setattr(d, "summary", "Made there.\n"), summary_data=_data())
    out, report = _merge(base, _ours(base, lambda d: setattr(d, "name", "X")), theirs, binary)
    assert out.summary == "Made there.\n" and out.summary_data == _data()
    assert report["kept"] == 1
    # Made on both sides from nothing: items merge by id, the bodies three ways.
    ours = _ours(
        base,
        lambda d: setattr(d, "summary", "Here.\n"),
        summary_data=_data(action_items=[ActionItem(id="h1", text="Here item")]),
    )
    out, _ = _merge(base, ours, theirs, binary)
    assert sorted(_items(out)) == sorted(["Here item", BUDGET, DECK])


# ---- sets, rows by id, lists --------------------------------------------------------------


def test_sets_three_way(binary):
    base = _doc()

    def mine(d):
        d.summary_data.topics = ["Budget", "Mine"]  # dropped Hiring, added Mine
        d.participants = ["Ann", "Bo", "Cy"]
        d.attendees = ["Bo"]

    def yours(d):
        d.summary_data.topics = ["budget", "Hiring", "Launch"]
        d.participants = ["Bo"]
        d.attendees = ["Ann", "Bo", "Dee"]

    out, _ = _merge(base, _ours(base, mine), _theirs(base, yours), binary)
    assert out.summary_data.topics == ["Budget", "Mine", "Launch"]
    assert out.participants == ["Bo", "Cy"]
    assert out.attendees == ["Bo", "Dee"]


def test_rows_by_id(binary):
    base = _doc()

    def mine(d):
        d.bookmarks.append(SyncBookmark(id="b2", seconds=9.0, note="Mine", created_at=T0))
        d.external_notes = []  # removed here
        d.live_flags[0].text = "um, changed here"
        d.speaker_embeddings["Bo"] = [0.5]

    def yours(d):
        d.bookmarks = []  # removed there, untouched here: gone
        d.external_notes[0].text = "notes, edited there"  # edited there: stays
        d.live_flags = []  # removed there, changed here: stays
        d.assets.append(
            SyncAsset(
                id="l2",
                kind="link",
                title="Two",
                url="https://x.test/2",
                created_at=T0,
                added_at=T0,
            )
        )
        del d.speaker_embeddings["Ann"]

    out, _ = _merge(base, _ours(base, mine), _theirs(base, yours), binary)
    assert [b.id for b in out.bookmarks] == ["b2"]
    assert [n.text for n in out.external_notes] == ["notes, edited there"]
    assert [f.text for f in out.live_flags] == ["um, changed here"]
    assert [a.id for a in out.assets] == ["l1", "l2"]
    assert out.speaker_embeddings == {"Bo": [0.5]}


def test_agenda_and_whole_lists(binary):
    base = _doc()
    ours = _ours(base, lambda d: setattr(d.agenda[0], "covered", True))
    theirs = _theirs(
        base,
        lambda d: (
            setattr(d.agenda[1], "covered", True),
            setattr(d.summary_data, "chapters", [Chapter(start=0, title="Opening")]),
        ),
    )
    out, _ = _merge(base, ours, theirs, binary)
    assert [(a.text, a.covered) for a in out.agenda] == [("Scope", True), ("Budget", True)]
    assert [c.title for c in out.summary_data.chapters] == ["Opening"]
    # The agenda rewritten there, a point covered here: the rewrite, covered where the text
    # still matches. Chapters changed on both sides: the newer side's.
    ours = _ours(
        base,
        lambda d: (
            setattr(d.agenda[0], "covered", True),
            setattr(d.summary_data, "chapters", [Chapter(start=0, title="Mine")]),
        ),
        minutes=1,
    )
    theirs = _theirs(
        base,
        lambda d: (
            setattr(d, "agenda", [AgendaItem(text="Scope"), AgendaItem(text="Costs")]),
            setattr(d.summary_data, "chapters", [Chapter(start=0, title="Theirs")]),
        ),
        minutes=3,
    )
    out, report = _merge(base, ours, theirs, binary)
    assert [(a.text, a.covered) for a in out.agenda] == [("Scope", True), ("Costs", False)]
    assert [c.title for c in out.summary_data.chapters] == ["Theirs"]
    assert "chapters: theirs" in report["fields"]
    # Rewritten here (older), a point covered there: covering is not a rewrite.
    ours = _ours(base, lambda d: d.agenda.append(AgendaItem(text="Costs")), minutes=1)
    theirs = _theirs(base, lambda d: setattr(d.agenda[1], "covered", True), minutes=3)
    out, report = _merge(base, ours, theirs, binary)
    assert [(a.text, a.covered) for a in out.agenda] == [
        ("Scope", False),
        ("Budget", True),
        ("Costs", False),
    ]
    assert not [f for f in report["fields"] if f.startswith("agenda")]


# ---- transcript, events, recordings -------------------------------------------------------


def test_transcript_one_side_changed_takes_it(binary):
    base = _doc()
    edited = [TranscriptSegment(text="hello there, Bo", speaker="Ann", start=0.0, end=1.0)]
    out, report = _merge(base, _ours(base), _theirs(base, transcript=edited), binary)
    assert out.transcript == edited and report["transcript"] == "theirs"
    out, report = _merge(base, _ours(base, transcript=edited), _theirs(base), binary)
    assert out.transcript == edited and report["transcript"] == "ours"


def test_transcript_both_changed_the_origin_wins(binary):
    base = _doc()
    mine = [TranscriptSegment(text="mine", speaker="Ann", start=0.0, end=1.0)]
    yours = [TranscriptSegment(text="yours", speaker="Bo", start=0.0, end=1.0)]
    ours, theirs = _ours(base, transcript=mine), _theirs(base, transcript=yours)
    out, report = _merge(base, ours, theirs, binary, ours_is_origin=True)
    assert out.transcript == mine and report["transcript"] == "merged-origin"
    out, report = _merge(base, ours, theirs, binary, ours_is_origin=False)
    assert out.transcript == yours and report["transcript"] == "merged-origin"
    assert report["both"] == 1 and "transcript: theirs (origin)" in report["fields"]


def test_events_union_and_recordings_from_the_origin(binary):
    base = _doc()
    ours = _ours(base, events=[*base.events, SyncEvent(uid="e2", at="t", kind="summary_edited")])
    theirs = _theirs(
        base,
        events=[*base.events, SyncEvent(uid="e3", at="t", kind="imported")],
        recordings=[
            Recording(id="r1", source="mic", device_id=3, device_name="Mic", path="/b/mic.ogg")
        ],
        origin_audio=None,
    )
    out, _ = _merge(base, ours, theirs, binary, ours_is_origin=True)
    assert [e.uid for e in out.events] == ["e1", "e2", "e3"]
    assert out.recordings[0].path == "/a/mic.ogg" and out.origin_audio == "/a/mixed.ogg"
    out, _ = _merge(base, ours, theirs, binary, ours_is_origin=False)
    assert out.recordings[0].path == "/b/mic.ogg" and out.origin_audio is None


# ---- no base, no sidecar ------------------------------------------------------------------


def test_without_a_base_the_newer_side_is_taken_whole(binary):
    base = _doc()
    ours = _ours(base, lambda d: setattr(d, "notes", "Mine.\n"), minutes=1)
    theirs = _theirs(base, lambda d: setattr(d, "name", "Theirs"), minutes=2)
    theirs.events.append(SyncEvent(uid="e9", at="t", kind="imported"))
    out, report = merge(None, ours, theirs, ours_is_origin=True, binary=binary)
    assert out.name == "Theirs" and out.notes == "Intro.\nPlan.\n"
    assert report["replaced"] is True and report["taken"] == "theirs"
    assert out.vv == {"dev-a": 2, "dev-b": 1}
    assert [e.uid for e in out.events] == ["e1", "e9"]  # still a union
    assert out.recordings[0].path == "/a/mic.ogg"  # still the origin's
    out, report = merge(
        None, _ours(base, minutes=5, name="Mine"), theirs, ours_is_origin=True, binary=binary
    )
    assert out.name == "Mine" and report["taken"] == "ours"


def test_without_a_sidecar_texts_both_changed_take_the_newer_side():
    base = _doc()
    ours = _ours(base, lambda d: setattr(d, "notes", "Mine.\n"), minutes=1)
    theirs = _theirs(base, lambda d: setattr(d, "notes", "Theirs.\n"), minutes=2)
    out, report = merge(base, ours, theirs, ours_is_origin=True, binary=None)
    assert out.notes == "Theirs.\n"
    assert report["both"] == 1 and report["fields"] == ["notes: theirs (no text merge)"]
    assert report["both_texts"] == ["Theirs."]


def test_a_failing_sidecar_falls_back_the_same_way(sidecar, binary, monkeypatch):
    monkeypatch.setenv("FAKE_MERGE_FAIL", "1")
    base = _doc()
    ours = _ours(base, lambda d: setattr(d, "notes", "Mine.\n"), minutes=3)
    theirs = _theirs(base, lambda d: setattr(d, "notes", "Theirs.\n"), minutes=2)
    out, report = merge(base, ours, theirs, ours_is_origin=True, binary=binary)
    assert out.notes == "Mine.\n" and report["fields"] == ["notes: ours (no text merge)"]
    monkeypatch.setenv("FAKE_MERGE_FAIL", "")
    monkeypatch.setenv("FAKE_MERGE_ITEM_FAIL", "1")
    out, report = merge(base, ours, theirs, ours_is_origin=True, binary=binary)
    assert out.notes == "Mine.\n" and report["fields"] == ["notes: ours (no text merge)"]


# ---- apply --------------------------------------------------------------------------------


def _local(ctx) -> str:
    ctx.repo.device_id = "dev-a"
    s = ctx.repo.save(
        Session(
            name="Planning",
            status=SessionStatus.COMPLETED,
            audio_file="/a/mixed.ogg",
            summary="Intro.\n\nPlan.\n",
            summary_data=_data(),
            notes="Intro.\nPlan.\n",
            transcript=[TranscriptSegment(text="hello there", speaker="Ann", start=0.0, end=1.0)],
        )
    )
    return s.id


def _events(ctx, sid, kind):
    return [e for e in ctx.repo.events(sid) if e["kind"] == kind]


def test_apply_writes_logs_and_the_summary_doc_records_it(ctx, sidecar, binary):
    sid = _local(ctx)
    base = export_session(ctx.repo, sid, "dev-a")
    ctx.repo.update_fields(sid, notes="Intro.\nMine.\nPlan.\n")
    mine = ctx.repo.get(sid).summary_data.model_copy(deep=True)
    mine.action_items[0].text = "Send the final budget report to finance"
    ctx.repo.update_fields(sid, summary_data=mine)
    ours = export_session(ctx.repo, sid, "dev-a")

    def yours(d):
        d.notes = "Intro.\nPlan.\nTheirs.\n"
        d.summary_data.action_items[0].text = BUDGET + " by Friday"
        d.summary_data.action_items.pop(1)
        d.summary_data.action_items.append(ActionItem(id="y1", text=VENDOR))
        d.summary_data.edited_by = "Corey"
        d.summary_data.edited_at = T0

    theirs = _theirs(base, yours)
    assert vv_relation(ours.vv, theirs.vv) == "concurrent"
    merged, report = _merge(base, ours, theirs, binary)
    # a2 gone, y1 added, edited_at / edited_by set there; the notes and a1 merged
    assert report["kept"] == 4 and report["both"] == 2
    apply(ctx, merged, local_device_id="dev-a", by="Sync (Desk)", report=report)

    got = ctx.repo.get(sid)
    assert got.notes == "Intro.\nMine.\nPlan.\nTheirs.\n"
    assert [a.text for a in got.summary_data.action_items] == [
        "Send the final budget report to finance by Friday",
        VENDOR,
    ]
    assert got.summary_data.edited_by == "Corey" and got.summary_data.edited_at == T0
    assert got.audio_file == "/a/mixed.ogg" and got.status == SessionStatus.COMPLETED
    assert ctx.repo.sync_state(sid) == ("", {"dev-a": ours.vv["dev-a"] + 1, "dev-b": 1})
    doc = ensure_doc(ctx, sid)
    assert set(doc.blocks) >= {"a1", "y1", "d1", "q1"} and "a2" in doc.tombstones
    (event,) = _events(ctx, sid, "sync_merged")
    assert event["detail"]["source"] == "Sync (Desk)"
    assert event["detail"]["kept"] == report["kept"] and event["detail"]["transcript"] == "ours"
    assert event["detail"]["both"] == report["both"]


def test_apply_without_a_sidecar_still_writes_the_summary(ctx):
    sid = _local(ctx)
    base = export_session(ctx.repo, sid, "dev-a")
    ours = export_session(ctx.repo, sid, "dev-a")
    theirs = _theirs(base, lambda d: setattr(_item(d, "a1"), "text", BUDGET + "!"))
    merged, report = merge(base, ours, theirs, ours_is_origin=True, binary=None)
    apply(ctx, merged, local_device_id="dev-a", by="Sync (Desk)", report=report)
    got = ctx.repo.get(sid)
    assert got.summary_data.action_items[0].text == BUDGET + "!"
    assert ctx.repo.get_summary_doc(sid) is None
    assert _events(ctx, sid, "sync_merged")


def test_apply_keeps_a_version_when_the_transcript_is_replaced(ctx, sidecar, binary):
    sid = _local(ctx)
    base = export_session(ctx.repo, sid, "dev-a")
    ours = export_session(ctx.repo, sid, "dev-a")
    new = [TranscriptSegment(text="corrected there", speaker="Ann", start=0.0, end=1.0)]
    theirs = _theirs(base, transcript=new)
    merged, report = _merge(base, ours, theirs, binary)
    assert report["transcript"] == "theirs"
    apply(ctx, merged, local_device_id="dev-a", by="Sync (Desk)", report=report)
    assert [s.text for s in ctx.repo.get(sid).transcript] == ["corrected there"]
    versions = ctx.repo.versions(sid, with_content=True)
    assert [v["reason"] for v in versions] == ["synced"]
    assert versions[0]["content"]["transcript"][0]["text"] == "hello there"


# ---- the round trip: a merge is not merged again ---------------------------------------------


@pytest.fixture
def repo_a(tmp_path):
    r = SessionRepository(tmp_path / "a" / "db.sqlite")
    r.device_id = "dev-a"
    yield r
    r.close()


@pytest.fixture
def repo_b(tmp_path):
    r = SessionRepository(tmp_path / "b" / "db.sqlite")
    r.device_id = "dev-b"
    yield r
    r.close()


def test_round_trip_does_not_double_text(repo_a, repo_b, sidecar, binary):
    s = repo_a.save(
        Session(
            name="Planning",
            status=SessionStatus.COMPLETED,
            notes="Intro.\nPlan.\n",
            summary="Body.\n",
            summary_data=_data(),
        )
    )
    sid = s.id
    served = export_session(repo_a, sid, "dev-a")  # what B got: the base of A's merge
    write_session(repo_b, served, local_device_id="dev-b", vv=served.vv, bump=False)
    assert vv_relation(repo_a.sync_state(sid)[1], repo_b.sync_state(sid)[1]) == "equal"

    # Both edit the notes and an item concurrently.
    repo_a.update_fields(sid, notes="Intro.\nA's line.\nPlan.\n")
    data_a = repo_a.get(sid).summary_data.model_copy(deep=True)
    data_a.action_items[0].text = "Send the final budget report to finance"
    repo_a.update_fields(sid, summary_data=data_a)
    repo_b.update_fields(sid, notes="Intro.\nPlan.\nB's line.\n")
    data_b = repo_b.get(sid).summary_data.model_copy(deep=True)
    data_b.action_items[0].text = BUDGET + " by Friday"
    repo_b.update_fields(sid, summary_data=data_b)

    # A pulls B: concurrent, a three-way merge against what B had seen.
    ours, theirs = export_session(repo_a, sid, "dev-a"), export_session(repo_b, sid, "dev-b")
    assert vv_relation(ours.vv, theirs.vv) == "concurrent"
    merged, report = _merge(served, ours, theirs, binary)
    write_session(repo_a, merged, local_device_id="dev-a", vv=merged.vv, bump=True)
    assert repo_a.get(sid).notes == "Intro.\nA's line.\nPlan.\nB's line.\n"
    assert report["both"] == 2

    # B pulls A: A's merge dominates B's state, a fast-forward, nothing merged again.
    from_a = export_session(repo_a, sid, "dev-a")
    assert vv_relation(repo_b.sync_state(sid)[1], from_a.vv) == "dominated"
    runs = len(sidecar.requests)
    write_session(repo_b, from_a, local_device_id="dev-b", vv=from_a.vv, bump=False)
    assert len(sidecar.requests) == runs
    got = repo_b.get(sid)
    assert got.notes == "Intro.\nA's line.\nPlan.\nB's line.\n"
    assert got.notes.count("A's line") == 1 and got.notes.count("B's line") == 1
    assert (
        got.summary_data.action_items[0].text == "Send the final budget report to finance by Friday"
    )
    assert vv_relation(repo_a.sync_state(sid)[1], repo_b.sync_state(sid)[1]) == "equal"
    # Nothing to do on the next pull either way.
    back = export_session(repo_b, sid, "dev-b")
    assert vv_relation(repo_a.sync_state(sid)[1], back.vv) == "equal"


# ---- the real sidecar ---------------------------------------------------------------------


@pytest.fixture
def real_sidecar(monkeypatch):
    monkeypatch.delenv("MNEMOSYNE_LINK_BIN", raising=False)
    binary = find_binary()
    if binary is None:
        pytest.skip("no mnemosyne-link binary")
    try:
        result = merge_many(binary, [{"base": "a\n", "app": "a\n", "vault": "a b\n"}])
    except MergeError:
        pytest.skip("this mnemosyne-link has no batch `notes merge`")
    if isinstance(result[0], MergeError):
        pytest.skip("this mnemosyne-link has no `base` form")
    return binary


def test_real_sidecar_merges_body_items_and_notes(real_sidecar):
    base = _doc()

    def mine(d):
        d.summary = "Intro.\n\nMy A line.\n\nPlan.\n"
        d.summary_data.action_items[0].text = "Send the final budget report to finance"
        d.bookmarks[0].note = "Budget talk, long"

    def yours(d):
        d.summary = "Intro.\n\nPlan.\n\nB line.\n"
        d.summary_data.action_items[0].text = BUDGET + " by Friday"
        d.bookmarks[0].note = "The budget talk"

    out, report = _merge(base, _ours(base, mine), _theirs(base, yours), real_sidecar)
    assert out.summary == "Intro.\n\nMy A line.\n\nPlan.\n\nB line.\n"
    assert _items(out)[0] == "Send the final budget report to finance by Friday"
    assert out.bookmarks[0].note == "The budget talk, long"
    assert report["both"] == 3


def test_real_sidecar_identical_edit_kept_once(real_sidecar):
    base = _doc()

    def same(d):
        d.notes = "Intro.\nSame line.\nPlan.\n"
        d.summary_data.action_items[0].text = BUDGET + " today"

    ours = _ours(base, same)
    theirs = _theirs(base, same)
    theirs.notes = "Intro.\nSame line.\nPlan.\nMore there.\n"
    out, _ = _merge(base, ours, theirs, real_sidecar)
    assert out.notes == "Intro.\nSame line.\nPlan.\nMore there.\n"
    assert out.notes.count("Same line") == 1
    assert _items(out)[0] == BUDGET + " today"


def test_real_sidecar_round_trip_does_not_double(repo_a, repo_b, real_sidecar):
    s = repo_a.save(Session(name="P", status=SessionStatus.COMPLETED, notes="Intro.\nPlan.\n"))
    sid = s.id
    served = export_session(repo_a, sid, "dev-a")
    write_session(repo_b, served, local_device_id="dev-b", vv=served.vv, bump=False)
    repo_a.update_fields(sid, notes="Intro.\nA's line.\nPlan.\n")
    repo_b.update_fields(sid, notes="Intro.\nPlan.\nB's line.\n")
    ours, theirs = export_session(repo_a, sid, "dev-a"), export_session(repo_b, sid, "dev-b")
    merged, _ = _merge(served, ours, theirs, real_sidecar)
    write_session(repo_a, merged, local_device_id="dev-a", vv=merged.vv, bump=True)
    from_a = export_session(repo_a, sid, "dev-a")
    assert vv_relation(repo_b.sync_state(sid)[1], from_a.vv) == "dominated"
    write_session(repo_b, from_a, local_device_id="dev-b", vv=from_a.vv, bump=False)
    assert repo_b.get(sid).notes == "Intro.\nA's line.\nPlan.\nB's line.\n"


def test_module_exports():
    assert sync_merge.BOTH_TEXTS == 5


# ---- comments -----------------------------------------------------------------------------


def test_comments_added_on_each_side_are_both_kept(binary):
    base = _doc(comments=[_comment("c1")])

    def mine(d):
        d.comments.append(_comment("c2", "Mine", parent_id="c1"))

    def yours(d):
        d.comments.append(_comment("c3", "Yours"))

    out, report = _merge(base, _ours(base, mine), _theirs(base, yours), binary)
    assert [c.id for c in out.comments] == ["c1", "c2", "c3"]
    assert report["kept"] == 1


def test_comment_deleted_vs_edited(binary):
    base = _doc(comments=[_comment("c1"), _comment("c2"), _comment("c3")])

    def mine(d):
        d.comments = [c for c in d.comments if c.id != "c1"]  # removed here, edited there
        d.comments = [c for c in d.comments if c.id != "c2"]  # removed here, untouched there

    def yours(d):
        d.comments[0].text = "Edited there"
        d.comments[0].updated_at = T0.replace(minute=5).isoformat()

    out, _ = _merge(base, _ours(base, mine), _theirs(base, yours), binary)
    assert {c.id for c in out.comments} == {"c1", "c3"}
    assert next(c for c in out.comments if c.id == "c1").text == "Edited there"


def test_comment_resolved_on_one_side_is_taken(binary):
    base = _doc(comments=[_comment("c1"), _comment("c2")])
    at = T0.replace(minute=7).isoformat()

    def mine(d):
        d.comments[0].resolved_at, d.comments[0].resolved_by = at, "u2"

    def yours(d):
        d.comments[1].text = ""
        d.comments[1].deleted = True

    out, report = _merge(base, _ours(base, mine), _theirs(base, yours), binary)
    c1, c2 = out.comments
    assert (c1.resolved_at, c1.resolved_by, c1.deleted) == (at, "u2", False)
    assert c2.deleted and c2.text == "" and c2.resolved_at is None
    assert report["kept"] >= 1 and report["both"] == 0


def test_comment_resolved_on_both_sides_takes_the_newer_side(binary):
    base = _doc(comments=[_comment("c1")])

    def mine(d):
        d.comments[0].resolved_at, d.comments[0].resolved_by = T0.isoformat(), "u-mine"

    def yours(d):
        d.comments[0].resolved_at, d.comments[0].resolved_by = T0.isoformat(), "u-yours"

    out, report = _merge(base, _ours(base, mine), _theirs(base, yours), binary)  # theirs is newer
    assert out.comments[0].resolved_by == "u-yours"
    assert report["both"] == 1


def test_comment_text_edited_on_both_sides_is_merged(sidecar, binary):
    base = _doc(comments=[_comment("c1", "Intro.\nPlan.\n")])

    def mine(d):
        d.comments[0].text = "Intro.\nA line.\nPlan.\n"
        d.comments[0].updated_at = T0.replace(minute=3).isoformat()

    def yours(d):
        d.comments[0].text = "Intro.\nPlan.\nB line.\n"
        d.comments[0].updated_at = T0.replace(minute=4).isoformat()

    out, report = _merge(base, _ours(base, mine), _theirs(base, yours), binary)
    assert out.comments[0].text == "Intro.\nA line.\nPlan.\nB line.\n"
    assert out.comments[0].updated_at == T0.replace(minute=4).isoformat()
    assert len(sidecar.requests) == 1 and report["both"] >= 1


def test_comment_text_edited_on_both_sides_without_a_sidecar_takes_the_newer(binary):
    base = _doc(comments=[_comment("c1", "Intro.\n")])
    ours = _ours(base, lambda d: setattr(d.comments[0], "text", "Mine\n"))
    theirs = _theirs(base, lambda d: setattr(d.comments[0], "text", "Yours\n"))
    out, report = merge(base, ours, theirs, ours_is_origin=True, binary=None)
    assert out.comments[0].text == "Yours\n"
    assert any("comment c1 text" in f for f in report["fields"])
