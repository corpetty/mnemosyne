"""The record sealer (services/record_sealer.py) and the guard it stands on.

The coverage tests are the point (docs/plans/2026-11-09-records-by-frontier.md, section 7,
"Coverage without `_touch`"): a seal vouches for seven parts of a meeting and `verify` tells the
app's own changes ("pending", the meeting's `sync_seq` passed the seal's `rev`) from changes made
outside it. A write path to a covered part that does not advance `sync_seq` would show as
tampering. So every app path is driven here, and after each one the test checks that

  1. the part it should change did change (the step is not a no-op),
  2. the meeting's `sync_seq` is past the last seal's `rev`,
  3. `verify` says ok + pending (never a problem),
  4. the sealer, once the meeting was quiet, seals it with a reason naming the parts that
     differ, and `verify` is ok and not pending again.

The notes are written through the sidecar stand-in of tests/fake_link.py.
"""

import asyncio
import base64
import json
from datetime import datetime
from types import SimpleNamespace

import pytest

from mnemosyne.models.session import (
    ActionItem,
    AgendaItem,
    ExternalNotes,
    Recording,
    Session,
    SessionStatus,
    SummaryData,
    TranscriptSegment,
)
from mnemosyne.services import (
    decision_links,
    records,
    shared_notes,
    sync_state,
    task_links,
)
from mnemosyne.services.record_sealer import (
    CURSOR_KEY,
    MAX_WAIT,
    QUIET,
    RecordSealer,
    changed_reason,
)
from mnemosyne.storage.records import PARTS, compare_parts, part_digests, part_payloads
from tests import fake_link
from tests.fake_link import doc_frame


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def sidecar(tmp_path, monkeypatch):
    return fake_link.install(tmp_path, monkeypatch)


@pytest.fixture(autouse=True)
def only_the_sealer_seals(monkeypatch):
    """The routes seal in a background thread (records.seal_later): in a test that would land
    in the middle of a step. The sealer under test is what seals here."""
    monkeypatch.setattr(records, "seal_later", lambda app, sid, reason: None)


@pytest.fixture(autouse=True)
def no_timed_folds(monkeypatch):
    monkeypatch.setattr(shared_notes, "FOLD_DELAY", 1000.0)
    monkeypatch.setattr(shared_notes, "FOLD_EVERY", 1000.0)


SEGS = [
    TranscriptSegment(
        text="We will ship the release on Friday", speaker="SPEAKER_00", start=0, end=4
    ),
    TranscriptSegment(text="Sounds good to me", speaker="SPEAKER_01", start=4.5, end=6),
    TranscriptSegment(text="Let's also update the docs", speaker="SPEAKER_00", start=6.5, end=8),
]


def meeting(ctx, name="Weekly", day=10, **kw) -> str:
    fields = dict(
        name=name,
        status=SessionStatus.COMPLETED,
        created_at=datetime(2026, 11, day, 10),
        transcript=[s.model_copy() for s in SEGS],
        participants=["SPEAKER_00", "SPEAKER_01"],
        attendees=["Ann", "Bob"],
        summary="The team will ship on Friday.",
        summary_data=SummaryData(
            topics=["Release"],
            decisions=["Ship on Friday"],
            decision_ids=["d1"],
            action_items=[
                ActionItem(id="a1", text="Update the docs", owner="Ann"),
                ActionItem(id="a2", text="Tag the release", owner="Bob"),
            ],
        ),
        agenda=[AgendaItem(text="Release date"), AgendaItem(text="Docs")],
    )
    fields.update(kw)
    return ctx.repo.save(Session(**fields)).id


def parts_of(ctx, sid) -> dict[str, str]:
    session = ctx.repo.get(sid)
    return part_digests(part_payloads(session, ctx.repo.list_comments(sid)))


def new_sealer(ctx, clock) -> RecordSealer:
    """A sealer that starts looking now (what is there is not dirty)."""
    ctx.repo.set_meta(CURSOR_KEY, str(ctx.repo.current_sync_seq()))
    sealer = RecordSealer(ctx, clock)
    sealer.interval = 0
    sealer.start()
    return sealer


def tick(sealer) -> list[str]:
    return asyncio.run(sealer.tick())


class Rig:
    """One sealed meeting, a sealer on a clock, and `step`: do something, check the guard."""

    def __init__(self, ctx, clock, sid, table):
        self.ctx, self.clock, self.sid, self.table = ctx, clock, sid, table
        if not ctx.repo.seals(sid):
            assert records.seal(ctx, sid, "baseline") is not None
        self.sealer = new_sealer(ctx, clock)

    def step(self, label, fn, expect, *, also=(), sid=None):
        """Run `fn`; every covered part that differs from the last seal must have come with a
        newer `sync_seq`, on this meeting and on each of `also` (other meetings `fn` writes)."""
        ctx = self.ctx
        sids = [sid or self.sid, *also]
        lasts = {s: ctx.repo.seals(s)[-1] for s in sids}
        fn()
        for s in sids:
            last = lasts[s]
            changed = [d.name for d in compare_parts(last["manifest"]["parts"], parts_of(ctx, s))]
            if s == sids[0]:
                assert set(expect) <= set(changed), f"{label}: expected {expect}, changed {changed}"
            elif s in also:
                assert changed, f"{label}: {s} was expected to change"
            if not changed:
                continue
            assert ctx.repo.sync_seq_of(s) > last["manifest"]["rev"], (
                f"{label}: {changed} changed on {s} without advancing sync_seq"
            )
            check = records.verify(ctx, s)
            assert check.ok and check.pending, f"{label}: {check.problems}"
        # The sealer: dirty at once, sealed after the quiet time, with the parts as the reason.
        before = {s: len(ctx.repo.seals(s)) for s in sids}
        assert tick(self.sealer) == [], f"{label}: sealed before the meeting was quiet"
        self.clock.t += QUIET + 1
        reasons = tick(self.sealer)
        for s in sids:
            last = lasts[s]
            now = parts_of(ctx, s)
            differing = [p for p in PARTS if last["manifest"]["parts"][p] != now[p]]
            seals = ctx.repo.seals(s)
            if differing:
                assert len(seals) == before[s] + 1, f"{label}: {s} was not sealed"
                assert seals[-1]["reason"] == changed_reason(differing), label
                assert seals[-1]["reason"] in reasons
            else:
                assert len(seals) == before[s], f"{label}: sealed with nothing to seal"
            check = records.verify(ctx, s)
            assert check.ok and not check.pending, f"{label}: {check.problems}"
        self.table.append(label)


@pytest.fixture
def table():
    """The paths covered, in order (read it with -s)."""
    rows: list[str] = []
    yield rows


@pytest.fixture
def rig(ctx, clock, table):
    return Rig(ctx, clock, meeting(ctx), table)


# ---- coverage: comments -----------------------------------------------------------------------


def test_comment_paths_advance_sync_seq(client, ctx, rig, sidecar):
    sid, made = rig.sid, {}

    def url(rest=""):
        return f"/api/sessions/{sid}/comments{rest}"

    def add():
        r = client.post(url(), json={"text": "Really?", "target": "segment", "idx": 1})
        assert r.status_code == 200, r.text
        made["c"] = r.json()["id"]

    rig.step("comment add", add, ["comments"])
    rig.step(
        "comment reply",
        lambda: client.post(url(), json={"text": "Yes", "parent_id": made["c"]}).raise_for_status(),
        ["comments"],
    )
    rig.step(
        "comment edit",
        lambda: client.patch(
            url(f"/{made['c']}"), json={"text": "Really, Friday?"}
        ).raise_for_status(),
        ["comments"],
    )
    rig.step(
        "comment resolve",
        lambda: client.post(url(f"/{made['c']}/resolve")).raise_for_status(),
        ["comments"],
    )
    rig.step(
        "comment reopen",
        lambda: client.post(url(f"/{made['c']}/reopen")).raise_for_status(),
        ["comments"],
    )
    rig.step(
        "comment approve",
        lambda: client.post(url(f"/{made['c']}/approve")).raise_for_status(),
        ["comments"],
    )
    rig.step(
        "comment hide",
        lambda: client.post(url(f"/{made['c']}/hide")).raise_for_status(),
        ["comments"],
    )
    rig.step(
        "comment delete",
        lambda: client.delete(url(f"/{made['c']}")).raise_for_status(),
        ["comments"],
    )


def test_comments_on_summary_items_and_audio_moments(client, ctx, rig):
    sid = rig.sid

    def post(**body):
        r = client.post(f"/api/sessions/{sid}/comments", json={"text": "Note", **body})
        assert r.status_code == 200, r.text

    rig.step("comment on an action item", lambda: post(target="item", target_id="a1"), ["comments"])
    rig.step("comment on a decision", lambda: post(target="decision", target_id="d1"), ["comments"])
    rig.step(
        "comment on an audio moment", lambda: post(target="segment", seconds=3.0), ["comments"]
    )
    rig.step(
        "comment on the summary text",
        lambda: post(target="body", quote="ship on Friday", prefix="will ", suffix="."),
        ["comments"],
    )


def test_a_comment_on_a_task_object_reaches_every_mention(client, ctx, rig):
    other = meeting(ctx, "Earlier", day=1)
    # (the object's comment attaches to a mention: `comments.create` for kind "task")
    tid = task_links.link_pair(ctx, (rig.sid, "a1"), (other, "a1"))
    records.seal(ctx, other, "baseline")
    rig.step(
        "task object comment",
        lambda: client.post(
            f"/api/tasks/{tid}/comments", json={"text": "Who has this?"}
        ).raise_for_status(),
        ["comments"],
    )


# ---- coverage: notes --------------------------------------------------------------------------


def frame(frame_id: str, *ops) -> bytes:
    return base64.b64decode(doc_frame(frame_id, *ops))


def settle_ws(ws) -> None:
    ws.send_text(json.dumps({"type": "ping"}))
    assert ws.receive_json() == {"type": "pong"}


def test_notes_paths_advance_sync_seq(client, ctx, rig, sidecar):
    sid = rig.sid
    rig.step(
        "notes route, through the document",
        lambda: client.post(
            f"/api/sessions/{sid}/notes", json={"notes": "from the api\n"}
        ).raise_for_status(),
        ["notes"],
    )
    with client.websocket_connect(f"/ws/notes/{sid}") as ws:
        hello = ws.receive_json()
        if hello["snapshot"]:
            ws.receive_bytes()  # the document as stored
        for _ in range(hello["backlog"]):
            ws.receive_bytes()

        def type_and_fold():
            ws.send_bytes(frame("f1", ["ins", 0, "typed live\n"]))
            settle_ws(ws)
            assert shared_notes.fold_blocking(ctx, sid)

        rig.step("notes typed live, folded", type_and_fold, ["notes"])

        # Formatting that leaves the markdown alone: the snapshot changes, the digest does not.
        def artifact_only():
            ws.send_bytes(bytes([0xAF]) + b"an artifact")
            settle_ws(ws)
            shared_notes.fold_blocking(ctx, sid)

        rig.step("an artifact frame (markdown unchanged)", artifact_only, [])

        def comment_on_notes():
            r = client.post(
                f"/api/sessions/{sid}/comments",
                json={"text": "Nice", "target": "notes", "exact": "typed live"},
            )
            assert r.status_code == 200, r.text
            settle_ws(ws)  # (the server's mark frame has reached the editors)
            shared_notes.fold_blocking(ctx, sid)

        rig.step(
            "comment on the notes (the mark goes into the document)", comment_on_notes, ["comments"]
        )
    # The notes written around the document (a sync text merge, no sidecar): caught up on the
    # next fold, but the write itself is a version.
    rig.step(
        "notes written directly",
        lambda: ctx.sessions.update_notes(sid, "written around it\n"),
        ["notes"],
    )


def test_notes_without_a_sidecar(client, ctx, rig):
    sid = rig.sid
    rig.step(
        "notes route, no sidecar",
        lambda: client.post(
            f"/api/sessions/{sid}/notes", json={"notes": "plain\n"}
        ).raise_for_status(),
        ["notes"],
    )


# ---- coverage: summary, items, speakers, details ----------------------------------------------


def test_summary_and_item_paths_advance_sync_seq(client, ctx, rig):
    sid = rig.sid
    rig.step(
        "summary edit",
        lambda: client.put(
            f"/api/sessions/{sid}/summary",
            json={
                "summary": "Edited by hand.",
                "action_items": [
                    {"id": "a1", "text": "Update the docs", "owner": "Ann"},
                    {"id": "a2", "text": "Tag the release", "owner": "Bob"},
                ],
                "decisions": [{"id": "d1", "text": "Ship on Friday"}],
            },
        ).raise_for_status(),
        ["summary"],
    )
    rig.step(
        "follow-up draft",
        lambda: client.put(
            f"/api/sessions/{sid}/followup", json={"text": "Hi all"}
        ).raise_for_status(),
        ["summary_data"],
    )
    base = f"/api/sessions/{sid}/action-items/0"
    rig.step(
        "action item tick",
        lambda: client.patch(base, json={"done": True}).raise_for_status(),
        ["summary_data"],
    )
    rig.step(
        "action item untick",
        lambda: client.patch(base, json={"done": False}).raise_for_status(),
        ["summary_data"],
    )
    rig.step(
        "action item follow",
        lambda: client.patch(base, json={"follow": True}).raise_for_status(),
        ["summary_data"],
    )
    rig.step(
        "action item due",
        lambda: client.patch(base, json={"due": "2026-12-01"}).raise_for_status(),
        ["summary_data"],
    )
    rig.step(
        "action item owner",
        lambda: client.patch(base, json={"owner": "Cleo"}).raise_for_status(),
        ["summary_data"],
    )
    rig.step(
        "action item by id (repository)",
        lambda: ctx.repo.set_action_item_fields_by_id(sid, "a2", done=True),
        ["summary_data"],
    )
    rig.step(
        "action item by index (repository)",
        lambda: (
            ctx.repo.set_action_item_done(sid, 1, False),
            ctx.repo.set_action_item_follow(sid, 1, True),
        ),
        ["summary_data"],
    )


def test_speaker_rename_and_transcript_paths(client, ctx, rig):
    sid = rig.sid
    rig.step(
        "speaker rename",
        lambda: client.post(
            f"/api/sessions/{sid}/speakers/rename",
            json={"label": "SPEAKER_00", "name": "Ann", "enroll": False},
        ).raise_for_status(),
        ["transcript", "meta"],
    )
    rig.step(
        "segment edit",
        lambda: client.patch(
            f"/api/sessions/{sid}/segments/1", json={"text": "Sounds fine"}
        ).raise_for_status(),
        ["transcript"],
    )
    rig.step(
        "transcript replaced",
        lambda: ctx.sessions.set_transcript(
            sid, [TranscriptSegment(text="A new one", speaker="Ann", start=0, end=2)]
        ),
        ["transcript"],
    )


def test_details_paths_advance_sync_seq(client, ctx, rig):
    sid = rig.sid
    rig.step(
        "meeting renamed",
        lambda: client.patch(
            f"/api/sessions/{sid}", json={"name": "Weekly sync"}
        ).raise_for_status(),
        ["meta"],
    )
    rig.step(
        "attendees",
        lambda: client.put(
            f"/api/sessions/{sid}/attendees", json={"attendees": ["Ann", "Cleo"]}
        ).raise_for_status(),
        ["meta"],
    )
    rig.step(
        "agenda",
        lambda: client.put(
            f"/api/sessions/{sid}/agenda",
            json={"items": [{"text": "Release date", "covered": False}, {"text": "Hiring"}]},
        ).raise_for_status(),
        ["meta"],
    )
    rig.step(
        "agenda covered (the copilot)",
        lambda: ctx.repo.update_fields(
            sid, agenda=[AgendaItem(text="Release date", covered=True), AgendaItem(text="Hiring")]
        ),
        ["meta"],
    )
    rig.step(
        "participants",
        lambda: ctx.repo.update_fields(sid, participants=["Ann", "Cleo"]),
        ["meta"],
    )


@pytest.mark.xfail(
    strict=True,
    reason="REPORTED, not fixed: SessionRepository.adopt_unowned changes owner_id (part `meta`) "
    "without `_touch`, on purpose (tests/test_sync_storage.py NEVER pins 'local bookkeeping'). "
    "Turning team mode on would show every sealed older meeting as changed outside the app. "
    "Fix: `_touch(sid, keep_updated=True)` per adopted meeting and drop it from NEVER.",
)
def test_adopting_unowned_meetings_advances_sync_seq(ctx, rig):
    rig.step(
        "owner (team mode first turned on)",
        lambda: ctx.repo.adopt_unowned("owner-1"),
        ["meta"],
    )


def test_external_notes_paths_advance_sync_seq(client, ctx, rig):
    sid, made = rig.sid, {}

    def add():
        r = client.post(
            f"/api/sessions/{sid}/external-notes",
            json={"text": "Notes by Gemini: ship it", "source": "gemini"},
        )
        assert r.status_code == 200, r.text
        made["id"] = r.json()["id"]

    rig.step("external notes pasted", add, ["external_notes"])
    rig.step(
        "external notes deleted",
        lambda: client.delete(
            f"/api/sessions/{sid}/external-notes/{made['id']}"
        ).raise_for_status(),
        ["external_notes"],
    )
    rig.step(
        "external notes (repository)",
        lambda: ctx.repo.add_external_notes(sid, ExternalNotes(source="zoom", text="Zoom's notes")),
        ["external_notes"],
    )


# ---- coverage: library sync -------------------------------------------------------------------


def test_apply_synced_advances_sync_seq(ctx, rig):
    """A peer's version written by library sync (fast-forward and merge) moves every covered
    part and the change counter."""
    sid = rig.sid
    repo = ctx.repo

    def arrive(bump: bool):
        doc = sync_state.export_session(repo, sid, repo.device_id)
        doc.name = f"Renamed by the peer {bump}"
        doc.summary = f"Peer summary {bump}"
        doc.notes = f"peer notes {bump}\n"
        doc.transcript = [
            TranscriptSegment(text=f"Peer line {bump}", speaker="SPEAKER_00", start=0, end=2)
        ]
        doc.agenda = [AgendaItem(text="From the peer")]
        doc.external_notes = [ExternalNotes(id=f"x{bump}", source="zoom", text="theirs")]
        vv = dict(doc.vv)
        vv["peer"] = vv.get("peer", 0) + 1
        sync_state.write_session(repo, doc, local_device_id=repo.device_id, vv=vv, bump=bump)

    for bump in (False, True):
        rig.step(
            f"apply_synced (bump={bump})",
            lambda bump=bump: arrive(bump),
            ["transcript", "summary", "notes", "meta", "external_notes"],
        )


# ---- coverage: tasks and decisions across meetings --------------------------------------------


def _two_linked(ctx):
    """An older and a newer meeting sharing the task a1 and the decision d1, both sealed."""
    old = meeting(ctx, "Earlier", day=1)
    new = meeting(ctx, "Later", day=8)
    tid = task_links.link_pair(ctx, (old, "a1"), (new, "a1"))
    did = decision_links.link_pair(ctx, (old, "d1"), (new, "d1"))
    for s in (old, new):
        records.seal(ctx, s, "baseline")
    return old, new, tid, did


def test_task_and_decision_fan_out_reach_every_meeting(ctx, clock, table):
    old, new, tid, did = _two_linked(ctx)
    rig = Rig(ctx, clock, new, table)
    records.seal(ctx, old, "baseline")  # (new Rig sealer started after the links were made)
    rig.sealer = new_sealer(ctx, clock)
    rig.step(
        "task done, fanned out",
        lambda: task_links.set_done(ctx, tid, True),
        ["summary_data"],
        also=[old],
    )
    rig.step(
        "task follow, fanned out",
        lambda: task_links.set_follow(ctx, tid, True),
        ["summary_data"],
        also=[old],
    )
    rig.step(
        "task issue link, fanned out",
        lambda: task_links.set_issue_url(ctx, tid, "https://x/1"),
        ["summary_data"],
        also=[old],
    )
    rig.step(
        "task reopened, fanned out",
        lambda: task_links.set_done(ctx, tid, False),
        ["summary_data"],
        also=[old],
    )
    rig.step(
        "task unlinked", lambda: task_links.unlink(ctx, new, "a1"), ["summary_data"], also=[old]
    )
    rig.step(
        "task linked again",
        lambda: task_links.link_pair(ctx, (old, "a1"), (new, "a1")),
        ["summary_data"],
        also=[old],
    )
    rig.step(
        "decision unlinked",
        lambda: decision_links.unlink(ctx, new, "d1"),
        ["summary_data"],
        also=[old],
    )
    rig.step(
        "decision linked",
        lambda: decision_links.link_pair(ctx, (old, "d1"), (new, "d1")),
        ["summary_data"],
        also=[old],
    )


def test_reconcile_after_a_tick_arrived_for_one_mention(ctx, clock, table):
    """Startup and after sync: a tick merged in for the older mention only; the head wins."""
    old, new, tid, did = _two_linked(ctx)
    rig = Rig(ctx, clock, new, table)
    records.seal(ctx, old, "baseline")
    rig.sealer = new_sealer(ctx, clock)

    def a_tick_arrives_then_reconcile():
        ctx.repo.set_action_item_flag_by_id(old, "a1", "done", True)  # only the older mention
        ctx.repo.set_action_item_flag_by_id(new, "a1", "follow", True)  # the head's own change
        assert task_links.reconcile(ctx) == 1

    rig.step(
        "reconcile (head wins)",
        a_tick_arrives_then_reconcile,
        ["summary_data"],
        also=[old],
    )


def test_linking_new_items_touches_the_older_meeting(ctx, clock, table):
    old = meeting(ctx, "Earlier", day=1)
    new = meeting(ctx, "Later", day=8)
    records.seal(ctx, old, "baseline")
    rig = Rig(ctx, clock, new, table)
    rig.step(
        "auto-linking a new summary's items",
        lambda: task_links.link_items(ctx, new, "exact"),
        ["summary_data"],
        also=[old],
    )
    rig.step(
        "auto-linking decisions",
        lambda: decision_links.link_items(ctx, new),
        ["summary_data"],
        also=[old],
    )


# ---- the sealer's rules -----------------------------------------------------------------------


def test_a_change_is_sealed_after_the_quiet_time_with_the_parts_as_reason(ctx, clock):
    sid = meeting(ctx)
    records.seal(ctx, sid, "baseline")
    sealer = new_sealer(ctx, clock)
    assert tick(sealer) == []
    ctx.repo.update_fields(sid, notes="hello\n")
    ctx.repo.update_fields(sid, name="Renamed")
    assert tick(sealer) == []
    clock.t += QUIET - 1
    assert tick(sealer) == []
    assert len(ctx.repo.seals(sid)) == 1
    clock.t += 2
    assert tick(sealer) == ["changed: notes, meta"]
    assert [s["reason"] for s in ctx.repo.seals(sid)] == ["baseline", "changed: notes, meta"]
    assert tick(sealer) == []  # nothing waiting any more
    check = records.verify(ctx, sid)
    assert check.ok and not check.pending and check.seals == 2


def test_each_change_restarts_the_quiet_time_up_to_the_longest_wait(ctx, clock):
    sid = meeting(ctx)
    records.seal(ctx, sid, "baseline")
    sealer = new_sealer(ctx, clock)
    n = 0
    sealed: list[str] = []
    # Someone keeps typing every 30 s: never quiet for a minute, but not unsealed forever.
    while clock.t < 1000.0 + MAX_WAIT + 100 and not sealed:
        n += 1
        ctx.repo.update_fields(sid, notes=f"line {n}\n")
        sealed = tick(sealer)
        if not sealed:
            assert len(ctx.repo.seals(sid)) == 1
        clock.t += 30
    assert sealed == ["changed: notes"]
    assert 1000.0 + MAX_WAIT <= clock.t - 30 < 1000.0 + MAX_WAIT + 40


def test_a_change_that_no_part_sees_seals_nothing(ctx, clock):
    sid = meeting(ctx)
    records.seal(ctx, sid, "baseline")
    sealer = new_sealer(ctx, clock)
    seq = ctx.repo.sync_seq_of(sid)
    ctx.repo.set_notes_items(sid, [])  # derived state: not a part, not a version
    ctx.repo.update_fields(sid, local_only=True)  # a travelling field that is not covered
    assert ctx.repo.sync_seq_of(sid) > seq
    tick(sealer)
    clock.t += QUIET + 1
    assert tick(sealer) == []
    assert len(ctx.repo.seals(sid)) == 1
    check = records.verify(ctx, sid)
    assert check.ok and not check.pending


def test_a_meeting_being_recorded_or_in_a_job_waits(ctx, clock, monkeypatch):
    sid, other = meeting(ctx), meeting(ctx, "Other", day=2)
    for s in (sid, other):
        records.seal(ctx, s, "baseline")
    sealer = new_sealer(ctx, clock)
    ctx.repo.update_fields(sid, notes="x\n")
    ctx.repo.update_fields(other, notes="y\n")
    tick(sealer)
    clock.t += QUIET + 1
    ctx.active_recordings[sid] = object()  # (busy_sessions only looks at the key)
    assert tick(sealer) == ["changed: notes"]  # the other one
    assert len(ctx.repo.seals(sid)) == 1 and len(ctx.repo.seals(other)) == 2
    clock.t += 600
    assert tick(sealer) == []  # even past the longest wait: a recording is not interrupted
    del ctx.active_recordings[sid]
    monkeypatch.setattr(ctx.jobs, "list", lambda **kw: [SimpleNamespace(session_id=sid)])
    assert tick(sealer) == []  # a job (a transcription, a publish) is over first
    monkeypatch.undo()
    assert tick(sealer) == ["changed: notes"]
    assert len(ctx.repo.seals(sid)) == 2


def test_a_meeting_shared_with_us_is_never_sealed(ctx, clock):
    sid = meeting(ctx)
    with ctx.repo._lock, ctx.repo._conn:  # (what SharePuller's apply_shared leaves)
        ctx.repo._conn.execute("UPDATE sessions SET shared_from='contact-1' WHERE id=?", (sid,))
        ctx.repo._touch(sid)
    sealer = new_sealer(ctx, clock)
    with ctx.repo._lock, ctx.repo._conn:
        ctx.repo._conn.execute("UPDATE sessions SET notes='x' WHERE id=?", (sid,))
        ctx.repo._touch(sid)
    tick(sealer)
    clock.t += QUIET + 1
    assert tick(sealer) == []
    assert ctx.repo.seals(sid) == []
    assert sid not in {s for s, _ in ctx.repo.seal_candidates(0)}  # not even looked at


def test_first_start_seals_nothing_old_and_a_restart_catches_up(ctx, clock):
    sid = meeting(ctx)
    records.seal(ctx, sid, "baseline")
    ctx.repo.update_fields(sid, notes="before the sealer ever ran\n")
    assert ctx.repo.get_meta(CURSOR_KEY) is None
    sealer = RecordSealer(ctx, clock)
    sealer.interval = 0
    sealer.start()  # first start: the cursor is the current number
    assert ctx.repo.get_meta(CURSOR_KEY) == str(ctx.repo.current_sync_seq())
    clock.t += QUIET + 1
    assert tick(sealer) == []  # an upgrade does not seal the library
    # A change while the sealer waits, the backend stops before it was sealed ...
    ctx.repo.update_fields(sid, notes="changed, then the backend stopped\n")
    assert tick(sealer) == []
    assert int(ctx.repo.get_meta(CURSOR_KEY)) < ctx.repo.sync_seq_of(sid)
    # ... the next start finds it again, and seals it once it was quiet.
    again = RecordSealer(ctx, clock)
    again.interval = 0
    again.start()
    assert tick(again) == []
    clock.t += QUIET + 1
    assert tick(again) == ["changed: notes"]
    assert int(ctx.repo.get_meta(CURSOR_KEY)) == ctx.repo.current_sync_seq()


def test_a_meeting_with_nothing_to_keep_is_not_sealed(ctx, clock):
    sid = ctx.repo.save(Session(name="Empty")).id
    sealer = new_sealer(ctx, clock)
    ctx.repo.update_fields(sid, name="Still empty")
    tick(sealer)
    clock.t += QUIET + 1
    assert tick(sealer) == []
    assert ctx.repo.seals(sid) == []
    assert not sealer._dirty and not sealer._failures  # and not retried either


def test_a_notes_only_meeting_is_sealed_on_its_first_change(ctx, clock):
    sid = ctx.repo.save(Session(name="Notes only")).id
    sealer = new_sealer(ctx, clock)
    ctx.repo.update_fields(sid, notes="just notes\n")
    tick(sealer)
    clock.t += QUIET + 1
    assert tick(sealer) == ["changed"]  # (no earlier seal to compare with)
    assert records.verify(ctx, sid).ok


def test_a_format_one_chain_is_continued_in_format_two(ctx, clock):
    sid = meeting(ctx)
    ctx.repo.add_seal(sid, "old seal", content="abc", audio={})
    sealer = new_sealer(ctx, clock)
    ctx.repo.update_fields(sid, notes="x\n")
    tick(sealer)
    clock.t += QUIET + 1
    assert tick(sealer) == ["changed"]
    seals = ctx.repo.seals(sid)
    assert [s["format"] for s in seals] == [1, 2] and seals[1]["prev"] == seals[0]["chain"]


def test_audio_that_changed_is_sealed_as_audio(ctx, clock, tmp_path):
    sid = meeting(ctx)
    audio = tmp_path / "a.ogg"
    audio.write_bytes(b"OggS" + b"\x01" * 400)
    ctx.sessions.set_audio(sid, str(audio), [])
    records.seal(ctx, sid, "baseline")
    sealer = new_sealer(ctx, clock)
    other = tmp_path / "b.ogg"
    other.write_bytes(b"OggS" + b"\x02" * 400)
    ctx.repo.add_recordings(
        sid, [Recording(source="mic", device_id=1, device_name="Mic", path=str(other))]
    )
    tick(sealer)
    clock.t += QUIET + 1
    assert tick(sealer) == ["changed: audio"]
    check = records.verify(ctx, sid)
    assert check.ok and not check.pending


def test_an_audio_file_that_cannot_be_read_is_retried_with_a_backoff(ctx, clock, monkeypatch):
    sid = meeting(ctx)
    records.seal(ctx, sid, "baseline")
    sealer = new_sealer(ctx, clock)
    ctx.repo.update_fields(sid, notes="x\n")
    monkeypatch.setattr(records, "seal", lambda app, s, reason: None)  # (cannot hash the audio)
    tick(sealer)
    clock.t += QUIET + 1
    assert tick(sealer) == []
    assert sealer._failures[sid][0] == 1
    clock.t += 30
    assert tick(sealer) == []  # not yet: the first backoff is a minute
    assert sealer._failures[sid][0] == 1
    monkeypatch.undo()
    clock.t += 120
    assert tick(sealer) == ["changed: notes"]
    assert sid not in sealer._failures


def test_a_locked_library_is_left_alone(ctx, clock):
    sid = meeting(ctx)
    records.seal(ctx, sid, "baseline")
    sealer = new_sealer(ctx, clock)
    ctx.repo.update_fields(sid, notes="x\n")
    ctx.locked = True
    clock.t += QUIET + 1
    assert tick(sealer) == []
    ctx.locked = False
    assert tick(sealer) == []  # (the first look after: quiet time starts now)
    clock.t += QUIET + 1
    assert tick(sealer) == ["changed: notes"]


def test_sealing_is_not_a_change(ctx, clock):
    """The sealer's own seal must not wake it up again."""
    sid = meeting(ctx)
    records.seal(ctx, sid, "baseline")
    sealer = new_sealer(ctx, clock)
    ctx.repo.update_fields(sid, notes="x\n")
    tick(sealer)
    clock.t += QUIET + 1
    assert tick(sealer) == ["changed: notes"]
    seq = ctx.repo.sync_seq_of(sid)
    for _ in range(3):
        clock.t += QUIET + 1
        assert tick(sealer) == []
    assert ctx.repo.sync_seq_of(sid) == seq and len(ctx.repo.seals(sid)) == 2


def test_the_sealer_is_wired_into_the_app(client, ctx):
    from mnemosyne.services.record_sealer import RecordSealer as Wired

    assert isinstance(ctx.record_sealer, Wired)
    assert ctx.record_sealer.interval == 0 and ctx.record_sealer._task is None
    assert ctx.repo.get_meta(CURSOR_KEY) is not None  # started with the app
