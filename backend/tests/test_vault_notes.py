"""Edits a user makes in an exported meeting note survive the next export (export/note_merge.py,
services/vault_sync.py write_note). The merge engine is the mnemosyne-link sidecar; here a stand-in
script (tests/fake_link.py) does a three-way merge with difflib, with the sidecar's protocol."""

import hashlib
import os
import time
from datetime import date, datetime

import pytest

from mnemosyne.api.routes.export import _resources, build_exporter, export_session
from mnemosyne.export.note_merge import (
    MergeError,
    lines_both_changed,
    merge,
    normalize,
    restore,
)
from mnemosyne.models.session import ActionItem, Session, SummaryData
from mnemosyne.services import vault_sync
from mnemosyne.services.link import find_binary
from mnemosyne.services.vault_sync import apply_vault_ticks, note_path, write_tick
from tests import fake_link


@pytest.fixture
def sidecar(tmp_path, monkeypatch):
    """A stand-in mnemosyne-link (tests/fake_link.py); the requests it got are in
    sidecar.requests."""
    return fake_link.install(tmp_path, monkeypatch)


# ---- normalize / restore ----------------------------------------------------------------


def test_normalize_opens_every_box_and_drops_done_dates():
    text = "# T\n- [x] One ✅ 2026-10-12\r\n  * [/] Two 📅 2026-10-01\n- [ ] Three\nplain [x]\n"
    norm, boxes = normalize(text)
    assert norm == "# T\n- [ ] One\r\n  * [ ] Two 📅 2026-10-01\n- [ ] Three\nplain [x]\n"
    assert boxes == {
        "- [ ] One": [["x", " ✅ 2026-10-12"]],
        "  * [ ] Two 📅 2026-10-01": [["/", ""]],
        "- [ ] Three": [[" ", ""]],
    }


def test_normalize_keeps_the_order_of_duplicate_lines():
    _, boxes = normalize("- [x] Same\n- [ ] Same\n- [/] Same\n")
    assert boxes == {"- [ ] Same": [["x", ""], [" ", ""], ["/", ""]]}


def test_restore_ticks_on_both_sides_stay_one_x():
    app_norm, app = normalize("- [x] A\n- [ ] B\n")
    vault_norm, vault = normalize("- [x] A\n- [x] B\n")
    assert restore("- [ ] A\n- [ ] B\n", app, vault) == "- [x] A\n- [ ] B\n"
    assert vault_norm == app_norm


def test_restore_keeps_the_status_of_the_vault_when_the_app_has_it_open():
    _, app = normalize("- [ ] A\n- [x] B\n- [ ] C\n")
    _, vault = normalize("- [/] A\n- [-] B ❌ 2026-10-12\n- [x] D ✅ 2026-10-01\n")
    merged = "- [ ] A\n- [ ] B\n- [ ] C\n- [ ] D\n- [ ] E\n"
    assert restore(merged, app, vault) == (
        "- [/] A\n"  # Tasks' In Progress: kept
        "- [x] B\n"  # the app has it done: wins over the vault's other status
        "- [ ] C\n"
        "- [x] D ✅ 2026-10-01\n"  # only in the vault: its status and its date
        "- [ ] E\n"  # nowhere: open
    )


def test_restore_keeps_obsidians_done_date_and_never_invents_one():
    _, app = normalize("- [x] A ✅ 2026-10-12\n- [x] B\n")
    _, vault = normalize("- [ ] A\n- [x] B ✅ 2026-10-03\n")
    assert restore("- [ ] A\n- [ ] B\n", app, vault) == "- [x] A\n- [x] B ✅ 2026-10-03\n"


def test_restore_keeps_line_endings():
    _, app = normalize("- [x] A\r\n- [ ] B\r\n")
    _, vault = normalize("- [ ] A ✅ 2026-10-12\r\n")
    assert restore("- [ ] A\r\n- [ ] B\r\n", app, vault) == "- [x] A\r\n- [ ] B\r\n"
    _, vault = normalize("- [x] A ✅ 2026-10-12\r\n")
    assert restore("- [ ] A\r\n- [ ] B", app, vault) == "- [x] A ✅ 2026-10-12\r\n- [ ] B"


def test_restore_duplicate_lines_by_occurrence():
    _, app = normalize("- [x] Same\n- [ ] Same\n")
    assert restore("- [ ] Same\n- [ ] Same\n", app, {}) == "- [x] Same\n- [ ] Same\n"


def test_lines_both_changed():
    assert lines_both_changed("a\nmine and yours\n\nb\n", "a\nyours\nb\n", "a\nmine\nb\n") == 1
    assert lines_both_changed("a\nb\n", "a\nb\n", "a\nb\n") == 0


def test_merge_speaks_the_sidecar_protocol(sidecar):
    binary = find_binary()
    history, text = merge(binary, None, "a\nb\n", None)
    assert text == "a\nb\n"
    history, text = merge(binary, history, "a\nb\nc\n", "x\na\nb\n")
    assert text == "x\na\nb\nc\n"
    assert sidecar.requests[0] == {"history": None, "app": "a\nb\n", "vault": None}


def test_merge_errors(sidecar, monkeypatch):
    monkeypatch.setenv("FAKE_MERGE_FAIL", "1")
    with pytest.raises(MergeError, match="boom"):
        merge(find_binary(), None, "a\n", None)
    with pytest.raises(MergeError):
        merge("/nonexistent/mnemosyne-link", None, "a\n", None)


# ---- exports ------------------------------------------------------------------------------

ITEMS = [
    ActionItem(id="aaaa0001", text="Write the guide", owner="Corey Petty", due=date(2026, 10, 1)),
    ActionItem(id="aaaa0002", text="Send the deck", owner="Me"),
    ActionItem(id="aaaa0003", text="Fix the build", owner="Ana"),
    ActionItem(id="aaaa0004", text="Ask about it", owner="SPEAKER_12", follow=True),
]
DECK = " ^m-aaaa0002"  # the block id of "Send the deck" (export/templates.py)


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


def _meeting(ctx, vault, done=(), export=True) -> Session:
    items = [i.model_copy(update={"done": n in done}) for n, i in enumerate(ITEMS)]
    session = Session(
        name="Sync",
        created_at=datetime(2026, 10, 12, 9),
        summary="First summary.",
        summary_data=SummaryData(action_items=items),
    )
    ctx.repo.save(session)
    session = ctx.sessions.get_session(session.id)
    if export:
        export_session(ctx, session, str(vault))
    return session


def _export(ctx, session, vault):
    return export_session(ctx, ctx.sessions.get_session(session.id), str(vault))


def _render(ctx, session, vault) -> str:
    session = ctx.sessions.get_session(session.id)
    exporter = build_exporter(ctx, str(vault), session)
    return exporter.render(session, _resources(ctx, session, exporter))


def _change(ctx, session, summary=None, extra_item=None):
    s = ctx.sessions.get_session(session.id)
    if summary is not None:
        s.summary = summary
    if extra_item is not None:
        s.summary_data.action_items.append(extra_item)
    ctx.repo.save(s)


def _edit(path, old, new):
    text = path.read_text(encoding="utf-8")
    assert old in text, old
    path.write_text(text.replace(old, new, 1), encoding="utf-8")


def _events(ctx, session, kind):
    return [e for e in ctx.repo.events(session.id) if e["kind"] == kind]


def test_the_first_export_writes_the_render_and_starts_a_record(ctx, vault, sidecar):
    s = _meeting(ctx, vault)
    path = note_path(ctx, s, str(vault))
    assert path.read_text(encoding="utf-8") == _render(ctx, s, vault)
    record = ctx.repo.get_vault_note(s.id)
    assert record["path"] == str(path)
    assert record["written_sha"] == hashlib.sha256(path.read_bytes()).hexdigest()
    assert record["boxes"][f"- [ ] Send the deck ([[Me]]){DECK}"] == [[" ", ""]]
    assert sidecar.requests[0]["history"] is None and sidecar.requests[0]["vault"] is None
    assert ctx.vault_seen[str(path)] == path.stat().st_mtime_ns


def test_an_unchanged_vault_exports_the_render(ctx, vault, sidecar):
    s = _meeting(ctx, vault)
    path = note_path(ctx, s, str(vault))
    _change(ctx, s, summary="Second summary.")
    _export(ctx, s, vault)
    assert "Second summary." in path.read_text(encoding="utf-8")
    assert path.read_text(encoding="utf-8") == _render(ctx, s, vault)
    assert sidecar.requests[-1]["vault"] is None
    assert _events(ctx, s, "note_merged") == []


def test_user_edits_survive_a_changed_summary(ctx, vault, sidecar):
    s = _meeting(ctx, vault)
    path = note_path(ctx, s, str(vault))
    _edit(path, "## Action Items\n", "## Action Items\n\nMy own paragraph.\n")
    _edit(path, "- [[Ana]]: Fix the build", "- [[Bob]]: Fix the build")
    _change(
        ctx,
        s,
        summary="Second summary.",
        extra_item=ActionItem(text="Book flights", owner="Corey Petty"),
    )
    _export(ctx, s, vault)
    text = path.read_text(encoding="utf-8")
    assert "My own paragraph." in text
    assert "- [[Bob]]: Fix the build" in text and "[[Ana]]: Fix the build" not in text
    assert "Second summary." in text and "First summary." not in text
    assert "- [ ] Book flights ([[Corey Petty]])" in text
    assert [e["detail"] for e in _events(ctx, s, "note_merged")] == [{"both": 0}]
    # The next export finds the vault as we left it.
    _export(ctx, s, vault)
    assert path.read_text(encoding="utf-8") == text
    assert sidecar.requests[-1]["vault"] is None
    assert len(_events(ctx, s, "note_merged")) == 1


def test_ticks_on_both_sides_do_not_double(ctx, vault, sidecar):
    s = _meeting(ctx, vault)
    path = note_path(ctx, s, str(vault))
    _edit(path, "- [ ] Send the deck ([[Me]])", "- [x] Send the deck ([[Me]]) ✅ 2026-10-03")
    _edit(path, "## Action Items\n", "## Action Items\n\nMy own paragraph.\n")
    _export(ctx, s, vault)  # the tick is taken first (before_export), then merged
    text = path.read_text(encoding="utf-8")
    assert f"- [x] Send the deck ([[Me]]) ✅ 2026-10-03{DECK}\n" in text
    assert "[xx]" not in text and "My own paragraph." in text
    assert ctx.sessions.get_session(s.id).summary_data.action_items[1].done is True


def test_tasks_statuses_survive(ctx, vault, sidecar):
    s = _meeting(ctx, vault)
    path = note_path(ctx, s, str(vault))
    _edit(path, "- [ ] Send the deck ([[Me]])", "- [/] Send the deck ([[Me]])")
    _change(ctx, s, summary="Second summary.")
    _export(ctx, s, vault)
    assert f"- [/] Send the deck ([[Me]]){DECK}\n" in path.read_text(encoding="utf-8")


def test_a_note_without_a_sidecar_is_written_as_before(ctx, vault, tmp_path):
    s = _meeting(ctx, vault)  # MNEMOSYNE_LINK_BIN points nowhere
    path = note_path(ctx, s, str(vault))
    assert ctx.repo.get_vault_note(s.id) is None
    _edit(path, "## Action Items\n", "## Action Items\n\nMy own paragraph.\n")
    _export(ctx, s, vault)
    assert path.read_text(encoding="utf-8") == _render(ctx, s, vault)


def test_a_failing_sidecar_forgets_the_record(ctx, vault, sidecar, monkeypatch):
    s = _meeting(ctx, vault)
    path = note_path(ctx, s, str(vault))
    assert ctx.repo.get_vault_note(s.id) is not None
    monkeypatch.setenv("FAKE_MERGE_FAIL", "1")
    _edit(path, "## Action Items\n", "## Action Items\n\nMy own paragraph.\n")
    _change(ctx, s, summary="Second summary.")
    _export(ctx, s, vault)
    assert path.read_text(encoding="utf-8") == _render(ctx, s, vault)  # overwritten, as before
    assert ctx.repo.get_vault_note(s.id) is None
    # Working again: a new history from what is there.
    monkeypatch.delenv("FAKE_MERGE_FAIL")
    _export(ctx, s, vault)
    assert ctx.repo.get_vault_note(s.id) is not None


def test_a_note_that_is_not_ours_is_overwritten_without_a_record(ctx, vault, sidecar):
    s = _meeting(ctx, vault, export=False)
    path = note_path(ctx, s, str(vault))
    path.parent.mkdir(parents=True)
    path.write_text("---\ntitle: Sync\n---\n\nMy own note.\n", encoding="utf-8")
    _export(ctx, s, vault)
    assert path.read_text(encoding="utf-8") == _render(ctx, s, vault)
    assert ctx.repo.get_vault_note(s.id) is None
    assert not [r for r in sidecar.requests if isinstance(r, dict)]  # no note merge


def test_a_note_moved_to_another_folder_starts_again(ctx, vault, sidecar):
    s = _meeting(ctx, vault)
    old = ctx.repo.get_vault_note(s.id)["path"]
    ctx.settings.obsidian_subfolder = "elsewhere"
    path = _export(ctx, s, vault)
    record = ctx.repo.get_vault_note(s.id)
    assert str(path) != old and record["path"] == str(path)
    assert sidecar.requests[-1]["history"] is None


def test_a_missing_note_is_written_again(ctx, vault, sidecar):
    s = _meeting(ctx, vault)
    path = note_path(ctx, s, str(vault))
    path.unlink()
    _export(ctx, s, vault)
    assert path.read_text(encoding="utf-8") == _render(ctx, s, vault)
    assert _events(ctx, s, "note_merged") == []


def test_the_route_exports_through_the_merge(client, ctx, vault, sidecar):
    s = _meeting(ctx, vault)
    path = note_path(ctx, s, str(vault))
    _edit(path, "## Action Items\n", "## Action Items\n\nMy own paragraph.\n")
    _change(ctx, s, summary="Second summary.")
    assert client.post(f"/api/sessions/{s.id}/export/obsidian").status_code == 200
    text = path.read_text(encoding="utf-8")
    assert "My own paragraph." in text and "Second summary." in text
    assert not list(path.parent.glob(".*.mnemosyne"))


# ---- ticks --------------------------------------------------------------------------------


def _done(ctx, session):
    return [i.done for i in ctx.sessions.get_session(session.id).summary_data.action_items]


def test_the_record_decides_a_stale_note(ctx, vault, sidecar):
    s = _meeting(ctx, vault)
    path = note_path(ctx, s, str(vault))
    t = time.time() + 3600  # written "after" the meeting changed: the mtime rule would reopen
    os.utime(path, (t, t))
    ctx.repo.set_action_item_done(s.id, 0, True)
    assert apply_vault_ticks(ctx, s, str(vault)) is False  # the note says what we wrote
    assert _done(ctx, s)[0] is True


def test_an_untick_in_the_vault_still_reopens(ctx, vault, sidecar):
    s = _meeting(ctx, vault, done={0})
    path = note_path(ctx, s, str(vault))
    assert "- [x] Write the guide" in path.read_text(encoding="utf-8")
    t = time.time() - 3600  # older than the meeting: no matter, the box differs from the record
    _edit(path, "- [x] Write the guide", "- [ ] Write the guide")
    os.utime(path, (t, t))
    assert apply_vault_ticks(ctx, s, str(vault)) is True
    assert _done(ctx, s)[0] is False


def test_a_tick_in_the_vault_is_taken_and_kept(ctx, vault, sidecar):
    s = _meeting(ctx, vault)
    path = note_path(ctx, s, str(vault))
    _edit(path, "- [ ] Send the deck ([[Me]])", "- [x] Send the deck ([[Me]]) ✅ 2026-10-03")
    assert apply_vault_ticks(ctx, s, str(vault)) is True
    _export(ctx, s, vault)
    assert f"- [x] Send the deck ([[Me]]) ✅ 2026-10-03{DECK}\n" in path.read_text(encoding="utf-8")
    assert apply_vault_ticks(ctx, s, str(vault)) is False  # now it is what we wrote


def test_write_tick_keeps_the_record_in_sync(client, ctx, vault, sidecar):
    s = _meeting(ctx, vault)
    path = note_path(ctx, s, str(vault))
    r = client.patch(f"/api/sessions/{s.id}/action-items/1", json={"done": True})
    assert r.status_code == 200
    today = date.today().isoformat()
    assert f"- [x] Send the deck ([[Me]]) ✅ {today}{DECK}\n" in path.read_text(encoding="utf-8")
    record = ctx.repo.get_vault_note(s.id)
    assert record["written_sha"] == hashlib.sha256(path.read_bytes()).hexdigest()
    assert record["boxes"][f"- [ ] Send the deck ([[Me]]){DECK}"] == [["x", f" ✅ {today}"]]
    # Nothing for the vault to report: no merge, the tick stays.
    _change(ctx, s, summary="Second summary.")
    _export(ctx, s, vault)
    assert sidecar.requests[-1]["vault"] is None
    assert _events(ctx, s, "note_merged") == []
    assert f"- [x] Send the deck ([[Me]]) ✅ {today}{DECK}\n" in path.read_text(encoding="utf-8")
    assert apply_vault_ticks(ctx, s, str(vault)) is False


def test_write_tick_does_not_hide_the_users_edits(ctx, vault, sidecar):
    s = _meeting(ctx, vault)
    path = note_path(ctx, s, str(vault))
    _edit(path, "## Action Items\n", "## Action Items\n\nMy own paragraph.\n")
    before = ctx.repo.get_vault_note(s.id)
    assert write_tick(ctx, s, 1, True) is True
    assert ctx.repo.get_vault_note(s.id)["written_sha"] == before["written_sha"]  # still "edited"
    ctx.repo.set_action_item_done(s.id, 1, True)
    _change(ctx, s, summary="Second summary.")
    _export(ctx, s, vault)
    text = path.read_text(encoding="utf-8")
    assert "My own paragraph." in text and "- [x] Send the deck ([[Me]])" in text
    assert "Second summary." in text


def test_the_mtime_rule_stays_for_notes_without_a_record(ctx, vault):
    s = _meeting(ctx, vault)  # no sidecar: no record
    assert ctx.repo.get_vault_note(s.id) is None
    path = note_path(ctx, s, str(vault))
    t = time.time() - 3600
    os.utime(path, (t, t))
    ctx.repo.set_action_item_done(s.id, 0, True)
    assert apply_vault_ticks(ctx, s, str(vault)) is False
    assert _done(ctx, s)[0] is True


def test_deleting_a_meeting_removes_its_record(ctx, vault, sidecar):
    s = _meeting(ctx, vault)
    assert ctx.repo._conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    assert ctx.repo.get_vault_note(s.id) is not None
    assert ctx.repo.delete(s.id) is True
    assert ctx.repo.get_vault_note(s.id) is None
    assert ctx.repo._conn.execute("SELECT count(*) FROM vault_notes").fetchone()[0] == 0


def test_sync_vault_uses_the_record(ctx, vault, sidecar):
    s = _meeting(ctx, vault)
    path = note_path(ctx, s, str(vault))
    _edit(path, "- [ ] Send the deck ([[Me]])", "- [x] Send the deck ([[Me]])")
    assert vault_sync.sync_vault(ctx) == 1
    assert _done(ctx, s)[1] is True


# ---- the real sidecar ---------------------------------------------------------------------


@pytest.fixture
def real_sidecar(monkeypatch):
    monkeypatch.delenv("MNEMOSYNE_LINK_BIN", raising=False)
    binary = find_binary()
    if binary is None:
        pytest.skip("no mnemosyne-link binary")
    try:
        merge(binary, None, "a\n", None)
    except MergeError:
        pytest.skip("this mnemosyne-link has no `notes merge`")
    return binary


def test_with_the_real_sidecar(ctx, vault, real_sidecar):
    s = _meeting(ctx, vault)
    path = note_path(ctx, s, str(vault))
    assert ctx.repo.get_vault_note(s.id) is not None
    _edit(path, "## Action Items\n", "## Action Items\n\nMy own paragraph.\n")
    _edit(path, "- [ ] Send the deck ([[Me]])", "- [x] Send the deck ([[Me]]) ✅ 2026-10-03")
    _change(ctx, s, summary="Second summary.")
    _export(ctx, s, vault)
    text = path.read_text(encoding="utf-8")
    assert "My own paragraph." in text and "Second summary." in text
    assert f"- [x] Send the deck ([[Me]]) ✅ 2026-10-03{DECK}\n" in text and "[xx]" not in text
    assert _events(ctx, s, "note_merged")


def test_with_the_real_sidecar_earlier_edits_stay(ctx, vault, real_sidecar):
    s = _meeting(ctx, vault)
    path = note_path(ctx, s, str(vault))
    _edit(path, "## Action Items\n", "## Action Items\n\nMy own paragraph.\n")
    _change(ctx, s, summary="Second summary.")
    _export(ctx, s, vault)
    _change(ctx, s, summary="Third summary.")
    _export(ctx, s, vault)  # the vault unchanged since the last export
    text = path.read_text(encoding="utf-8")
    assert "My own paragraph." in text and "Third summary." in text
