"""Ticks come back from Obsidian (services/vault_sync.py) and ticks in the app go to the note."""

import os
import time
from datetime import date, datetime

import pytest

from mnemosyne.api.routes.export import export_session
from mnemosyne.models.session import ActionItem, Session, SummaryData
from mnemosyne.services import vault_sync
from mnemosyne.services.vault_sync import (
    apply_vault_ticks,
    note_path,
    parse_ticks,
    read_ticks,
    sync_vault,
    write_tick,
)

NOTE = """---
title: "Sync"
source: mnemosyne
---

# Sync

## Action Items

- [x] Write the guide ([[Corey Petty]]) [issue](https://example.com/1) 📅 2026-10-01 ✅ 2026-10-12
- [ ] Send the deck ([[Me]])
- [/] Book the room
- [ ] Ask about it (Speaker 12)
- [-] Drop the idea ❌ 2026-10-12
- [X] Ship it ⏫ 🔁 every week ➕ 2026-10-01 ^abc123

### Others

- [[Ana]]: Fix the build
- [x] Fix the build

## Open Questions

- [ ] Not an action item
"""


def test_parse_strips_our_decorations_and_tasks_fields():
    ticks = parse_ticks(NOTE)
    assert [(t.text, t.done) for t in ticks] == [
        ("Write the guide", True),
        ("Send the deck", False),
        ("Book the room", None),  # In Progress: left alone
        ("Ask about it (Speaker 12)", False),  # an owner not linked: matched per item
        ("Drop the idea", None),  # Cancelled
        ("Ship it", True),
    ]
    lines = NOTE.splitlines()
    assert lines[ticks[1].line] == "- [ ] Send the deck ([[Me]])"


def test_read_ticks_only_from_our_notes(tmp_path):
    ours = tmp_path / "ours.md"
    ours.write_text(NOTE, encoding="utf-8")
    assert len(read_ticks(ours)) == 6
    theirs = tmp_path / "theirs.md"
    theirs.write_text(NOTE.replace("source: mnemosyne\n", ""), encoding="utf-8")
    assert read_ticks(theirs) == []
    assert read_ticks(tmp_path / "missing.md") == []


ITEMS = [
    ActionItem(text="Write the guide", owner="Corey Petty", due=date(2026, 10, 1)),
    ActionItem(text="Send the deck", owner="Me"),
    ActionItem(text="Fix the build", owner="Ana"),
    ActionItem(text="Ask about it", owner="SPEAKER_12", follow=True),  # a label: not linked
]


@pytest.fixture
def vault(ctx, tmp_path):
    root = tmp_path / "vault"
    root.mkdir()
    st = ctx.settings
    st.obsidian_vault_path = str(root)
    st.obsidian_subfolder = "meetings"
    st.local_speaker_name = "Corey Petty"
    st.obsidian_people_notes = False
    st.obsidian_daily_notes = False
    return root


def _meeting(ctx, vault, done=(), export=True, name="Sync") -> Session:
    items = [i.model_copy(update={"done": n in done}) for n, i in enumerate(ITEMS)]
    session = Session(
        name=name,
        created_at=datetime(2026, 10, 12, 9),
        summary="s",
        summary_data=SummaryData(action_items=items),
    )
    ctx.repo.save(session)
    session = ctx.sessions.get_session(session.id)
    if export:
        export_session(ctx, session, str(vault))
    return session


def _edit(path, old, new, later=True):
    text = path.read_text(encoding="utf-8")
    assert old in text
    path.write_text(text.replace(old, new), encoding="utf-8")
    if later:  # written after the meeting last changed, whatever the clock's granularity
        t = time.time() + 5
        os.utime(path, (t, t))


def _done(ctx, session):
    return [i.done for i in ctx.sessions.get_session(session.id).summary_data.action_items]


def _ticks_by(ctx, session):
    return [
        (e["kind"], e["detail"]["task"], e["detail"]["by"])
        for e in ctx.repo.events(session.id)
        if e["kind"] in ("task_done", "task_reopened")
    ]


def test_a_tick_in_the_vault_is_taken(ctx, vault):
    s = _meeting(ctx, vault)
    path = note_path(ctx, s, str(vault))
    _edit(path, "- [ ] Send the deck ([[Me]])", "- [x] Send the deck ([[Me]]) ✅ 2026-10-12")
    _edit(path, "- [ ] Ask about it (SPEAKER_12)", "- [x] Ask about it (SPEAKER_12)")
    assert apply_vault_ticks(ctx, s, str(vault)) is True
    assert _done(ctx, s) == [False, True, False, True]
    assert _ticks_by(ctx, s) == [
        ("task_done", "Send the deck", "Obsidian"),
        ("task_done", "Ask about it", "Obsidian"),
    ]
    assert apply_vault_ticks(ctx, s, str(vault)) is False  # the same again: nothing to do


def test_an_untick_in_the_vault_reopens(ctx, vault):
    s = _meeting(ctx, vault, done={0})
    path = note_path(ctx, s, str(vault))
    _edit(path, "- [x] Write the guide", "- [ ] Write the guide")
    assert apply_vault_ticks(ctx, s, str(vault)) is True
    assert _done(ctx, s) == [False, False, False, False]
    assert _ticks_by(ctx, s) == [("task_reopened", "Write the guide", "Obsidian")]


def test_a_stale_note_does_not_reopen(ctx, vault):
    """Exported before the item was ticked in the app (and never written back)."""
    s = _meeting(ctx, vault)
    path = note_path(ctx, s, str(vault))
    t = time.time() - 3600
    os.utime(path, (t, t))
    ctx.repo.set_action_item_done(s.id, 0, True)
    assert apply_vault_ticks(ctx, s, str(vault)) is False
    assert _done(ctx, s)[0] is True


def test_other_statuses_are_left_alone(ctx, vault):
    s = _meeting(ctx, vault, done={0})
    path = note_path(ctx, s, str(vault))
    _edit(path, "- [x] Write the guide", "- [/] Write the guide")
    _edit(path, "- [ ] Send the deck", "- [-] Send the deck")
    assert apply_vault_ticks(ctx, s, str(vault)) is False
    assert _done(ctx, s) == [True, False, False, False]


def test_others_are_not_tasks(ctx, vault):
    s = _meeting(ctx, vault)
    path = note_path(ctx, s, str(vault))
    assert "### Others\n\n- [[Ana]]: Fix the build" in path.read_text(encoding="utf-8")
    _edit(path, "- [[Ana]]: Fix the build", "- [x] Fix the build")
    assert apply_vault_ticks(ctx, s, str(vault)) is False
    assert _done(ctx, s)[2] is False


def test_a_vault_tick_survives_exporting_again(client, ctx, vault):
    s = _meeting(ctx, vault)
    path = note_path(ctx, s, str(vault))
    _edit(path, "- [ ] Send the deck ([[Me]])", "- [x] Send the deck ([[Me]]) ✅ 2026-10-12")
    assert client.post(f"/api/sessions/{s.id}/export/obsidian").status_code == 200
    assert "- [x] Send the deck ([[Me]])" in path.read_text(encoding="utf-8")
    assert _done(ctx, s)[1] is True


def test_a_tick_in_the_app_rewrites_only_its_line(client, ctx, vault):
    s = _meeting(ctx, vault)
    path = note_path(ctx, s, str(vault))
    # The user's own additions, and Windows line endings, must survive.
    original = (path.read_text(encoding="utf-8") + "\nMy own line.\n").replace("\n", "\r\n")
    path.write_bytes(original.encode("utf-8"))

    r = client.patch(f"/api/sessions/{s.id}/action-items/1", json={"done": True})
    assert r.status_code == 200
    today = date.today().isoformat()
    expected = original.replace(
        "- [ ] Send the deck ([[Me]])\r\n", f"- [x] Send the deck ([[Me]]) ✅ {today}\r\n"
    )
    assert expected != original
    assert path.read_bytes() == expected.encode("utf-8")

    r = client.patch(f"/api/sessions/{s.id}/action-items/1", json={"done": False})
    assert r.status_code == 200
    assert path.read_bytes() == original.encode("utf-8")  # back to what it was, byte for byte
    assert not list(path.parent.glob(".*.mnemosyne"))  # no temp file left


def test_a_tick_keeps_obsidians_done_date(ctx, vault):
    s = _meeting(ctx, vault)
    path = note_path(ctx, s, str(vault))
    _edit(path, "- [ ] Send the deck ([[Me]])", "- [/] Send the deck ([[Me]]) ✅ 2026-10-01")
    assert write_tick(ctx, s, 1, True) is True
    assert "- [x] Send the deck ([[Me]]) ✅ 2026-10-01\n" in path.read_text(encoding="utf-8")
    assert write_tick(ctx, s, 1, True) is False  # already says so: not written


def test_a_note_that_is_not_ours_is_never_written(client, ctx, vault):
    s = _meeting(ctx, vault, export=False)
    path = note_path(ctx, s, str(vault))
    path.parent.mkdir(parents=True)
    text = "---\ntitle: Sync\n---\n\n## Action Items\n\n- [ ] Send the deck ([[Me]])\n"
    path.write_text(text, encoding="utf-8")
    assert client.patch(f"/api/sessions/{s.id}/action-items/1", json={"done": True}).is_success
    assert path.read_text(encoding="utf-8") == text
    _edit(path, "- [ ] Send", "- [ ] Send", later=True)
    assert apply_vault_ticks(ctx, s, str(vault)) is False  # nor read
    assert _done(ctx, s)[1] is True


def test_the_periodic_pass_reads_only_changed_notes(ctx, vault, monkeypatch):
    s = _meeting(ctx, vault)
    _meeting(ctx, vault, export=False, name="Other")  # no note: nothing to read
    calls = []
    real = vault_sync.apply_vault_ticks

    def spy(ctx_, session, vault_path):
        calls.append(session.id)
        return real(ctx_, session, vault_path)

    monkeypatch.setattr(vault_sync, "apply_vault_ticks", spy)
    assert sync_vault(ctx) == 0 and calls == [s.id]  # first pass: every note
    assert sync_vault(ctx) == 0 and calls == [s.id]  # unchanged: not read again
    path = note_path(ctx, s, str(vault))
    _edit(path, "- [ ] Send the deck ([[Me]])", "- [x] Send the deck ([[Me]])")
    assert sync_vault(ctx) == 1 and calls == [s.id, s.id]
    assert _done(ctx, s)[1] is True


def test_no_pass_without_a_vault(ctx, tmp_path):
    ctx.settings.obsidian_vault_path = ""
    assert sync_vault(ctx) == 0
    ctx.settings.obsidian_vault_path = str(tmp_path / "gone")
    assert sync_vault(ctx) == 0


def _ided_meeting(ctx, vault):
    items = [
        ActionItem(text="Write the guide", owner="Corey Petty", due=date(2026, 10, 1), id="a1"),
        ActionItem(text="Send the deck", owner="Me", id="b2"),
        ActionItem(text="Fix the build", owner="Ana", id="c3"),
    ]
    session = Session(
        name="Ids",
        created_at=datetime(2026, 10, 12, 9),
        summary="s",
        summary_data=SummaryData(
            action_items=items,
            decisions=["Use Rust"],
            decision_ids=["d4"],
            open_questions=["Who pays?"],
            question_ids=["e5"],
        ),
    )
    ctx.repo.save(session)
    session = ctx.sessions.get_session(session.id)
    export_session(ctx, session, str(vault))
    return session


def test_block_ids_end_every_summary_line(ctx, vault):
    s = _ided_meeting(ctx, vault)
    text = note_path(ctx, s, str(vault)).read_text(encoding="utf-8")
    assert "- Use Rust ^m-d4\n" in text
    assert "- Who pays? ^m-e5\n" in text
    lines = text.splitlines()
    assert any(ln.startswith("- [ ] Send the deck") and ln.endswith(" ^m-b2") for ln in lines)
    assert any(ln.startswith("- [ ] Write the guide") and ln.endswith("📅 2026-10-01 ^m-a1")
               for ln in lines)  # fmt: skip
    assert "- [[Ana]]: Fix the build ^m-c3" in lines
    assert [t.block_id for t in parse_ticks(text)] == ["a1", "b2"]


def test_ticks_match_by_id_when_the_text_was_reworded(ctx, vault):
    s = _ided_meeting(ctx, vault)
    path = note_path(ctx, s, str(vault))
    _edit(path, "- [ ] Send the deck ([[Me]]) ^m-b2", "- [x] Mail the slides to everyone ^m-b2")
    assert apply_vault_ticks(ctx, s, str(vault)) is True
    assert _done(ctx, s) == [False, True, False]


def test_write_tick_keeps_the_block_id_last(ctx, vault):
    s = _ided_meeting(ctx, vault)
    path = note_path(ctx, s, str(vault))
    assert write_tick(ctx, s, 1, True) is True
    today = date.today().isoformat()
    text = path.read_text(encoding="utf-8")
    assert f"- [x] Send the deck ([[Me]]) ✅ {today} ^m-b2\n" in text
    assert write_tick(ctx, s, 1, False) is True
    assert "- [ ] Send the deck ([[Me]]) ^m-b2\n" in path.read_text(encoding="utf-8")


def test_write_tick_finds_a_reworded_line_by_id(ctx, vault):
    s = _ided_meeting(ctx, vault)
    path = note_path(ctx, s, str(vault))
    _edit(path, "Send the deck ([[Me]])", "Mail the slides")
    assert write_tick(ctx, s, 1, True) is True
    today = date.today().isoformat()
    assert f"- [x] Mail the slides ✅ {today} ^m-b2\n" in path.read_text(encoding="utf-8")


def test_notes_without_ids_still_match_by_text(ctx, vault):
    s = _meeting(ctx, vault)  # items without ids: no block ids rendered
    path = note_path(ctx, s, str(vault))
    assert "^m-" not in path.read_text(encoding="utf-8")
    _edit(path, "- [ ] Send the deck ([[Me]])", "- [x] Send the deck ([[Me]])")
    assert apply_vault_ticks(ctx, s, str(vault)) is True
    assert _done(ctx, s)[1] is True


# ---- one task in several meetings: one checkbox, ticks fan out ------------------------------

TASK = "abcdef012345"


def _task_meeting(ctx, name, day, linked, item_id="aaaa0001"):
    item = ActionItem(
        id=item_id, text="Update the docs", owner="Me", task_id=TASK if linked else ""
    )
    s = Session(
        name=name,
        created_at=datetime(2026, 10, day, 9),
        summary="s",
        summary_data=SummaryData(action_items=[item]),
    )
    ctx.repo.save(s)
    return ctx.sessions.get_session(s.id)


def _export_all(ctx, vault, *sessions):
    for s in sessions:
        export_session(ctx, ctx.sessions.get_session(s.id), str(vault))


def _link_all(ctx, *sessions):
    for s in sessions:
        assert ctx.repo.set_action_item_fields_by_id(s.id, "aaaa0001", task_id=TASK)


def _note(ctx, vault, s) -> str:
    return note_path(ctx, s, str(vault)).read_text(encoding="utf-8")


def _tick_in_a_thread(ctx, s, vault, timeout=15):
    """apply_vault_ticks must return: the fan-out writes notes, which takes the same lock."""
    import threading

    out = []
    t = threading.Thread(target=lambda: out.append(apply_vault_ticks(ctx, s, str(vault))))
    t.start()
    t.join(timeout)
    assert not t.is_alive(), "apply_vault_ticks hangs (deadlock on the vault lock)"
    return out[0]


def test_only_the_latest_mention_of_a_task_is_a_checkbox(ctx, vault):
    a = _task_meeting(ctx, "Infra weekly", 1, linked=True)
    b = _task_meeting(ctx, "Planning", 8, linked=True)
    _export_all(ctx, vault, a, b)
    assert (
        "- Update the docs ([[Me]]) ↩ continued in [[2026-10-08-Planning]] ^m-aaaa0001\n"
        in _note(ctx, vault, a)
    )
    assert "- [ ]" not in _note(ctx, vault, a)
    assert "- [ ] Update the docs ([[Me]]) ^m-aaaa0001\n" in _note(ctx, vault, b)
    assert "continued in" not in _note(ctx, vault, b)


def test_a_continued_bullet_is_no_tick(ctx, vault):
    a = _task_meeting(ctx, "Infra weekly", 1, linked=True)
    b = _task_meeting(ctx, "Planning", 8, linked=True)
    _export_all(ctx, vault, a, b)
    assert parse_ticks(_note(ctx, vault, a)) == []
    assert [t.text for t in parse_ticks(_note(ctx, vault, b))] == ["Update the docs"]


def test_an_item_nothing_continues_renders_as_before(ctx, vault):
    a = _task_meeting(ctx, "Infra weekly", 1, linked=False)
    b = _task_meeting(ctx, "Planning", 8, linked=False)
    _export_all(ctx, vault, a, b)
    for s in (a, b):
        assert "- [ ] Update the docs ([[Me]]) ^m-aaaa0001\n" in _note(ctx, vault, s)
        assert "continued in" not in _note(ctx, vault, s)
    # Linked, but a single meeting: it is the head.
    c = _task_meeting(ctx, "Alone", 20, linked=True)
    ctx.repo.set_action_item_fields_by_id(a.id, "aaaa0001", task_id="")
    ctx.repo.set_action_item_fields_by_id(b.id, "aaaa0001", task_id="")
    _export_all(ctx, vault, c)
    assert "- [ ] Update the docs" in _note(ctx, vault, c)


def test_a_tick_in_the_newer_note_ticks_the_older_meeting(ctx, vault):
    a = _task_meeting(ctx, "Infra weekly", 1, linked=True)
    b = _task_meeting(ctx, "Planning", 8, linked=True)
    _export_all(ctx, vault, a, b)
    path = note_path(ctx, b, str(vault))
    _edit(path, "- [ ] Update the docs", "- [x] Update the docs")
    assert _tick_in_a_thread(ctx, b, vault) is True
    assert _done(ctx, b) == [True] and _done(ctx, a) == [True]
    assert _ticks_by(ctx, a) == [("task_done", "Update the docs", "Obsidian")]
    assert _ticks_by(ctx, b) == [("task_done", "Update the docs", "Obsidian")]
    assert "- [" not in _note(ctx, vault, a)  # still the plain bullet
    assert "continued in [[2026-10-08-Planning]]" in _note(ctx, vault, a)
    # Reopened in Obsidian: both are open again.
    _edit(path, "- [x] Update the docs", "- [ ] Update the docs")
    assert _tick_in_a_thread(ctx, b, vault) is True
    assert _done(ctx, b) == [False] and _done(ctx, a) == [False]


def test_a_tick_rewrites_the_lines_of_the_other_meetings_notes(ctx, vault):
    """Notes exported before the task was linked still have a box each."""
    a = _task_meeting(ctx, "Infra weekly", 1, linked=False)
    b = _task_meeting(ctx, "Planning", 8, linked=False)
    c = _task_meeting(ctx, "Review", 15, linked=False)
    _export_all(ctx, vault, a, b, c)
    _link_all(ctx, a, b, c)
    _edit(note_path(ctx, b, str(vault)), "- [ ] Update the docs", "- [x] Update the docs")
    assert _tick_in_a_thread(ctx, b, vault) is True
    assert _done(ctx, a) == [True] and _done(ctx, b) == [True] and _done(ctx, c) == [True]
    for s in (a, c):
        assert "- [x] Update the docs ([[Me]]) ✅ " in _note(ctx, vault, s)
        assert _note(ctx, vault, s).count("^m-aaaa0001") == 1
    # Our own writes are not read back as the user's: nothing more to do.
    assert _tick_in_a_thread(ctx, a, vault) is False
    assert _tick_in_a_thread(ctx, c, vault) is False


def test_a_tick_of_an_unlinked_item_touches_no_other_meeting(ctx, vault):
    a = _task_meeting(ctx, "Infra weekly", 1, linked=False)
    b = _task_meeting(ctx, "Planning", 8, linked=False)
    _export_all(ctx, vault, a, b)
    _edit(note_path(ctx, b, str(vault)), "- [ ] Update the docs", "- [x] Update the docs")
    assert _tick_in_a_thread(ctx, b, vault) is True
    assert _done(ctx, b) == [True] and _done(ctx, a) == [False]


# ---- action items typed in the notes (services/notes_items.py) -------------------------------

ADA = "[@Ada Lovelace](mnemosyne:person/Ada%20Lovelace)"


@pytest.fixture
def sidecar(tmp_path, monkeypatch):
    from mnemosyne.services import shared_notes
    from tests import fake_link

    monkeypatch.setattr(shared_notes, "FOLD_DELAY", 1000.0)
    monkeypatch.setattr(shared_notes, "FOLD_EVERY", 1000.0)
    return fake_link.install(tmp_path, monkeypatch)


def _typed_meeting(ctx, vault, summary_items=()):
    """A summarized meeting whose notes hold one action item (Ada: sends the deck) and a
    checklist line; exported to the vault."""
    import base64

    from mnemosyne.services import shared_notes
    from tests.fake_link import doc_frame

    ctx.settings.obsidian_task_scope = "all"
    s = ctx.repo.save(
        Session(
            name="Typed",
            created_at=datetime(2026, 10, 12, 9),
            summary="s",
            summary_data=SummaryData(action_items=list(summary_items)),
        )
    )
    notes = f"- [ ] {ADA} sends the deck\n- [ ] buy milk\n"
    frame = base64.b64decode(doc_frame("f1", ["set", notes]))
    shared_notes.accept_frames(ctx, s.id, [frame], "u-ada", "Ada", can_edit=True)
    shared_notes.fold_blocking(ctx, s.id)
    (item,) = ctx.repo.notes_items(s.id)
    session = ctx.sessions.get_session(s.id)
    export_session(ctx, session, str(vault))
    return session, item


def test_a_tick_of_a_notes_item_in_the_vault_goes_through_write_fields(
    ctx, vault, sidecar, monkeypatch
):
    from mnemosyne.services import notes_items

    s, item = _typed_meeting(ctx, vault)
    path = note_path(ctx, s, str(vault))
    text = path.read_text(encoding="utf-8")
    line = f"- [ ] sends the deck ([[Ada Lovelace]]) ^m-{item.id}"
    assert line in text
    assert [t.block_id for t in parse_ticks(text)] == [item.id]
    calls = []
    real = notes_items.write_fields
    monkeypatch.setattr(
        notes_items, "write_fields", lambda *a, **kw: calls.append((a[2], kw)) or real(*a, **kw)
    )
    _edit(path, line, line.replace("[ ]", "[x]"))
    assert apply_vault_ticks(ctx, s, str(vault)) is True
    assert calls == [(item.id, {"author": ("", "Obsidian"), "done": True})]
    assert ctx.repo.notes_items(s.id)[0].done is True
    assert ctx.repo.get(s.id).notes.startswith("- [x]")  # the notes document says so too
    assert ctx.repo.notes_ops(s.id)[-1]["author"] == "Obsidian"
    assert _ticks_by(ctx, s) == [("task_done", "sends the deck", "Obsidian")]
    assert apply_vault_ticks(ctx, s, str(vault)) is False  # the same again: nothing to do


def test_an_untick_of_a_notes_item_in_the_vault_reopens_it(ctx, vault, sidecar):
    from mnemosyne.services import notes_items

    s, item = _typed_meeting(ctx, vault)
    assert notes_items.write_fields(ctx, s.id, item.id, done=True)
    path = note_path(ctx, s, str(vault))
    export_session(ctx, ctx.sessions.get_session(s.id), str(vault))
    line = f"- [x] sends the deck ([[Ada Lovelace]]) ✅ {date.today().isoformat()} ^m-{item.id}"
    _edit(path, line.split(" ✅")[0], line.split(" ✅")[0].replace("[x]", "[ ]"))
    assert apply_vault_ticks(ctx, s, str(vault)) is True
    assert ctx.repo.notes_items(s.id)[0].done is False
    assert _ticks_by(ctx, s) == [("task_reopened", "sends the deck", "Obsidian")]


def test_a_stale_note_does_not_reopen_a_notes_item(ctx, vault, sidecar):
    from mnemosyne.services import notes_items

    s, item = _typed_meeting(ctx, vault)
    path = note_path(ctx, s, str(vault))
    t = time.time() - 3600
    os.utime(path, (t, t))
    ctx.repo.delete_vault_note(s.id)  # no record: the clock decides
    assert notes_items.write_fields(ctx, s.id, item.id, done=True)
    assert apply_vault_ticks(ctx, s, str(vault)) is False
    assert ctx.repo.notes_items(s.id)[0].done is True


def test_a_notes_ticks_never_reach_a_similar_summary_item(ctx, vault, sidecar):
    twin = ActionItem(id="a1b2c3d4", text="sends the deck now", owner="Bo")
    s, item = _typed_meeting(ctx, vault, [twin])
    path = note_path(ctx, s, str(vault))
    line = f"- [ ] sends the deck ([[Ada Lovelace]]) ^m-{item.id}"
    _edit(path, line, line.replace("[ ]", "[x]"))
    assert apply_vault_ticks(ctx, s, str(vault)) is True
    assert [i.done for i in ctx.sessions.get_session(s.id).summary_data.action_items] == [False]
    assert ctx.repo.notes_items(s.id)[0].done is True
    # The other way round: a summary item's tick leaves the notes item alone.
    _edit(path, "- [x] sends the deck (", "- [ ] sends the deck (")
    _edit(path, "- [ ] sends the deck now", "- [x] sends the deck now")
    assert apply_vault_ticks(ctx, ctx.sessions.get_session(s.id), str(vault)) is True
    assert ctx.sessions.get_session(s.id).summary_data.action_items[0].done is True


def test_a_notes_item_without_a_sidecar_stays_as_it_is(ctx, vault, sidecar, monkeypatch):
    from mnemosyne.services import shared_notes

    s, item = _typed_meeting(ctx, vault)
    path = note_path(ctx, s, str(vault))
    line = f"- [ ] sends the deck ([[Ada Lovelace]]) ^m-{item.id}"
    _edit(path, line, line.replace("[ ]", "[x]"))
    monkeypatch.setattr(shared_notes, "_binary", lambda: None)
    assert apply_vault_ticks(ctx, s, str(vault)) is False  # logged, never raised
    assert ctx.repo.notes_items(s.id)[0].done is False
    assert _ticks_by(ctx, s) == []


def test_edits_in_the_vault_never_turn_a_notes_item_into_a_summary_item(ctx, vault, sidecar):
    from mnemosyne.services.vault_sync import apply_vault_edits

    s, item = _typed_meeting(ctx, vault, [ActionItem(id="a1b2c3d4", text="Order badges")])
    path = note_path(ctx, s, str(vault))
    text = path.read_text(encoding="utf-8")
    assert "- [ ] Order badges ^m-a1b2c3d4" in text
    _edit(path, "- [ ] Order badges", "- [ ] Order the badges")
    apply_vault_edits(ctx, s, str(vault))
    after = ctx.sessions.get_session(s.id)
    assert [a.text for a in after.summary_data.action_items] == ["Order the badges"]
    assert [n.id for n in after.notes_items] == [item.id]
