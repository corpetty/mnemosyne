"""Action items from the meeting notes (services/notes_items.py, phase 11): derivation from the
sidecar's task lines, state kept across edits and across set_text's line id shifts, history on
local folds only, the stored column. The sidecar is the `notes doc` stand-in of tests/fake_link.py
(a task line's id is its line index; `frame` is always null there, so tests that need the
author of a frame name it by patching the reply)."""

import base64
import sqlite3
from datetime import date

import pytest

from mnemosyne.models.session import NotesItem, Recording, Session
from mnemosyne.services import notes_items, shared_notes
from mnemosyne.storage.sqlite import SessionRepository
from tests import fake_link
from tests.fake_link import doc_frame

ADA = "[@Ada Lovelace](mnemosyne:person/Ada%20Lovelace)"
BOB = "[@Bob](mnemosyne:person/Bob)"


@pytest.fixture
def sidecar(tmp_path, monkeypatch):
    return fake_link.install(tmp_path, monkeypatch)


@pytest.fixture(autouse=True)
def no_timed_folds(monkeypatch):
    monkeypatch.setattr(shared_notes, "FOLD_DELAY", 1000.0)
    monkeypatch.setattr(shared_notes, "FOLD_EVERY", 1000.0)


def frame(frame_id: str, *ops) -> bytes:
    return base64.b64decode(doc_frame(frame_id, *ops))


def meeting(ctx, notes: str = "", **kw) -> str:
    return ctx.repo.save(Session(name="Planning", notes=notes, **kw)).id


def typed(ctx, sid: str, author: str, *frames: bytes) -> None:
    """Frames typed by `author` on this machine, folded now (no event loop: accept_frames folds)."""
    shared_notes.accept_frames(ctx, sid, list(frames), "u-" + author, author, can_edit=True)
    shared_notes.fold_blocking(ctx, sid)


def items(ctx, sid: str) -> list[NotesItem]:
    return ctx.repo.notes_items(sid)


def history(ctx, sid: str, *kinds: str) -> list[dict]:
    return [e for e in ctx.repo.events(sid) if e["kind"] in kinds]


def task(line: int, text: str, links=(), done=False, frame=None) -> dict:
    return {
        "line": format(line, "064x"),
        "done": done,
        "text": text,
        "links": [list(x) for x in links],
        "frame": frame,
    }


# ---- pure parts --------------------------------------------------------------------------


def test_item_ids_are_hex_never_all_digits_and_never_taken():
    line = "ab" * 8 + "cd" * 24
    assert notes_items.item_id(line, set()) == "ab" * 8
    assert notes_items.item_id(line, {"ab" * 8}) == "cd" * 8
    digits = "1234567890123456" + "a" * 48
    assert notes_items.item_id(digits, set()) == "a" * 16  # the next 16
    zero = format(3, "064x")  # all digits: one from a hash of it
    a = notes_items.item_id(zero, set())
    assert len(a) == 16 and not a.isdigit() and int(a, 16) >= 0
    assert notes_items.item_id(zero, set()) == a  # deterministic
    assert notes_items.item_id(zero, {a}) != a
    assert notes_items.item_id(line.upper(), set()) == "ab" * 8


def test_lines_owner_text_at_and_skips():
    offsets = {0: 0.0, 1: 600.0}
    text = "Send @Ada Lovelace the  deck 12:34 re Planning, by @Bob"
    links = [
        (5, 18, "mnemosyne:person/Ada%20Lovelace"),
        (29, 34, "mnemosyne:t/1/30"),
        (38, 46, "mnemosyne:meeting/abcdef12"),
        (51, 55, "mnemosyne:person/Bob"),
    ]
    tasks = [
        task(0, text, links, frame=2),
        task(1, "A checklist line"),  # no person chip
        task(2, "@Ada", [(0, 4, "mnemosyne:person/Ada")]),  # nothing but chips
        task(
            3,
            "Ask @Cy 1:00 2:00",
            [
                (4, 7, "mnemosyne:person/Cy"),
                (8, 12, "mnemosyne:t/7/60"),
                (13, 17, "mnemosyne:t/1/5"),
            ],
            done=True,
        ),
        task(4, "Call @Di 0:10", [(5, 8, "mnemosyne:person/Di"), (9, 13, "mnemosyne:t/0/10")]),
        task(5, "Link @x", [(5, 7, "https://example.com")]),  # not a chip
        {"line": "zz", "text": "bad", "links": []},
        "nonsense",
    ]
    out = notes_items.lines_of(tasks, offsets)
    assert [(x.line[-1], x.owner, x.text, x.at, x.done, x.frame) for x in out] == [
        ("0", "Ada Lovelace", "Send the deck re Planning, by", 630.0, False, 2),
        ("3", "Cy", "Ask", None, True, None),  # the first time chip names an unknown part
        ("4", "Di", "Call", 10.0, False, None),
    ]
    # Part 0 starts the timeline even before any recording row exists.
    assert notes_items.lines_of([task(4, "Call @Di 0:10", tasks[4]["links"])], {})[0].at == 10.0


def test_derive_keeps_state_and_matches_moved_lines():
    a = format(1, "064x")
    stored = [
        NotesItem(
            id="aaaa000000000001",
            line=a,
            text="Send deck",
            owner="Ada",
            follow=True,
            due=date(2026, 11, 2),
            issue_url="https://x/1",
            task_id="0123456789ab",
        ),
        NotesItem(id="bbbb000000000002", line=format(2, "064x"), text="Book room", owner="Bob"),
    ]
    lines = [
        notes_items.Line(format(9, "064x"), False, "Book  room!", "bob", None, None),  # moved
        notes_items.Line(a, True, "Send the deck", "Ada", 5.0, 0),  # edited and ticked
        notes_items.Line(format(7, "064x"), False, "New one", "Cy", None, None),
    ]
    change = notes_items.derive(stored, lines, ["Bob"], fallback="Me")
    moved, edited, new = change.items
    assert (moved.id, moved.line, moved.text, moved.owner) == (
        "bbbb000000000002",
        format(9, "064x"),
        "Book  room!",
        "bob",
    )
    assert edited.model_dump(
        include={"id", "text", "done", "at", "follow", "due", "issue_url", "task_id"}
    ) == {
        "id": "aaaa000000000001",
        "text": "Send the deck",
        "done": True,
        "at": 5.0,
        "follow": True,
        "due": date(2026, 11, 2),
        "issue_url": "https://x/1",
        "task_id": "0123456789ab",
    }
    assert new.id not in {"aaaa000000000001", "bbbb000000000002"} and new.by == "Me"
    assert [(i.id, by) for i, by in change.added] == [(new.id, "Me")]
    assert [(i.id, by) for i, by in change.ticked] == [("aaaa000000000001", "Bob")]
    assert change.changed
    assert not notes_items.derive(change.items, lines).changed


def test_a_different_owner_or_text_is_a_new_item():
    stored = [NotesItem(id="aaaa000000000001", line=format(1, "064x"), text="Send", owner="Ada")]
    for line in (
        notes_items.Line(format(2, "064x"), False, "Send", "Bob", None, None),
        notes_items.Line(format(2, "064x"), False, "Send it", "Ada", None, None),
    ):
        (item,) = notes_items.derive(stored, [line]).items
        assert item.id != "aaaa000000000001"


# ---- at the fold sites ---------------------------------------------------------------------


def with_frames(monkeypatch, index: int = -1) -> None:
    """The stand-in names a frame for every task line of a fold: the request frame at `index`."""
    run = shared_notes._run_doc

    def patched(binary, request):
        reply = run(binary, request)
        if request["op"] == "fold" and request["frames"]:
            for t in reply.get("tasks", []):
                t["frame"] = index % len(request["frames"])
        return reply

    monkeypatch.setattr(shared_notes, "_run_doc", patched)


def test_a_fold_derives_items_with_history_by_the_frames_author(ctx, sidecar, monkeypatch):
    with_frames(monkeypatch)
    sid = meeting(
        ctx,
        recordings=[
            Recording(source="mic", device_id=0, device_name="m", path="/a", part=0),
            Recording(source="mic", device_id=0, device_name="m", path="/b", part=1, offset=900),
        ],
    )
    typed(ctx, sid, "Ada", frame("f1", ["set", "# Plan\n- [ ] just a checklist\n"]))
    typed(
        ctx,
        sid,
        "Bob",
        frame("f2", ["ins", 30, f"- [ ] {ADA} sends the deck [1:00](mnemosyne:t/1/60)\n"]),
    )
    (item,) = items(ctx, sid)
    assert (item.owner, item.text, item.at, item.done, item.by, item.source) == (
        "Ada Lovelace",
        "sends the deck",
        960.0,
        False,
        "Bob",
        "notes",
    )
    assert len(item.id) == 16 and not item.id.isdigit()
    assert item.line == format(2, "064x")
    added = history(ctx, sid, "notes_task_added")
    assert [e["detail"] for e in added] == [
        {"task": "sends the deck", "owner": "Ada Lovelace", "by": "Bob"}
    ]
    # The API shows them.
    assert ctx.repo.get(sid).notes_items == [item]


def test_a_tick_typed_in_the_notes_keeps_state_and_logs(ctx, sidecar, monkeypatch):
    with_frames(monkeypatch)
    sid = meeting(ctx)
    line = f"- [ ] {BOB} books the room"
    typed(ctx, sid, "Ada", frame("f1", ["set", line + "\n"]))
    (item,) = items(ctx, sid)
    kept = item.model_copy(
        update={
            "follow": True,
            "due": date(2026, 11, 5),
            "issue_url": "https://x/9",
            "task_id": "00aa11bb22cc",
        }
    )
    ctx.repo.set_notes_items(sid, [kept])
    typed(ctx, sid, "Ada", frame("f2", ["ins", len(line), " today"]))  # edited
    typed(ctx, sid, "Cy", frame("f3", ["kind", 0, "task_done"]))
    (after,) = items(ctx, sid)
    assert after.done and after.text == "books the room today"
    assert after.model_dump(include={"id", "follow", "due", "issue_url", "task_id", "by"}) == {
        "id": item.id,
        "follow": True,
        "due": date(2026, 11, 5),
        "issue_url": "https://x/9",
        "task_id": "00aa11bb22cc",
        "by": "Ada",
    }
    assert [(e["kind"], e["detail"]["by"]) for e in history(ctx, sid, "task_done")] == [
        ("task_done", "Cy")
    ]
    typed(ctx, sid, "Ada", frame("f4", ["kind", 0, "task"]))
    assert [e["detail"]["by"] for e in history(ctx, sid, "task_reopened")] == ["Ada"]
    assert len(history(ctx, sid, "notes_task_added")) == 1


def test_state_follows_a_line_whose_id_set_text_shifted(ctx, sidecar):
    sid = meeting(ctx)
    typed(ctx, sid, "Ada", frame("f1", ["set", f"- [ ] {ADA} sends the deck\n"]))
    (item,) = items(ctx, sid)
    ctx.repo.set_notes_items(sid, [item.model_copy(update={"follow": True, "task_id": "abc"})])
    # An API write puts a line above it: the toy's line ids are indexes, so the task's changes.
    shared_notes.set_markdown_blocking(
        ctx, sid, f"# Notes\n- [ ] {ADA} sends the deck\n- [ ] {BOB} books\n"
    )
    first, second = items(ctx, sid)
    assert (first.id, first.line, first.follow, first.task_id) == (
        item.id,
        format(1, "064x"),
        True,
        "abc",
    )
    assert second.owner == "Bob" and second.id != item.id and not second.follow
    # "api" made it: derived without a history line.
    assert len(history(ctx, sid, "notes_task_added")) == 1


def test_deleted_lines_drop_their_items_and_unchanged_folds_write_nothing(ctx, sidecar):
    sid = meeting(ctx)
    typed(ctx, sid, "Ada", frame("f1", ["set", f"- [ ] {ADA} one\n- [ ] {BOB} two\n"]))
    assert [i.text for i in items(ctx, sid)] == ["one", "two"]
    calls = []
    real = ctx.repo.set_notes_items
    ctx.repo.set_notes_items = lambda *a: calls.append(a) or real(*a)
    typed(ctx, sid, "Ada", frame("f2", ["ins", 0, "# Notes\n"]))  # the lines move: same items
    typed(ctx, sid, "Ada", frame("f3", ["ins", 0, "x"]))  # nothing of the items changes
    assert len(calls) == 1
    typed(ctx, sid, "Ada", frame("f4", ["set", f"- [ ] {BOB} two\n"]))
    assert [i.text for i in items(ctx, sid)] == ["two"]


def test_derived_writes_make_no_new_version(ctx):
    sid = meeting(ctx)
    before = ctx.repo.sync_state(sid)
    links = [(0, 4, "mnemosyne:person/Ada")]
    assert notes_items.apply(ctx, sid, [task(1, "@Ada one", links)], log=False)
    assert [i.text for i in items(ctx, sid)] == ["one"]
    assert ctx.repo.sync_state(sid) == before
    assert history(ctx, sid, "notes_task_added") == []
    assert notes_items.apply(ctx, sid, None) == []  # a sidecar that does not report them
    assert notes_items.apply(ctx, "nope", [task(1, "@Ada one", links)]) == []


def test_save_never_puts_old_items_back(ctx, sidecar):
    sid = meeting(ctx)
    loaded = ctx.repo.get(sid)
    typed(ctx, sid, "Ada", frame("f1", ["set", f"- [ ] {ADA} one\n"]))
    ctx.repo.save(loaded)  # loaded before the fold, without items
    assert [i.text for i in items(ctx, sid)] == ["one"]


def test_a_mark_comment_fold_derives_too(ctx, sidecar):
    sid = meeting(ctx)
    typed(ctx, sid, "Ada", frame("f1", ["set", "hello\n"]))
    ctx.repo.append_notes_ops(sid, [frame("f2", ["set", f"- [ ] {ADA} one\n"])], "u", "Ada", True)
    shared_notes.mark_comment_blocking(ctx, sid, "c1", "0", "2", "u-r", "Reader")
    assert [i.text for i in items(ctx, sid)] == ["one"]
    assert [e["detail"]["by"] for e in history(ctx, sid, "notes_task_added")] == ["Ada"]


def test_task_linking_is_asked_for_new_items_only(ctx, sidecar, monkeypatch):
    from mnemosyne.services import summary_edit

    asked: list[str] = []
    monkeypatch.setattr(summary_edit, "link_quietly", lambda app, sid, *a, **k: asked.append(sid))
    sid = meeting(ctx)
    typed(ctx, sid, "Ada", frame("f1", ["set", f"- [ ] {ADA} one\n"]))
    typed(ctx, sid, "Ada", frame("f2", ["kind", 0, "task_done"]))
    assert asked == [sid]
    ctx.settings.task_linking = "off"
    typed(ctx, sid, "Ada", frame("f3", ["ins", 0, f"- [ ] {BOB} two\n"]))
    assert asked == [sid]


def test_a_failing_derivation_never_fails_the_fold(ctx, sidecar, monkeypatch):
    sid = meeting(ctx)

    def broken(*a):
        raise RuntimeError("boom")

    monkeypatch.setattr(ctx.repo, "part_offsets", broken)
    typed(ctx, sid, "Ada", frame("f1", ["set", f"- [ ] {ADA} one\n"]))
    assert ctx.repo.get(sid).notes.startswith("- [ ]")
    assert items(ctx, sid) == []


# ---- storage -------------------------------------------------------------------------------


def test_an_older_database_gains_the_column(tmp_path):
    path = tmp_path / "old.db"
    repo = SessionRepository(path)
    sid = repo.save(Session(name="Old")).id
    repo._conn.close()
    conn = sqlite3.connect(path)
    conn.execute("ALTER TABLE sessions DROP COLUMN notes_items")
    conn.commit()
    conn.close()

    repo = SessionRepository(path)
    assert repo.get(sid).notes_items == []
    item = NotesItem(id="ab" * 8, line="cd" * 32, text="Send", owner="Ada", due=date(2026, 1, 2))
    assert repo.set_notes_items(sid, [item])
    assert repo.get(sid).notes_items == [item]
    assert repo.notes_items("nope") is None and not repo.set_notes_items("nope", [])
    repo._conn.execute("UPDATE sessions SET notes_items='not json' WHERE id=?", (sid,))
    assert repo.get(sid).notes_items == []


# ---- text cleanup --------------------------------------------------------------------------


def test_separators_a_chip_leaves_at_an_end_go_with_it():
    ada = "mnemosyne:person/Ada"
    cases = [
        ("@Ada: send the deck", [(0, 4, ada)], "send the deck"),
        ("@Ada - send the deck", [(0, 4, ada)], "send the deck"),
        ("Send the deck — @Ada", [(16, 20, ada)], "Send the deck"),
        ("@Ada, @Bob; book it", [(0, 4, ada), (6, 10, "mnemosyne:person/Bob")], "book it"),
        ("@Ada – 1:00 – call", [(0, 4, ada), (7, 11, "mnemosyne:t/0/60")], "call"),
        # Only at an end a chip was cut from: the text's own punctuation stays.
        ("- dash first, @Ada", [(14, 18, ada)], "- dash first"),
        ("Ask @Ada: now;", [(4, 8, ada)], "Ask : now;"),
    ]
    for text, links, want in cases:
        (line,) = notes_items.lines_of([task(0, text, links)], {})
        assert line.text == want, text
    assert notes_items.lines_of([task(0, "@Ada: —", [(0, 4, ada)])], {}) == []  # nothing left


# ---- state written from outside the notes (write_fields, update) ----------------------------


def _one_item(ctx, sid: str, line: str = f"- [ ] {ADA} sends the deck") -> NotesItem:
    typed(ctx, sid, "Ada", frame("f1", ["set", line + "\n"]))
    (item,) = items(ctx, sid)
    return item


def test_a_tick_from_outside_goes_into_the_notes_and_the_next_fold_agrees(ctx, sidecar):
    sid = meeting(ctx)
    item = _one_item(ctx, sid)
    assert notes_items.write_fields(ctx, sid, item.id, done=True, author=("u-cy", "Cy"))
    assert items(ctx, sid)[0].done
    last = ctx.repo.notes_ops(sid)[-1]
    assert (last["author_id"], last["author"]) == ("u-cy", "Cy")
    assert last["frame"].startswith(b"\xde") and b'"kind"' in last["frame"]
    assert ctx.repo.get(sid).notes.startswith("- [x]")  # folded: the notes say it too
    typed(ctx, sid, "Ada", frame("f2", ["ins", 0, "# Plan\n"]))  # the next fold
    (after,) = items(ctx, sid)
    assert after.done and after.id == item.id
    assert history(ctx, sid, "task_done", "task_reopened") == []  # the caller logs, not the fold
    # Reopened the same way; ticking what is so already appends nothing.
    assert notes_items.write_fields(ctx, sid, item.id, done=False)
    n = len(ctx.repo.notes_ops(sid))
    assert notes_items.write_fields(ctx, sid, item.id, done=False)
    assert len(ctx.repo.notes_ops(sid)) == n and not items(ctx, sid)[0].done
    assert "- [ ]" in ctx.repo.get(sid).notes
    assert not notes_items.write_fields(ctx, sid, "0" * 16, done=True)  # no such item


def test_state_writes_are_a_new_version_derived_ones_not(ctx, sidecar):
    sid = meeting(ctx)
    item = _one_item(ctx, sid)
    _, vv = ctx.repo.sync_state(sid)
    seq = ctx.repo.changes_since(0, limit=1000)[1]
    assert notes_items.write_fields(ctx, sid, item.id, follow=True, due=date(2026, 11, 3))
    _, vv2 = ctx.repo.sync_state(sid)
    assert vv2 != vv and ctx.repo.changes_since(seq)[0][0][0] == sid
    stored = items(ctx, sid)[0]
    assert (stored.follow, stored.due) == (True, date(2026, 11, 3))
    assert notes_items.write_fields(ctx, sid, item.id, follow=True)  # the same: no new version
    assert ctx.repo.sync_state(sid)[1] == vv2
    with pytest.raises(ValueError):
        notes_items.write_fields(ctx, sid, item.id, text="no")


def test_a_tick_the_notes_cannot_take_changes_nothing(ctx, sidecar, monkeypatch):
    sid = meeting(ctx)
    item = _one_item(ctx, sid)
    n = len(ctx.repo.notes_ops(sid))
    monkeypatch.setenv("FAKE_NOTES_DOC_FAIL", "1")
    with pytest.raises(shared_notes.NotesDocError):
        notes_items.write_fields(ctx, sid, item.id, done=True, follow=True)
    after = items(ctx, sid)[0]
    assert (after.done, after.follow) == (False, False)
    assert len(ctx.repo.notes_ops(sid)) == n


def test_update_refuses_a_meeting_shared_with_me(ctx, sidecar):
    from mnemosyne import access

    sid = meeting(ctx)
    item = _one_item(ctx, sid)
    with ctx.repo._lock, ctx.repo._conn:
        ctx.repo._conn.execute("UPDATE sessions SET shared_from=? WHERE id=?", ("ab" * 32, sid))
    with pytest.raises(access.Forbidden, match="Tick it in the notes"):
        notes_items.update(ctx, sid, item.id, done=True)
    assert not items(ctx, sid)[0].done
