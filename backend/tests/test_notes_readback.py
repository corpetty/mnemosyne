"""The notes' Summary section read back into the summary (services/notes_summary.py `read_back`,
the core shared with Obsidian in services/summary_readback.py): one summary, two views. Edits in
the section change the summary as Obsidian edits do, a re-summarize merges with them, a box
ticked there ticks the task. The sidecar is the `notes doc` stand-in of tests/fake_link.py."""

import base64
import threading
from datetime import date, datetime

import pytest

from mnemosyne.api.app import create_app
from mnemosyne.models.session import ActionItem, Session, SummaryData
from mnemosyne.services import notes_summary, shared_notes, summary_doc, task_links
from mnemosyne.services.notes_summary import sync
from tests import fake_link
from tests.fake_link import doc_frame

D1, D2, A1, Q1 = "d1aaaaaa", "d2aaaaaa", "a1aaaaaa", "q1aaaaaa"
BODY = "We met about the launch."
SECTION = (
    "## Summary\n"
    "We met about the launch.\n"
    "### Decisions\n"
    "- Ship it in October\n"
    "- Hire a designer\n"
    "### Action items\n"
    "- [ ] Update the docs (Ada) 📅 2026-11-10\n"
    "### Open questions\n"
    "- Who pays?\n"
)


@pytest.fixture
def sidecar(tmp_path, monkeypatch):
    assert notes_summary.read_back in shared_notes.AFTER_FOLD  # registered on import
    return fake_link.install(tmp_path, monkeypatch)


def data(**changes) -> SummaryData:
    d = SummaryData(
        decisions=["Ship it in October", "Hire a designer"],
        decision_ids=[D1, D2],
        action_items=[
            ActionItem(id=A1, text="Update the docs", owner="Ada", due=date(2026, 11, 10))
        ],
        open_questions=["Who pays?"],
        question_ids=[Q1],
    )
    return d.model_copy(update=changes)


def written_meeting(ctx, notes: str = "Mine\n") -> str:
    """A summarized meeting whose notes end in the Summary section, as the summarize job left
    them."""
    sid = ctx.repo.save(Session(name="m", notes=notes)).id
    ctx.repo.update_fields(sid, summary=BODY, summary_data=data())
    assert sync(ctx, sid, create=True)
    assert ctx.repo.get(sid).notes == notes + SECTION
    return sid


def frame(frame_id: str, *ops) -> bytes:
    return base64.b64decode(doc_frame(frame_id, *ops))


def type_in(ctx, sid, frame_id: str, text: str, who: str = "Ann", fold: bool = True) -> None:
    """`who` makes the notes say `text` (a toy `set`: lines keep their refs by matching)."""
    ctx.repo.append_notes_ops(sid, [frame(frame_id, ["set", text])], "u-" + who, who, True)
    if fold:
        shared_notes.fold_blocking(ctx, sid)


def edit(ctx, sid, frame_id: str, old: str, new: str, who: str = "Ann") -> str:
    text = ctx.repo.get(sid).notes
    assert old in text
    type_in(ctx, sid, frame_id, text.replace(old, new, 1), who)
    return text.replace(old, new, 1)


def summary(ctx, sid) -> SummaryData:
    return ctx.repo.get(sid).summary_data


def taken(ctx, sid) -> list[dict]:
    return [e["detail"] for e in ctx.repo.events(sid) if e["kind"] == "notes_summary_edits_taken"]


def events(ctx, sid, kind) -> list[dict]:
    return [e["detail"] for e in ctx.repo.events(sid) if e["kind"] == kind]


def refs(ctx, sid) -> list[str]:
    return [r["ref"] for r in ctx.repo.notes_refs(sid) or []]


def summary_ops(ctx, sid) -> int:
    return sum(o["author"] == shared_notes.SUMMARY_AUTHOR for o in ctx.repo.notes_ops(sid))


def counts(items=0, decisions=0, questions=0, body=False, by="Ann") -> dict:
    return {"items": items, "decisions": decisions, "questions": questions, "body": body, "by": by}


# ---- text edits ----------------------------------------------------------------------------


def test_a_reworded_decision(ctx, sidecar):
    sid = written_meeting(ctx)
    versions = len(ctx.repo.versions(sid))
    text = edit(ctx, sid, "ann1", "- Ship it in October\n", "- Ship it in November\n")
    d = summary(ctx, sid)
    assert d.decisions == ["Ship it in November", "Hire a designer"]
    assert d.decision_ids == [D1, D2]
    assert d.edited_by == "Ann"
    assert taken(ctx, sid) == [counts(decisions=1)]
    assert len(ctx.repo.versions(sid)) == versions + 1
    assert ctx.repo.get_summary_doc(sid)["notes"]["written"]["blocks"][D1]["shown"] == (
        "- Ship it in November"
    )
    # The sync that follows the summary's write finds the notes as they are: nothing written,
    # nothing read back again.
    ops = summary_ops(ctx, sid)
    assert not sync(ctx, sid, create=False)
    assert summary_ops(ctx, sid) == ops and ctx.repo.get(sid).notes == text
    assert len(taken(ctx, sid)) == 1


def test_a_new_question_line(ctx, sidecar):
    sid = written_meeting(ctx)
    text = edit(ctx, sid, "ann1", "- Who pays?\n", "- Who pays?\n- When do we start?\n")
    d = summary(ctx, sid)
    assert d.open_questions == ["Who pays?", "When do we start?"]
    new_id = d.question_ids[1]
    assert new_id and new_id != Q1
    assert taken(ctx, sid) == [counts(questions=1)]
    # The line gets the block's ref (same text), and is never added a second time.
    assert sync(ctx, sid, create=False)
    assert ctx.repo.get(sid).notes == text
    assert refs(ctx, sid)[-1] == f"sum:{new_id}"
    assert not sync(ctx, sid, create=True)
    assert ctx.repo.get(sid).notes == text
    assert len(taken(ctx, sid)) == 1


def test_a_new_question_is_not_doubled_by_a_summarize_before_its_ref(ctx, sidecar):
    sid = written_meeting(ctx)
    text = edit(ctx, sid, "ann1", "- Who pays?\n", "- Who pays?\n- When do we start?\n")
    assert sync(ctx, sid, create=True)  # the line is adopted, not inserted again
    assert ctx.repo.get(sid).notes == text
    assert summary(ctx, sid).open_questions == ["Who pays?", "When do we start?"]


def test_a_deleted_item_is_tombstoned_and_a_resummarize_keeps_it_out(ctx, sidecar):
    sid = written_meeting(ctx)
    text = edit(ctx, sid, "ann1", "- Hire a designer\n", "")
    assert summary(ctx, sid).decisions == ["Ship it in October"]
    assert D2 in summary_doc.ensure_doc(ctx, sid).tombstones
    assert taken(ctx, sid) == [counts(decisions=1)]
    assert not sync(ctx, sid, create=False)
    assert ctx.repo.get(sid).notes == text

    # The LLM says it again: the merge keeps it deleted, and the notes do not get it back.
    llm = SummaryData(
        decisions=["Ship it in October", "Hire a designer"],
        action_items=[ActionItem(text="Update the docs", owner="Ada", due=date(2026, 11, 10))],
        open_questions=["Who pays?"],
    )
    out, body, _ = summary_doc.apply_llm(ctx, sid, llm, BODY)
    ctx.repo.update_fields(sid, summary=body, summary_data=out)
    assert out.decisions == ["Ship it in October"]
    sync(ctx, sid, create=True)
    assert "Hire a designer" not in ctx.repo.get(sid).notes


def test_a_deleted_heading_deletes_nothing(ctx, sidecar):
    sid = written_meeting(ctx)
    edit(ctx, sid, "ann1", "### Decisions\n- Ship it in October\n- Hire a designer\n", "")
    assert summary(ctx, sid).decisions == ["Ship it in October", "Hire a designer"]
    assert taken(ctx, sid) == []
    # Only the heading: its lines run into the body, and are neither body nor decisions.
    sid = written_meeting(ctx)
    edit(ctx, sid, "ann2", "### Decisions\n", "")
    assert summary(ctx, sid).decisions == ["Ship it in October", "Hire a designer"]
    assert ctx.repo.get(sid).summary == BODY
    assert taken(ctx, sid) == []


def test_an_owner_change(ctx, sidecar):
    sid = written_meeting(ctx)
    edit(ctx, sid, "ann1", "(Ada)", "(Bob)")
    item = summary(ctx, sid).action_items[0]
    assert (item.id, item.text, item.owner, item.due) == (
        A1,
        "Update the docs",
        "Bob",
        date(2026, 11, 10),
    )
    assert taken(ctx, sid) == [counts(items=1)]


def test_a_due_date_change(ctx, sidecar):
    sid = written_meeting(ctx)
    edit(ctx, sid, "ann1", "📅 2026-11-10", "📅 2026-11-17")
    item = summary(ctx, sid).action_items[0]
    assert (item.text, item.owner, item.due) == ("Update the docs", "Ada", date(2026, 11, 17))
    edit(ctx, sid, "ann2", " 📅 2026-11-17", "")
    assert summary(ctx, sid).action_items[0].due is None


def test_a_body_edit(ctx, sidecar):
    sid = written_meeting(ctx)
    new = "We met about the launch and the budget.\n\n- The budget is fixed."
    edit(ctx, sid, "ann1", BODY + "\n", new + "\n")
    assert ctx.repo.get(sid).summary == new
    assert taken(ctx, sid) == [counts(body=True)]
    assert not sync(ctx, sid, create=False)


def test_a_decisions_heading_typed_in_the_body_is_not_a_section(ctx, sidecar):
    sid = written_meeting(ctx)
    new = BODY + "\n## Decisions\n- Not a decision"
    edit(ctx, sid, "ann1", BODY + "\n", new + "\n")
    session = ctx.repo.get(sid)
    assert session.summary == new
    assert session.summary_data.decisions == ["Ship it in October", "Hire a designer"]
    assert session.summary_data.decision_ids == [D1, D2]
    assert taken(ctx, sid) == [counts(body=True)]


def test_a_person_chip_task_line_is_a_notes_item(ctx, sidecar):
    sid = written_meeting(ctx)
    line = "- [ ] [@Bob](mnemosyne:person/Bob) book the venue\n"
    edit(ctx, sid, "ann1", "- [ ] Update the docs (Ada) 📅 2026-11-10\n",
         "- [ ] Update the docs (Ada) 📅 2026-11-10\n" + line)  # fmt: skip
    session = ctx.repo.get(sid)
    assert [(a.id, a.text) for a in session.summary_data.action_items] == [(A1, "Update the docs")]
    assert [(n.text, n.owner) for n in session.notes_items] == [("book the venue", "Bob")]
    assert taken(ctx, sid) == []
    # A plain task line there is a new summary item (its owner as text).
    edit(ctx, sid, "ann2", line, line + "- [ ] Book the train (Cy)\n")
    items = summary(ctx, sid).action_items
    assert [(a.text, a.owner) for a in items] == [
        ("Update the docs", "Ada"),
        ("Book the train", "Cy"),
    ]
    assert len(ctx.repo.get(sid).notes_items) == 1


# ---- three-way and loops -------------------------------------------------------------------


def _edit_summary(ctx, sid, by="me", **changes) -> None:
    """The Summary tab's edit: through the summary doc, the notes not synced yet."""
    d = summary(ctx, sid).model_copy(update=changes)
    out, body, _ = summary_doc.apply_human(ctx, sid, d, ctx.repo.get(sid).summary, by=by)
    ctx.repo.update_fields(sid, summary=body, summary_data=out)


def test_a_line_edited_after_the_summary_moved_is_merged(ctx, sidecar):
    sid = written_meeting(ctx)
    _edit_summary(ctx, sid, decisions=["Ship it in late October", "Hire a designer"])
    edit(ctx, sid, "ann1", "- Ship it in October\n", "- Ship it in October, on a Friday\n")
    merged = "Ship it in late October, on a Friday"
    assert summary(ctx, sid).decisions == [merged, "Hire a designer"]
    assert taken(ctx, sid) == [counts(decisions=1)]
    # The sync writes the merge result, which then reads back as it is.
    assert sync(ctx, sid, create=False)
    assert f"- {merged}\n" in ctx.repo.get(sid).notes
    assert summary(ctx, sid).decisions == [merged, "Hire a designer"]
    assert len(taken(ctx, sid)) == 1
    assert not sync(ctx, sid, create=False)


def test_edits_not_folded_yet_are_read_back_before_a_sync(ctx, sidecar):
    sid = written_meeting(ctx)
    text = ctx.repo.get(sid).notes.replace("- Hire a designer\n", "- Hire two designers\n")
    type_in(ctx, sid, "ann1", text, fold=False)
    _edit_summary(ctx, sid, decisions=["Ship it in November", "Hire a designer"])
    assert sync(ctx, sid, create=False)
    assert summary(ctx, sid).decisions == ["Ship it in November", "Hire two designers"]
    assert ctx.repo.get(sid).notes == text.replace("October", "November")
    assert taken(ctx, sid) == [counts(decisions=1)]


def test_our_own_writes_are_not_read_as_edits(ctx, sidecar):
    sid = written_meeting(ctx)
    # The summary changes elsewhere (a re-summarize): the sync rewrites lines, the read-back of
    # that write sees what was written.
    ctx.repo.update_fields(
        sid,
        summary="We met again.",
        summary_data=data(decisions=["Ship it soon", "Hire a designer"]),
    )
    versions = len(ctx.repo.versions(sid))  # the re-summarize's own
    assert sync(ctx, sid, create=False)
    assert sync(ctx, sid, create=True) is False
    # Someone types above the section: the section is as written.
    edit(ctx, sid, "ann1", "Mine\n", "Mine, and more\n")
    assert ctx.repo.get(sid).notes.endswith(
        SECTION.replace(BODY, "We met again.").replace("in October", "soon")
    )
    assert taken(ctx, sid) == []
    assert len(ctx.repo.versions(sid)) == versions
    assert summary(ctx, sid).decisions == ["Ship it soon", "Hire a designer"]


def test_nothing_with_the_setting_off_or_on_another_computers_meeting(ctx, sidecar):
    sid = written_meeting(ctx)
    ctx.settings.summary_in_notes = False
    edit(ctx, sid, "ann1", "- Ship it in October\n", "- Ship it in November\n")
    assert summary(ctx, sid).decisions[0] == "Ship it in October"
    ctx.settings.summary_in_notes = True
    sid = written_meeting(ctx)
    with ctx.repo._lock, ctx.repo._conn:
        ctx.repo._conn.execute("UPDATE sessions SET origin=? WHERE id=?", ("otherdevice1", sid))
    edit(ctx, sid, "ann2", "- Ship it in October\n", "- Ship it in November\n")
    assert summary(ctx, sid).decisions[0] == "Ship it in October"
    assert taken(ctx, sid) == []


# ---- ticks ---------------------------------------------------------------------------------


def _tick(ctx, sid, frame_id: str, who: str, kind: str = "task_done") -> None:
    box = " " if kind == "task_done" else "x"
    line = (
        ctx.repo.get(sid).notes.split("\n").index(f"- [{box}] Update the docs (Ada) 📅 2026-11-10")
    )
    ctx.repo.append_notes_ops(sid, [frame(frame_id, ["kind", line, kind])], "u-" + who, who, True)
    shared_notes.fold_blocking(ctx, sid)
    assert notes_summary.wait_ticks()  # ticks run once the notes lock is free


def test_a_tick_by_another_person_ticks_the_task_everywhere(ctx, sidecar):
    older = ctx.repo.save(
        Session(
            name="earlier",
            created_at=datetime(2026, 10, 1, 10),
            summary="s",
            summary_data=SummaryData(
                action_items=[ActionItem(id="b1bbbbbb", text="Update the docs", owner="Ada")]
            ),
        )
    ).id
    sid = written_meeting(ctx)
    assert task_links.link_items(ctx, sid, "exact")
    assert summary(ctx, sid).action_items[0].task_id

    _tick(ctx, sid, "bob1", "Bob")
    assert summary(ctx, sid).action_items[0].done is True
    assert summary(ctx, older).action_items[0].done is True  # the task's other mention
    assert events(ctx, sid, "task_done") == [{"task": "Update the docs", "by": "Bob"}]
    assert events(ctx, older, "task_done") == [{"task": "Update the docs", "by": "Bob"}]
    assert taken(ctx, sid) == []  # a tick is not a text edit
    # The sync after finds the box as it renders it: nothing written, nothing ticked again.
    ops = summary_ops(ctx, sid)
    assert not sync(ctx, sid, create=False)
    assert summary_ops(ctx, sid) == ops

    _tick(ctx, sid, "cy1", "Cy", "task")
    assert summary(ctx, sid).action_items[0].done is False
    assert summary(ctx, older).action_items[0].done is False
    assert events(ctx, sid, "task_reopened") == [{"task": "Update the docs", "by": "Cy"}]


def test_an_unlinked_tick_and_a_stale_box(ctx, sidecar):
    sid = written_meeting(ctx)
    _tick(ctx, sid, "bob1", "Bob")
    assert summary(ctx, sid).action_items[0].done is True
    assert events(ctx, sid, "task_done") == [{"task": "Update the docs", "by": "Bob"}]
    sync(ctx, sid, create=False)
    # Reopened in the app; before the notes follow, someone types elsewhere: the box the
    # notes still show is the old state, not a tick.
    ctx.repo.set_action_item_done(sid, 0, False)
    edit(ctx, sid, "ann1", "Mine\n", "Mine!\n")
    assert summary(ctx, sid).action_items[0].done is False
    assert events(ctx, sid, "task_done") == [{"task": "Update the docs", "by": "Bob"}]


def test_a_ticked_summary_line_is_one_task_never_a_notes_item(ctx, sidecar):
    # The e2e's case (CI, 2026-11-04): an earlier meeting already has the same action item,
    # done, so the new one stays its own task (linking reaches open tasks only). Ticking the
    # Summary section's line gives the meeting exactly one task, the summary's item.
    earlier = ctx.repo.save(
        Session(
            name="earlier",
            created_at=datetime(2026, 10, 1, 10),
            summary="s",
            summary_data=SummaryData(
                action_items=[
                    ActionItem(id="b1bbbbbb", text="Update the docs", owner="Ada", done=True)
                ]
            ),
        )
    ).id
    sid = written_meeting(ctx)
    assert task_links.link_items(ctx, sid, "exact") == []
    _tick(ctx, sid, "bob1", "Bob")
    sync(ctx, sid, create=False)
    shared_notes.fold_blocking(ctx, sid)
    assert ctx.repo.get(sid).notes_items == []
    objs = task_links.index(ctx.repo, ctx.repo.visible_ids()).values()
    ours = [
        [(m.source, m.item_id) for m in o.mentions] for o in objs if o.mentions[0].session_id == sid
    ]
    assert ours == [[("summary", A1)]]
    # Two rows in Tasks with the same text, one per meeting: the e2e must look in its own group.
    assert sorted(o.mentions[0].session_id for o in objs) == sorted([sid, earlier])
    assert summary(ctx, sid).action_items[0].done is True


def _noted_meeting(ctx) -> str:
    """An earlier meeting whose action item was typed in its notes (a notes item, Ada's)."""
    sid = ctx.repo.save(
        Session(
            name="earlier",
            created_at=datetime(2026, 10, 1, 10),
            notes="- [ ] [@Ada](mnemosyne:person/Ada) Update the docs\n",
        )
    ).id
    shared_notes.ensure_doc_blocking(ctx, sid)
    shared_notes.fold_blocking(ctx, sid)
    assert [n.text for n in ctx.repo.get(sid).notes_items] == ["Update the docs"]
    return sid


def test_a_tick_fans_out_with_no_notes_lock_held(ctx, sidecar, monkeypatch):
    older = _noted_meeting(ctx)
    sid = written_meeting(ctx)
    assert task_links.link_items(ctx, sid, "exact")
    assert ctx.repo.get(older).notes_items[0].task_id == summary(ctx, sid).action_items[0].task_id
    hub = shared_notes.hub(ctx)
    seen: list[dict] = []
    real = task_links.set_done

    def probing(app, key, done, **kw):
        state = {}
        for name, meeting in (("this", sid), ("older", older)):
            lock = hub.lock(meeting)
            free = lock.acquire(blocking=False)  # held by no other thread
            if free:
                lock.release()
            state[name] = free and not lock._is_owned()  # nor by this one
        seen.append(state)
        return real(app, key, done, **kw)

    monkeypatch.setattr(task_links, "set_done", probing)
    _tick(ctx, sid, "bob1", "Bob")
    assert seen == [{"this": True, "older": True}]
    assert summary(ctx, sid).action_items[0].done is True
    # The fan-out ticked the earlier meeting's notes line (through its document).
    assert ctx.repo.get(older).notes_items[0].done is True
    assert ctx.repo.get(older).notes.startswith("- [x] ")


def test_crossing_ticks_in_two_meetings_do_not_deadlock(ctx, sidecar):
    older = _noted_meeting(ctx)
    sid = written_meeting(ctx)
    assert task_links.link_items(ctx, sid, "exact")
    line = ctx.repo.get(sid).notes.split("\n").index("- [ ] Update the docs (Ada) 📅 2026-11-10")
    ctx.repo.append_notes_ops(sid, [frame("bob1", ["kind", line, "task_done"])], "u-b", "Bob", True)
    ctx.repo.append_notes_ops(older, [frame("cy1", ["kind", 0, "task_done"])], "u-c", "Cy", True)
    threads = [
        threading.Thread(target=shared_notes.fold_blocking, args=(ctx, m), daemon=True)
        for m in (sid, older)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join(20)
    assert not any(t.is_alive() for t in threads)
    assert notes_summary.wait_ticks(20)
    assert summary(ctx, sid).action_items[0].done is True
    assert ctx.repo.get(older).notes_items[0].done is True


def test_a_fresh_app_reads_the_notes_back_from_the_start(settings, keystore, monkeypatch):
    monkeypatch.setattr(shared_notes, "AFTER_FOLD", [])
    create_app(settings, keystore=keystore)
    assert shared_notes.AFTER_FOLD == [notes_summary.read_back]
