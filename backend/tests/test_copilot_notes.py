"""The copilot's points in the live notes (services/copilot_notes.py): each new item added once
under a Copilot heading, as the copilot, never rewritten or added again. The sidecar is the
`notes doc` stand-in of tests/fake_link.py (toy frames, see its docstring)."""

import asyncio
import base64
import json
import types

import pytest

from mnemosyne.models.session import CopilotAdded, CopilotItem, CopilotNotes, Session
from mnemosyne.models.transcript import TranscriptSegment
from mnemosyne.services import copilot, copilot_notes, shared_notes
from mnemosyne.services.copilot import MIN_NEW_CHARS, copilot_runner
from mnemosyne.services.copilot_notes import CHIP, add_new, append, line_md, new_items
from tests import fake_link
from tests.fake_link import doc_frame


@pytest.fixture
def sidecar(tmp_path, monkeypatch):
    return fake_link.install(tmp_path, monkeypatch)


def frame(frame_id: str, *ops) -> bytes:
    return base64.b64decode(doc_frame(frame_id, *ops))


def notes_of(sid, summary=(), decisions=(), todos=(), questions=()) -> CopilotNotes:
    return CopilotNotes(
        session_id=sid,
        summary=list(summary),
        decisions=list(decisions),
        action_items=[CopilotItem(text=t, owner=o) for t, o in todos],
        open_questions=list(questions),
    )


def refs(ctx, sid) -> list[tuple[str, int]]:
    return [(r["ref"], r["md_line"]) for r in ctx.repo.notes_refs(sid) or []]


def type_text(ctx, sid, frame_id: str, text: str) -> None:
    """Someone makes the notes say `text` (a toy `set`: lines keep their refs by matching)."""
    ctx.repo.append_notes_ops(sid, [frame(frame_id, ["set", text])], "u1", "Ann", True)
    shared_notes.fold_blocking(ctx, sid)


def copilot_ops(ctx, sid) -> int:
    return sum(o["author"] == shared_notes.COPILOT_AUTHOR for o in ctx.repo.notes_ops(sid))


A = f"- {CHIP} "


# ---- pure ----------------------------------------------------------------------------------


def test_new_items_dedups_by_kind_and_numbers_on():
    added = [
        CopilotAdded(kind="decision", text="Ship the migration in October", n=0),
        CopilotAdded(kind="action", text="Update the docs", owner="Ana", n=4),
    ]
    notes = notes_of(
        "s",
        summary=["Ship the migration in October", "  talked   about   the  plan "],
        decisions=["Ship the migration in October.", "Hire a designer"],
        todos=[("update the docs", "Bob"), ("Book the venue", None)],
        questions=["Who pays?", "Who pays ?"],
    )
    got = new_items(notes, added)
    assert [(i.kind, i.text, i.owner, i.n) for i in got] == [
        ("summary", "Ship the migration in October", None, 5),  # another kind: new
        ("summary", "talked about the plan", None, 6),  # whitespace collapsed
        ("decision", "Hire a designer", None, 7),
        ("action", "Book the venue", None, 8),
        ("question", "Who pays?", None, 9),  # the second one is the same
    ]
    assert new_items(None, added) == []
    assert new_items(notes, [*added, *got]) == []


def test_line_md_shapes():
    def item(kind, text, owner=None):
        return CopilotAdded(kind=kind, text=text, owner=owner, n=0)

    assert line_md(item("summary", "We talked")) == f"- {CHIP} We talked"
    assert line_md(item("decision", "Ship it")) == f"- {CHIP} Decided: Ship it"
    assert line_md(item("question", "Who pays?")) == f"- {CHIP} Question: Who pays?"
    assert line_md(item("action", "Book it", "Ana")) == f"- [ ] {CHIP} Book it (Ana)"
    assert line_md(item("action", "Book it")) == f"- [ ] {CHIP} Book it"
    # Markup in the text is escaped: the line says it.
    assert line_md(item("summary", "Use *nix [docs] a_b")) == (
        f"- {CHIP} Use \\*nix \\[docs\\] a\\_b"
    )


# ---- the runner ----------------------------------------------------------------------------


class FakeLive:
    def __init__(self):
        self.committed: list[TranscriptSegment] = []


def _seg(i, text):
    return TranscriptSegment(text=text, speaker="SPEAKER_00", start=i * 10.0, end=i * 10.0 + 5)


async def _until(check, rounds: int = 300):
    for _ in range(rounds):
        if check():
            return
        await asyncio.sleep(0.01)
    raise AssertionError("timed out")


@pytest.mark.anyio
async def test_runner_adds_each_point_once(ctx, fake_provider, sidecar, monkeypatch):
    from mnemosyne.jobs import JobContext

    clock = [1000.0]  # the runner's own clock: the interval passes when the test says so
    monkeypatch.setattr(copilot, "time", types.SimpleNamespace(monotonic=lambda: clock[0]))
    sid = ctx.repo.save(Session(name="live", notes="Mine\n")).id
    ctx.settings.default_provider = "fake"
    first = {
        "summary": ["We talked about the migration"],
        "decisions": ["Ship the migration in October"],
        "action_items": [{"text": "Update the docs", "owner": "Ana"}],
        "open_questions": ["Who pays for the venue?"],
    }
    fake_provider.reply = json.dumps(first)
    live = FakeLive()
    ctx.live[sid] = live
    events = []
    job_ctx = JobContext.__new__(JobContext)
    job_ctx.update = lambda *a, **k: None
    job_ctx.emit = events.append
    task = asyncio.create_task(copilot_runner(ctx, sid, tick=0.01)(job_ctx))
    live.committed.append(_seg(0, "y" * MIN_NEW_CHARS))
    await _until(lambda: len(events) == 1)

    assert ctx.repo.get(sid).notes == (
        "Mine\n"
        "## Copilot\n"
        f"{A}We talked about the migration\n"
        f"{A}Decided: Ship the migration in October\n"
        f"- [ ] {CHIP} Update the docs (Ana)\n"
        f"{A}Question: Who pays for the venue?\n"
    )
    assert refs(ctx, sid) == [
        ("sec:copilot", 1),
        ("cp:0", 2),
        ("cp:1", 3),
        ("cp:2", 4),
        ("cp:3", 5),
    ]
    assert ctx.repo.notes_doc(sid)["last_by"] == "Copilot"
    stored = ctx.repo.get(sid).copilot_notes
    assert [(a.kind, a.n) for a in stored.added] == [
        ("summary", 0),
        ("decision", 1),
        ("action", 2),
        ("question", 3),
    ]
    assert events[0]["notes"]["added"][2] == {
        "kind": "action",
        "text": "Update the docs",
        "owner": "Ana",
        "n": 2,
    }
    assert [e["detail"] for e in ctx.repo.events(sid) if e["kind"] == "copilot_notes_added"] == [
        {"count": 4}
    ]

    # Ann types a line of her own under the section.
    text = ctx.repo.get(sid).notes
    type_text(ctx, sid, "ann1", text + "my own line\n")

    # The next notes reword the decision and the point: nothing is added.
    reworded = dict(first, decisions=["Ship the migration in October."])
    reworded["summary"] = ["We talked about the migration."]
    fake_provider.reply = json.dumps(reworded)
    live.committed.append(_seg(1, "more"))
    clock[0] += 1000
    await _until(lambda: len(events) == 2)
    assert copilot_ops(ctx, sid) == 1
    assert ctx.repo.get(sid).notes == text + "my own line\n"

    # A new question goes after the last copilot line, before Ann's.
    fake_provider.reply = json.dumps(
        dict(reworded, open_questions=["Who pays for the venue?", "Is it Friday?"])
    )
    live.committed.append(_seg(2, "more"))
    clock[0] += 1000
    await _until(lambda: len(events) == 3)
    assert ctx.repo.get(sid).notes == text + f"{A}Question: Is it Friday?\nmy own line\n"
    assert refs(ctx, sid)[-1] == ("cp:4", 6)
    assert [a.n for a in ctx.repo.get(sid).copilot_notes.added] == [0, 1, 2, 3, 4]
    task.cancel()
    assert (await task)["updates"] == 3


# ---- what is never added again -------------------------------------------------------------


def _start(ctx, sid) -> CopilotNotes:
    notes = notes_of(
        sid,
        decisions=["Ship the migration in October"],
        todos=[("Update the docs", "Ana")],
        questions=["Who pays?"],
    )
    asyncio.run(add_new(ctx, sid, notes))
    assert len(notes.added) == 3
    return notes


def test_a_deleted_line_is_not_added_again_and_an_edited_one_is_left_alone(ctx, sidecar):
    sid = ctx.repo.save(Session(name="m")).id
    notes = _start(ctx, sid)
    before = ctx.repo.get(sid).notes
    assert before == (
        "## Copilot\n"
        f"{A}Decided: Ship the migration in October\n"
        f"- [ ] {CHIP} Update the docs (Ana)\n"
        f"{A}Question: Who pays?\n"
    )
    # Ann deletes the decision, then rewrites the task in her words.
    type_text(ctx, sid, "ann1", before.replace(f"{A}Decided: Ship the migration in October\n", ""))
    edited = f"## Copilot\n- [ ] {CHIP} Ana updates the README by Friday\n{A}Question: Who pays?\n"
    type_text(ctx, sid, "ann2", edited)
    assert refs(ctx, sid) == [("sec:copilot", 0), ("cp:1", 1), ("cp:2", 2)]

    later = notes_of(
        sid,
        decisions=["Ship the migration in October", "Hire a designer"],
        todos=[("Update the docs", "Ana")],
        questions=["Who pays?"],
    )
    later.added = notes.added
    written = asyncio.run(add_new(ctx, sid, later))
    assert [(w.kind, w.text, w.n) for w in written] == [("decision", "Hire a designer", 3)]
    assert ctx.repo.get(sid).notes == edited + f"{A}Decided: Hire a designer\n"
    assert refs(ctx, sid)[-1] == ("cp:3", 3)
    assert len(later.added) == 4
    # Nothing new: no write at all.
    n = copilot_ops(ctx, sid)
    assert asyncio.run(add_new(ctx, sid, later)) == []
    assert copilot_ops(ctx, sid) == n


def test_the_heading_comes_back_at_the_end_when_there_is_something_new(ctx, sidecar):
    sid = ctx.repo.save(Session(name="m", notes="Mine\n")).id
    notes = _start(ctx, sid)
    type_text(ctx, sid, "ann1", "Mine\n")  # the section deleted
    type_text(ctx, sid, "ann2", "Mine\nthe rest is gone\n")
    notes.decisions.append("Hire a designer")
    asyncio.run(add_new(ctx, sid, notes))
    assert ctx.repo.get(sid).notes == (
        f"Mine\nthe rest is gone\n## Copilot\n{A}Decided: Hire a designer\n"
    )
    assert refs(ctx, sid) == [("sec:copilot", 2), ("cp:3", 3)]


def test_no_copilot_line_left_under_the_heading_goes_right_after_it(ctx, sidecar):
    sid = ctx.repo.save(Session(name="m")).id
    notes = _start(ctx, sid)
    type_text(ctx, sid, "ann1", "## Copilot\n")  # the copilot's lines deleted
    type_text(ctx, sid, "ann2", "## Copilot\nmine\n## Next\n")
    notes.open_questions.append("Is it Friday?")
    asyncio.run(add_new(ctx, sid, notes))
    assert ctx.repo.get(sid).notes == f"## Copilot\n{A}Question: Is it Friday?\nmine\n## Next\n"


# ---- when nothing is written ---------------------------------------------------------------


def _item() -> list[CopilotAdded]:
    return [CopilotAdded(kind="decision", text="Ship it", n=0)]


def test_nothing_with_the_setting_off(ctx, sidecar):
    sid = ctx.repo.save(Session(name="m", notes="Mine\n")).id
    ctx.settings.copilot_to_notes = False
    notes = notes_of(sid, decisions=["Ship it"])
    assert asyncio.run(add_new(ctx, sid, notes)) == [] and notes.added == []
    assert ctx.repo.notes_ops(sid) == [] and sidecar.doc_requests == []


@pytest.mark.parametrize(
    ("column", "value"), [("origin", "otherdevice1"), ("shared_from", "ab" * 32)]
)
def test_nothing_for_a_meeting_of_another_computer_or_person(ctx, sidecar, column, value):
    sid = ctx.repo.save(Session(name="m", notes="Theirs\n")).id
    with ctx.repo._lock, ctx.repo._conn:
        ctx.repo._conn.execute(f"UPDATE sessions SET {column}=? WHERE id=?", (value, sid))
    assert append(ctx, sid, _item()) == []
    assert ctx.repo.notes_ops(sid) == [] and sidecar.doc_requests == []
    assert ctx.repo.get(sid).notes == "Theirs\n"


def test_nothing_without_a_sidecar(ctx, tmp_path, monkeypatch):
    monkeypatch.setenv("MNEMOSYNE_LINK_BIN", str(tmp_path / "gone"))
    sid = ctx.repo.save(Session(name="m", notes="Mine\n")).id
    notes = notes_of(sid, decisions=["Ship it"])
    assert asyncio.run(add_new(ctx, sid, notes)) == [] and notes.added == []
    assert ctx.repo.get(sid).notes == "Mine\n"


def test_a_failing_sidecar_adds_nothing_and_does_not_raise(ctx, sidecar, monkeypatch):
    sid = ctx.repo.save(Session(name="m", notes="Mine\n")).id
    assert shared_notes.available()
    monkeypatch.setenv("FAKE_NOTES_DOC_FAIL", "1")
    assert append(ctx, sid, _item()) == []
    assert not any(e["kind"] == "copilot_notes_added" for e in ctx.repo.events(sid))
    assert copilot_notes.new_items(notes_of(sid, decisions=["Ship it"]), []) == _item()
