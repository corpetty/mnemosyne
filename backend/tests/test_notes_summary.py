"""The summary at the end of the live notes (services/notes_summary.py): a Summary section that
follows summary_data block by block, replaces the copilot's lines at the summarize job and never
touches the person's lines. The sidecar is the `notes doc` stand-in of tests/fake_link.py (toy
frames, see its docstring)."""

import asyncio
import base64
import json
import time
from datetime import date

import pytest

from mnemosyne.models.session import ActionItem, CopilotItem, CopilotNotes, Session, SummaryData
from mnemosyne.services import notes_summary, shared_notes
from mnemosyne.services.copilot_notes import CHIP, add_new
from mnemosyne.services.notes_summary import render, sync
from tests import fake_link
from tests.conftest import run_summarize
from tests.fake_link import doc_frame

D1, D2, D3, A1, Q1 = "d1aaaaaa", "d2aaaaaa", "d3aaaaaa", "a1aaaaaa", "q1aaaaaa"


@pytest.fixture
def sidecar(tmp_path, monkeypatch):
    return fake_link.install(tmp_path, monkeypatch)


def frame(frame_id: str, *ops) -> bytes:
    return base64.b64decode(doc_frame(frame_id, *ops))


def type_text(ctx, sid, frame_id: str, text: str) -> None:
    """Ann makes the notes say `text` (a toy `set`: lines keep their refs by matching)."""
    ctx.repo.append_notes_ops(sid, [frame(frame_id, ["set", text])], "u1", "Ann", True)
    shared_notes.fold_blocking(ctx, sid)


def refs(ctx, sid) -> list[tuple[str, int]]:
    return [(r["ref"], r["md_line"]) for r in ctx.repo.notes_refs(sid) or []]


def summary_ops(ctx, sid) -> int:
    return sum(o["author"] == shared_notes.SUMMARY_AUTHOR for o in ctx.repo.notes_ops(sid))


def data(**changes) -> SummaryData:
    d = SummaryData(
        decisions=["Ship it in October", "Hire a designer"],
        decision_ids=[D1, D2],
        action_items=[
            ActionItem(id=A1, text="Update the docs", owner="Ana", due=date(2026, 11, 10))
        ],
        open_questions=["Who pays?"],
        question_ids=[Q1],
    )
    return d.model_copy(update=changes)


SECTION = (
    "## Summary\n"
    "We met about the launch.\n"
    "### Decisions\n"
    "- Ship it in October\n"
    "- Hire a designer\n"
    "### Action items\n"
    "- [ ] Update the docs (Ana) 📅 2026-11-10\n"
    "### Open questions\n"
    "- Who pays?\n"
)
BODY = "We met about the launch."


def summarized(ctx, sid, body: str = BODY, **changes) -> None:
    ctx.repo.update_fields(sid, summary=body, summary_data=data(**changes))


def meeting(ctx, notes: str = "Mine\n") -> str:
    return ctx.repo.save(Session(name="m", notes=notes)).id


# ---- render (pure) -------------------------------------------------------------------------


def test_render_layout():
    lines = render(data(), "\n" + BODY + "\n\n")
    assert [(x.md, x.ref) for x in lines] == [
        ("## Summary", "sec:summary"),
        (BODY, None),
        ("### Decisions", "sec:decisions"),
        ("- Ship it in October", f"sum:{D1}"),
        ("- Hire a designer", f"sum:{D2}"),
        ("### Action items", "sec:action_items"),
        ("- [ ] Update the docs (Ana) 📅 2026-11-10", f"sum:{A1}"),
        ("### Open questions", "sec:open_questions"),
        ("- Who pays?", f"sum:{Q1}"),
    ]
    # Done, no owner or date; empty sub-sections left out; whitespace collapsed.
    done = data(
        decisions=[],
        decision_ids=[],
        open_questions=[],
        question_ids=[],
        action_items=[ActionItem(id=A1, text="Book  the\nvenue", done=True)],
    )
    assert [(x.md, x.ref) for x in render(done, "")] == [
        ("## Summary", "sec:summary"),
        ("### Action items", "sec:action_items"),
        ("- [x] Book the venue", f"sum:{A1}"),
    ]


# ---- the summarize job ---------------------------------------------------------------------


def test_the_first_summary_replaces_the_copilot_lines(ctx, sidecar):
    sid = meeting(ctx)
    notes = CopilotNotes(
        session_id=sid,
        decisions=["Ship it in October"],
        action_items=[CopilotItem(text="Update the docs", owner="Ana")],
        open_questions=["Who pays?"],
    )
    asyncio.run(add_new(ctx, sid, notes))
    ctx.repo.update_fields(sid, copilot_notes=notes)
    text = ctx.repo.get(sid).notes
    assert text == (
        "Mine\n"
        "## Copilot\n"
        f"- {CHIP} Decided: Ship it in October\n"
        f"- [ ] {CHIP} Update the docs (Ana)\n"
        f"- {CHIP} Question: Who pays?\n"
    )
    # Ann rewords the question; the other two lines (whitespace aside) are the copilot's.
    edited = text.replace("Question: Who pays?", "Question: Who pays for the venue?")
    edited = edited.replace("Update the docs (Ana)", "Update  the docs (Ana)")
    type_text(ctx, sid, "ann1", edited)

    summarized(ctx, sid)
    assert sync(ctx, sid, create=True)
    assert ctx.repo.get(sid).notes == (
        f"Mine\n- {CHIP} Question: Who pays for the venue?\n" + SECTION
    )
    assert refs(ctx, sid) == [
        ("sec:summary", 2),
        ("sec:decisions", 4),
        (f"sum:{D1}", 5),
        (f"sum:{D2}", 6),
        ("sec:action_items", 7),
        (f"sum:{A1}", 8),
        ("sec:open_questions", 9),
        (f"sum:{Q1}", 10),
    ]
    assert ctx.repo.notes_doc(sid)["last_by"] == "Summary"
    assert [e["kind"] for e in ctx.repo.events(sid)].count("notes_summary_written") == 1
    # What was written, for the read-back.
    written = ctx.repo.get_summary_doc(sid)["notes"]["written"]
    assert written["body"] == BODY and written["body_shown"] == BODY
    assert written["blocks"][A1] == {
        "section": "action_items",
        "text": "Update the docs",
        "owner": "Ana",
        "due": "2026-11-10",
        "box": True,
        "done": False,
        "exact": True,
        "md": "- [ ] Update the docs (Ana) 📅 2026-11-10",
        "shown": "- [ ] Update the docs (Ana) 📅 2026-11-10",
    }
    assert set(written["blocks"]) == {D1, D2, A1, Q1}
    # The person's line is a person's line: not a task of anyone, nothing below it moved.
    assert ctx.repo.get(sid).notes_items == []


def test_copilot_lines_of_a_later_part_after_the_summary(ctx, sidecar):
    sid = meeting(ctx)
    summarized(ctx, sid)
    sync(ctx, sid, create=True)
    # A second part: the copilot adds a heading and lines after the Summary.
    notes = CopilotNotes(session_id=sid, decisions=["Book the hall"], open_questions=["When?"])
    asyncio.run(add_new(ctx, sid, notes))
    ctx.repo.update_fields(sid, copilot_notes=notes)
    text = ctx.repo.get(sid).notes
    assert text == "Mine\n" + SECTION + (
        f"## Copilot\n- {CHIP} Decided: Book the hall\n- {CHIP} Question: When?\n"
    )
    edited = text.replace("Question: When?", "Question: When, Friday?")
    type_text(ctx, sid, "ann1", edited)
    # Re-summarized: the unedited line goes, the edited one keeps the heading above it, so it
    # does not run into Open questions.
    summarized(ctx, sid, decisions=["Ship it in October", "Hire a designer", "Book the hall"])
    assert sync(ctx, sid, create=True)
    got = ctx.repo.get(sid).notes
    assert (
        got
        == "Mine\n"
        + SECTION.replace("- Hire a designer\n", "- Hire a designer\n- Book the hall\n")
        + f"## Copilot\n- {CHIP} Question: When, Friday?\n"
    )
    assert [r for r, _ in refs(ctx, sid)][-2:] == [f"sum:{Q1}", "sec:copilot"]
    # Nothing left under it at the next summarize: then it goes.
    type_text(ctx, sid, "ann2", got.replace(f"- {CHIP} Question: When, Friday?\n", ""))
    summarized(ctx, sid, body="We met again.")
    assert sync(ctx, sid, create=True)
    assert ctx.repo.get(sid).notes.endswith("- Who pays?\n")


def test_without_copilot_lines_the_section_goes_at_the_end(ctx, sidecar):
    sid = meeting(ctx, "Mine\n\n- a point\n")
    summarized(ctx, sid)
    assert sync(ctx, sid, create=True)
    assert ctx.repo.get(sid).notes == "Mine\n\n- a point\n" + SECTION


def test_a_resummarize_changes_one_line_and_keeps_the_persons_line(ctx, sidecar):
    sid = meeting(ctx)
    summarized(ctx, sid)
    sync(ctx, sid, create=True)
    text = ctx.repo.get(sid).notes
    with_mine = text.replace("- Ship it in October\n", "- Ship it in October\n  (Ann: maybe Nov)\n")
    type_text(ctx, sid, "ann1", with_mine)
    ops = summary_ops(ctx, sid)

    summarized(ctx, sid, decisions=["Ship it in October", "Hire two designers"])
    assert sync(ctx, sid, create=True)
    assert ctx.repo.get(sid).notes == with_mine.replace(
        "- Hire a designer\n", "- Hire two designers\n"
    )
    assert summary_ops(ctx, sid) == ops + 1
    assert (f"sum:{D2}", 6) in refs(ctx, sid)


def test_new_and_gone_blocks(ctx, sidecar):
    sid = meeting(ctx, "")
    summarized(ctx, sid)
    sync(ctx, sid, create=True)
    text = ctx.repo.get(sid).notes
    type_text(ctx, sid, "ann1", text.replace("- Hire a designer\n", "- Hire a designer\nmine\n"))

    # A new decision after the first, the action item gone (and its heading with it).
    summarized(
        ctx,
        sid,
        decisions=["Ship it in October", "Rent a hall", "Hire a designer"],
        decision_ids=[D1, D3, D2],
        action_items=[],
    )
    assert sync(ctx, sid, create=False)
    assert ctx.repo.get(sid).notes == (
        "## Summary\n"
        "We met about the launch.\n"
        "### Decisions\n"
        "- Ship it in October\n"
        "- Rent a hall\n"
        "- Hire a designer\n"
        "mine\n"
        "### Open questions\n"
        "- Who pays?\n"
    )
    # A new block with no predecessor goes at the end of its sub-section.
    summarized(
        ctx,
        sid,
        action_items=[],
        decisions=["Ship it in October", "Rent a hall", "Hire a designer"],
        decision_ids=[D1, D3, D2],
        open_questions=["When?", "Who pays?"],
        question_ids=["q0aaaaaa", Q1],
    )
    assert sync(ctx, sid, create=False)
    assert ctx.repo.get(sid).notes.endswith("### Open questions\n- Who pays?\n- When?\n")


def test_the_body_follows_the_summary(ctx, sidecar):
    sid = meeting(ctx)
    summarized(ctx, sid)
    sync(ctx, sid, create=True)
    summarized(ctx, sid, body="We met.\n\n- The launch is on.")
    assert sync(ctx, sid, create=False)
    assert ctx.repo.get(sid).notes == "Mine\n" + SECTION.replace(
        BODY, "We met.\n\n- The launch is on."
    )


# ---- ticks and other writes of the summary -------------------------------------------------


def _until(check, seconds: float = 5.0) -> None:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if check():
            return
        time.sleep(0.02)
    raise AssertionError("timed out")


def test_a_tick_from_the_tasks_route_shows_in_the_notes(client, ctx, sidecar):
    ctx.summary_notes_delay = 0.05
    sid = meeting(ctx)
    summarized(ctx, sid)
    sync(ctx, sid, create=True)
    r = client.patch(f"/api/sessions/{sid}/action-items/0", json={"done": True})
    assert r.status_code == 200, r.text
    _until(lambda: "- [x] Update the docs (Ana)" in ctx.repo.get(sid).notes)
    assert ctx.repo.get(sid).notes == "Mine\n" + SECTION.replace("- [ ]", "- [x]")
    # The other way back.
    client.patch(f"/api/sessions/{sid}/action-items/0", json={"done": False})
    _until(lambda: "- [ ] Update the docs (Ana)" in ctx.repo.get(sid).notes)


def test_a_deleted_heading_comes_back_only_at_a_summarize(ctx, sidecar):
    sid = meeting(ctx)
    summarized(ctx, sid)
    sync(ctx, sid, create=True)
    without = ctx.repo.get(sid).notes.replace("### Decisions\n", "")
    type_text(ctx, sid, "ann1", without)

    summarized(ctx, sid, decisions=["Ship it in November", "Hire a designer"])
    assert not sync(ctx, sid, create=False)
    assert ctx.repo.get(sid).notes == without

    assert sync(ctx, sid, create=True)
    assert ctx.repo.get(sid).notes == "Mine\n" + SECTION.replace("October", "November")


def test_a_deleted_line_is_not_written_again_by_an_edit(ctx, sidecar):
    sid = meeting(ctx)
    summarized(ctx, sid)
    sync(ctx, sid, create=True)
    without = ctx.repo.get(sid).notes.replace("- Hire a designer\n", "")
    type_text(ctx, sid, "ann1", without)
    ctx.repo.set_action_item_done(sid, 0, True)  # the summary changes elsewhere
    assert sync(ctx, sid, create=False)
    assert ctx.repo.get(sid).notes == without.replace("- [ ]", "- [x]")


def test_a_line_edited_in_the_notes_is_left_to_the_read_back(ctx, sidecar):
    sid = meeting(ctx)
    summarized(ctx, sid)
    sync(ctx, sid, create=True)
    edited = ctx.repo.get(sid).notes.replace("- Who pays?", "- Who pays for the hall?")
    type_text(ctx, sid, "ann1", edited)
    ctx.repo.set_action_item_done(sid, 0, True)
    sync(ctx, sid, create=False)
    assert ctx.repo.get(sid).notes == edited.replace("- [ ]", "- [x]")


def test_nothing_is_written_when_nothing_differs(ctx, sidecar):
    sid = meeting(ctx)
    summarized(ctx, sid)
    assert sync(ctx, sid, create=True)
    runs, merges, ops = len(sidecar.doc_requests), len(sidecar.requests), summary_ops(ctx, sid)
    assert not sync(ctx, sid, create=False)
    assert not sync(ctx, sid, create=True)
    ctx.repo.set_action_item_done(sid, 0, False)  # what it was
    assert not sync(ctx, sid, create=False)
    assert len(sidecar.doc_requests) == runs  # not even a fold
    assert len(sidecar.requests) == merges
    assert summary_ops(ctx, sid) == ops


# ---- when nothing is written ---------------------------------------------------------------


@pytest.mark.parametrize(
    ("column", "value"), [("origin", "otherdevice1"), ("shared_from", "ab" * 32)]
)
def test_nothing_for_a_meeting_of_another_computer_or_person(ctx, sidecar, column, value):
    sid = meeting(ctx, "Theirs\n")
    summarized(ctx, sid)
    with ctx.repo._lock, ctx.repo._conn:
        ctx.repo._conn.execute(f"UPDATE sessions SET {column}=? WHERE id=?", (value, sid))
    assert not sync(ctx, sid, create=True)
    assert ctx.repo.notes_ops(sid) == [] and sidecar.doc_requests == []
    assert ctx.repo.get(sid).notes == "Theirs\n"


def test_nothing_with_the_setting_off_or_without_a_summary(ctx, sidecar):
    sid = meeting(ctx)
    assert not sync(ctx, sid, create=True)  # no summary yet
    summarized(ctx, sid)
    ctx.settings.summary_in_notes = False
    assert not sync(ctx, sid, create=True)
    assert ctx.repo.notes_ops(sid) == [] and sidecar.doc_requests == []
    assert ctx.repo.get(sid).notes == "Mine\n"


def test_nothing_without_a_sidecar(ctx, tmp_path, monkeypatch):
    monkeypatch.setenv("MNEMOSYNE_LINK_BIN", str(tmp_path / "gone"))
    sid = meeting(ctx)
    summarized(ctx, sid)
    assert not sync(ctx, sid, create=True)
    assert ctx.repo.get(sid).notes == "Mine\n"


def test_a_failing_sidecar_does_not_raise(ctx, sidecar, monkeypatch):
    sid = meeting(ctx)
    summarized(ctx, sid)
    monkeypatch.setenv("FAKE_NOTES_DOC_FAIL", "1")
    assert not sync(ctx, sid, create=True)
    assert ctx.repo.get(sid).notes == "Mine\n"


# ---- the repository hook and the debounce --------------------------------------------------


def test_every_write_of_the_summary_calls_the_hook(ctx):
    calls: list[str] = []
    ctx.repo.after_summary = calls.append
    sid = meeting(ctx)
    assert calls == []  # no summary
    ctx.repo.update_fields(sid, name="renamed", notes="x\n")
    assert calls == []

    def wrote(step) -> bool:
        calls.clear()
        step()
        return calls == [sid]

    assert wrote(lambda: summarized(ctx, sid))
    assert wrote(lambda: ctx.repo.update_fields(sid, summary="other"))
    assert wrote(lambda: ctx.repo.set_action_item_done(sid, 0, True))
    assert wrote(lambda: ctx.repo.set_action_item_follow(sid, 0, True))
    assert wrote(lambda: ctx.repo.set_action_item_fields_by_id(sid, A1, done=False))
    assert not wrote(lambda: ctx.repo.set_action_item_fields_by_id(sid, A1, done=False))  # same
    assert wrote(lambda: ctx.repo.fan_out_action_item_fields(sid, A1, done=True))
    assert wrote(lambda: ctx.repo.set_decision_links(sid, {D1: "abcabcabcabc"}))
    assert wrote(lambda: ctx.repo.relabel_session_speaker(sid, "Ana", "Ana B"))
    session = ctx.repo.get(sid)
    assert not wrote(lambda: ctx.repo.save(session))  # the same summary
    session.summary = "changed"
    assert wrote(lambda: ctx.repo.save(session))


def test_writes_are_debounced_into_one_sync(client, ctx, monkeypatch):
    seen: list[tuple[str, bool]] = []
    monkeypatch.setattr(
        notes_summary, "sync", lambda app, sid, *, create: seen.append((sid, create))
    )
    ctx.summary_notes_delay = 0.2
    sid = meeting(ctx)
    for body in ("one", "two", "three"):
        ctx.repo.update_fields(sid, summary=body)
    _until(lambda: seen)
    time.sleep(0.4)
    assert seen == [(sid, False)]
    ctx.settings.summary_in_notes = False
    ctx.repo.update_fields(sid, summary="four")
    time.sleep(0.4)
    assert seen == [(sid, False)]


def test_the_summarize_job_writes_the_section_once(
    client, ctx, fake_provider, transcribed_session, sidecar, monkeypatch
):
    real = notes_summary.sync
    seen: list[bool] = []

    def counting(app, sid, *, create):
        seen.append(create)
        return real(app, sid, create=create)

    monkeypatch.setattr(notes_summary, "sync", counting)
    ctx.summary_notes_delay = 0.1
    sid = transcribed_session["id"]
    ctx.repo.update_fields(sid, notes="Mine\n")
    fake_provider.summary = json.dumps(
        {
            "summary": "We met.",
            "decisions": ["Ship it in October"],
            "action_items": [{"text": "Update the docs", "owner": "Ana"}],
            "open_questions": [],
        }
    )
    assert run_summarize(client, sid, {"provider": "fake"})["status"] == "completed"
    notes = ctx.repo.get(sid).notes
    assert notes == (
        "Mine\n"
        "## Summary\n"
        "We met.\n"
        "### Decisions\n"
        "- Ship it in October\n"
        "### Action items\n"
        "- [ ] Update the docs (Ana)\n"
    )
    time.sleep(0.4)
    assert seen == [True]  # the job's own writes did not schedule another
