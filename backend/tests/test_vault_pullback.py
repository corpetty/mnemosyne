"""Edits made in Obsidian to the summary's sections of a meeting note flow back into the summary
(services/vault_sync.py apply_vault_edits, export/note_parse.py), and those sections are
rendered whole on export instead of being merged as text, so nothing doubles. The merge engine is
the stand-in of tests/fake_link.py, plus the real sidecar when it is built."""

from datetime import date, datetime

import pytest

from mnemosyne.export.note_merge import MergeError, merge, normalize, restore
from mnemosyne.export.note_parse import PLACEHOLDER, cut, parse, splice
from mnemosyne.export.templates import render_meeting_note
from mnemosyne.models.session import ActionItem, Session, SummaryData
from mnemosyne.services import summary_doc, vault_sync
from mnemosyne.services.link import find_binary
from mnemosyne.services.vault_sync import apply_vault_edits, note_path
from tests import fake_link
from tests.test_vault_notes import DECK, _change, _edit, _events, _export, _meeting, _render


@pytest.fixture
def sidecar(tmp_path, monkeypatch):
    return fake_link.install(tmp_path, monkeypatch)


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


def _data(ctx, s) -> SummaryData:
    return ctx.sessions.get_session(s.id).summary_data


def _item(ctx, s, item_id):
    return next(a for a in _data(ctx, s).action_items if a.id == item_id)


def _text(path) -> str:
    return path.read_text(encoding="utf-8")


def _with_decisions(ctx, vault):
    """A meeting with decisions and open questions too, exported once."""
    s = _meeting(ctx, vault, export=False)
    full = ctx.sessions.get_session(s.id)
    full.summary_data.decisions = ["Ship on Friday", "Keep the old API"]
    full.summary_data.decision_ids = ["dddd0001", "dddd0002"]
    full.summary_data.open_questions = ["Who owns the docs?"]
    full.summary_data.question_ids = ["eeee0001"]
    ctx.repo.save(full)
    _export(ctx, s, vault)
    return s


# ---- parsing -------------------------------------------------------------------------------


def test_parse_reads_the_rendering_back():
    data = SummaryData(
        decisions=["Use [[Postgres]] for it"],
        decision_ids=["dddd0001"],
        open_questions=["Why (really)?"],
        question_ids=["eeee0001"],
    )
    data.action_items = [
        *SummaryData.model_validate(
            {
                "action_items": [
                    {"id": "aaaa0001", "text": "Write it", "owner": "Ana", "due": "2026-10-20"},
                    {"id": "aaaa0002", "text": "Review the doc (draft)"},
                    {"id": "aaaa0003", "text": "Fix the build", "owner": "SPEAKER_3"},
                ]
            }
        ).action_items
    ]
    note = render_meeting_note(
        "T",
        datetime(2026, 10, 12, 9),
        [],
        [],
        "Body line one.\n\n## A body heading\nMore.",
        "",
        summary_data=data,
        is_mine=lambda item: item.owner != "SPEAKER_3",
        annex={"aaaa0001": ["  - a sub point"], "@others": ["Theirs:"]},
    )
    known = {"aaaa0002": (None, "Review the doc (draft)"), "aaaa0003": ("SPEAKER_3", "")}
    parsed = parse(note, known, {"## A body heading"})
    assert parsed.body == "Body line one.\n\n## A body heading\nMore."
    [d] = parsed.sections["decisions"]
    assert (d.id, d.text) == ("dddd0001", "Use Postgres for it")
    [q] = parsed.sections["open_questions"]
    assert (q.id, q.text) == ("eeee0001", "Why (really)?")
    a, b, c = parsed.sections["action_items"]
    assert (a.id, a.text, a.owner, a.due, a.box) == (
        "aaaa0001",
        "Write it",
        "Ana",
        "2026-10-20",
        True,
    )
    assert a.annex == ["  - a sub point"]
    assert (b.text, b.owner) == ("Review the doc (draft)", None)  # written without an owner
    assert (c.text, c.owner, c.others) == ("Fix the build", "SPEAKER_3", True)
    assert parsed.heads == {"others": ["Theirs:"]}


def test_cut_and_splice():
    text = "# T\n\n## Summary\n\nBody.\n\n## Decisions\n\n- A ^m-1\n\n## Notes\n\nMine.\n"
    rest, sections = cut(text)
    assert rest == (
        f"# T\n\n{PLACEHOLDER.format('summary')}\n{PLACEHOLDER.format('decisions')}\n"
        "## Notes\n\nMine.\n"
    )
    assert sections["decisions"] == "## Decisions\n\n- A ^m-1\n\n"
    assert splice(rest, sections) == text
    # A copy of a placeholder goes; one without a section too.
    twice = rest.replace("## Notes", PLACEHOLDER.format("decisions") + "\n## Notes")
    assert splice(twice, sections) == text
    assert splice(rest, {}) == "# T\n\n## Notes\n\nMine.\n"


def test_restore_puts_the_done_date_before_the_block_id():
    _, app = normalize("- [x] A ^m-aaaa0001\n")
    _, vault = normalize("- [x] A ^m-aaaa0001 ✅ 2026-10-03\n")
    assert restore("- [ ] A ^m-aaaa0001\n", app, vault) == "- [x] A ✅ 2026-10-03 ^m-aaaa0001\n"


# ---- pull-back -----------------------------------------------------------------------------


def test_a_reworded_item_reaches_the_summary_and_does_not_double(ctx, vault, sidecar):
    s = _meeting(ctx, vault)
    path = note_path(ctx, s, str(vault))
    _edit(path, "Send the deck ([[Me]])", "Send the final deck ([[Me]])")
    _export(ctx, s, vault)
    item = _item(ctx, s, "aaaa0002")
    assert item.text == "Send the final deck" and item.owner == "Me"
    assert _data(ctx, s).edited_by == "Obsidian"
    text = _text(path)
    assert text.count("Send the final deck") == 1 and "Send the deck" not in text
    assert f"- [ ] Send the final deck ([[Me]]){DECK}\n" in text
    assert [e["detail"] for e in _events(ctx, s, "note_edits_taken")] == [
        {"items": 1, "decisions": 0, "questions": 0, "body": False}
    ]
    # Again, and after a change in the app: the same, nothing taken twice.
    _export(ctx, s, vault)
    assert _text(path) == text
    _change(ctx, s, summary="Second summary.")
    _export(ctx, s, vault)
    text = _text(path)
    assert text.count("Send the final deck") == 1 and "Second summary." in text
    assert len(_events(ctx, s, "note_edits_taken")) == 1


def test_with_the_real_sidecar_a_reworded_item_does_not_double(ctx, vault, real_sidecar):
    s = _meeting(ctx, vault)
    path = note_path(ctx, s, str(vault))
    _edit(path, "Send the deck ([[Me]])", "Send the final deck ([[Me]])")
    _edit(path, "## Action Items\n", "## Action Items\n\nMy own paragraph.\n")
    _export(ctx, s, vault)
    assert _item(ctx, s, "aaaa0002").text == "Send the final deck"
    _change(ctx, s, summary="Second summary.")
    _export(ctx, s, vault)
    _export(ctx, s, vault)
    text = _text(path)
    assert text.count("Send the final deck") == 1 and "Send the deck" not in text
    assert text.count("My own paragraph.") == 1 and "Second summary." in text
    assert _text(path) == _render(ctx, s, vault)


def test_an_owner_changed_in_the_vault(ctx, vault, sidecar):
    s = _meeting(ctx, vault)
    path = note_path(ctx, s, str(vault))
    _edit(path, "Send the deck ([[Me]])", "Send the deck ([[Bob]])")
    _edit(path, "- [[Ana]]: Fix the build", "- [[Cleo]]: Fix the build")
    _export(ctx, s, vault)
    assert _item(ctx, s, "aaaa0002").owner == "Bob"
    assert _item(ctx, s, "aaaa0003").owner == "Cleo"
    text = _text(path)
    assert "- [[Bob]]: Send the deck ^m-aaaa0002\n" in text  # someone else's: under Others
    assert text.count("Send the deck") == 1 and "[[Me]]" not in text


def test_a_due_date_changed_in_the_vault(ctx, vault, sidecar):
    s = _meeting(ctx, vault)
    path = note_path(ctx, s, str(vault))
    _edit(path, "📅 2026-10-01", "📅 2026-10-09")
    _export(ctx, s, vault)
    assert _item(ctx, s, "aaaa0001").due == date(2026, 10, 9)


def test_a_new_bullet_becomes_an_item(ctx, vault, sidecar):
    s = _meeting(ctx, vault)
    path = note_path(ctx, s, str(vault))
    _edit(
        path,
        f"Send the deck ([[Me]]){DECK}\n",
        f"Send the deck ([[Me]]){DECK}\n- [ ] Book the room ([[Corey Petty]]) 📅 2026-10-20\n",
    )
    _export(ctx, s, vault)
    items = _data(ctx, s).action_items
    assert [a.text for a in items][:3] == ["Write the guide", "Send the deck", "Book the room"]
    new = items[2]
    assert new.owner == "Corey Petty" and new.due == date(2026, 10, 20) and new.id
    text = _text(path)
    assert f"- [ ] Book the room ([[Corey Petty]]) 📅 2026-10-20 ^m-{new.id}\n" in text
    assert text.count("Book the room") == 1
    _export(ctx, s, vault)
    assert len(_data(ctx, s).action_items) == len(items)


def test_a_new_decision_and_question(ctx, vault, sidecar):
    s = _with_decisions(ctx, vault)
    path = note_path(ctx, s, str(vault))
    _edit(path, "- Ship on Friday ^m-dddd0001\n", "- Ship on Friday ^m-dddd0001\n- Freeze Monday\n")
    _edit(path, "- Who owns the docs? ^m-eeee0001\n", "- Who owns the docs? ^m-eeee0001\n- When?\n")
    _export(ctx, s, vault)
    data = _data(ctx, s)
    assert data.decisions == ["Ship on Friday", "Freeze Monday", "Keep the old API"]
    assert data.decision_at == [None, None, None] and all(data.decision_ids)
    assert data.open_questions == ["Who owns the docs?", "When?"]
    text = _text(path)
    assert f"- Freeze Monday ^m-{data.decision_ids[1]}\n" in text
    assert text.count("Freeze Monday") == 1 and text.count("When?") == 1
    assert [e["detail"] for e in _events(ctx, s, "note_edits_taken")] == [
        {"items": 0, "decisions": 1, "questions": 1, "body": False}
    ]


def test_a_deleted_bullet_is_tombstoned(ctx, vault, sidecar):
    s = _with_decisions(ctx, vault)
    path = note_path(ctx, s, str(vault))
    _edit(path, "- [[Ana]]: Fix the build ^m-aaaa0003\n", "")
    _edit(path, "- Keep the old API ^m-dddd0002\n", "")
    _export(ctx, s, vault)
    data = _data(ctx, s)
    assert "aaaa0003" not in [a.id for a in data.action_items]
    assert data.decisions == ["Ship on Friday"]
    doc = summary_doc.ensure_doc(ctx, s.id)
    assert {"aaaa0003", "dddd0002"} <= set(doc.tombstones)
    text = _text(path)
    assert "Fix the build" not in text and "Keep the old API" not in text


def test_a_removed_section_changes_nothing(ctx, vault, sidecar):
    s = _with_decisions(ctx, vault)
    path = note_path(ctx, s, str(vault))
    _edit(
        path, "## Decisions\n\n- Ship on Friday ^m-dddd0001\n- Keep the old API ^m-dddd0002\n\n", ""
    )
    assert apply_vault_edits(ctx, ctx.sessions.get_session(s.id), str(vault)) is False
    assert _data(ctx, s).decisions == ["Ship on Friday", "Keep the old API"]


def test_a_body_paragraph_edit_flows_back(ctx, vault, sidecar):
    s = _meeting(ctx, vault)
    path = note_path(ctx, s, str(vault))
    _edit(path, "First summary.\n", "First summary, corrected.\n\nA second paragraph.\n")
    _export(ctx, s, vault)
    session = ctx.sessions.get_session(s.id)
    assert session.summary == "First summary, corrected.\n\nA second paragraph."
    text = _text(path)
    assert text.count("A second paragraph.") == 1 and text.count("corrected") == 1
    assert _events(ctx, s, "note_edits_taken")[0]["detail"]["body"] is True


def test_annex_lines_survive_two_exports(ctx, vault, sidecar):
    s = _with_decisions(ctx, vault)
    path = note_path(ctx, s, str(vault))
    _edit(
        path, f"Send the deck ([[Me]]){DECK}\n", f"Send the deck ([[Me]]){DECK}\n  - slides 1-4\n"
    )
    _edit(path, "### Others\n", "### Others\n\nWaiting on these.\n")
    _edit(path, "## Decisions\n", "## Decisions\n\nAgreed by everyone:\n")
    _change(ctx, s, summary="Second summary.")
    _export(ctx, s, vault)
    _change(ctx, s, summary="Third summary.")
    _export(ctx, s, vault)
    text = _text(path)
    assert f"Send the deck ([[Me]]){DECK}\n  - slides 1-4\n" in text
    assert "### Others\n\nWaiting on these.\n\n- [[Ana]]: Fix the build" in text
    assert "## Decisions\n\nAgreed by everyone:\n\n- Ship on Friday" in text
    for line in ("slides 1-4", "Waiting on these.", "Agreed by everyone:"):
        assert text.count(line) == 1
    assert "Third summary." in text
    assert _events(ctx, s, "note_edits_taken") == []  # nothing for the summary itself
    assert len(_data(ctx, s).action_items) == 4


def test_a_block_changed_on_both_sides_is_merged(ctx, vault, sidecar):
    s = _meeting(ctx, vault)
    path = note_path(ctx, s, str(vault))
    _edit(path, "Send the deck ([[Me]])", "Send the deck today ([[Me]])")
    data = _data(ctx, s).model_copy(deep=True)
    data.action_items[1].text = "Send the slide deck"
    out, body, _ = summary_doc.apply_human(ctx, s.id, data, "First summary.", by="Corey")
    ctx.repo.update_fields(s.id, summary=body, summary_data=out)
    _export(ctx, s, vault)
    assert _item(ctx, s, "aaaa0002").text == "Send the slide deck today"
    text = _text(path)
    assert f"- [ ] Send the slide deck today ([[Me]]){DECK}\n" in text
    assert text.count("deck") == 1
    merges = [x for r in sidecar.requests if isinstance(r, list) for x in r]
    wanted = {"base": "Send the deck", "app": "Send the slide deck", "vault": "Send the deck today"}
    assert wanted in merges  # a one-off three-way merge


def test_a_redacted_export_pulls_nothing_redacted_back(ctx, vault, sidecar):
    assert ctx.settings.redact_exports is True
    s = _meeting(ctx, vault, export=False)
    full = ctx.sessions.get_session(s.id)
    full.summary = "Wire it to account 123456789012."
    full.summary_data.action_items[1].text = "Send the deck to card 4111 1111 1111 1111"
    ctx.repo.save(full)
    _export(ctx, s, vault)
    path = note_path(ctx, s, str(vault))
    text = _text(path)
    assert "[account ••9012]" in text and "[card ••1111]" in text
    _edit(path, "Wire it to account [account ••9012].", "Wire it to [account ••9012] now.")
    _edit(path, "Send the deck to card [card ••1111]", "Send the deck to [card ••1111] today")
    _edit(path, "### Others\n", "- [ ] Pay [account ••9012]\n\n### Others\n")
    _export(ctx, s, vault)
    session = ctx.sessions.get_session(s.id)
    assert session.summary == "Wire it to account 123456789012."
    assert _item(ctx, s, "aaaa0002").text == "Send the deck to card 4111 1111 1111 1111"
    assert len(session.summary_data.action_items) == 4
    assert _events(ctx, s, "note_edits_taken") == []
    assert "- [ ] Pay [account ••9012]" in _text(path)  # kept in the note, not taken
    # A line redaction did not touch is still taken.
    _edit(path, "Write the guide", "Write the user guide")
    _export(ctx, s, vault)
    assert _item(ctx, s, "aaaa0001").text == "Write the user guide"


def test_the_sync_pass_takes_edits_and_does_not_export(ctx, vault, sidecar):
    s = _meeting(ctx, vault)
    path = note_path(ctx, s, str(vault))
    _edit(path, "Send the deck ([[Me]])", "Send the final deck ([[Me]])")
    _edit(path, f"{DECK}\n", f"{DECK}\n- [ ] Book the room\n")
    before = path.read_bytes()
    assert vault_sync.sync_vault(ctx) == 1
    assert path.read_bytes() == before
    items = _data(ctx, s).action_items
    assert [a.text for a in items][1:3] == ["Send the final deck", "Book the room"]
    # The note changes again elsewhere: what was taken is not taken twice.
    _edit(path, "## Action Items\n", "## Action Items\n\nMy own paragraph.\n")
    assert vault_sync.sync_vault(ctx) == 0
    assert len(_data(ctx, s).action_items) == len(items)
    assert len(_events(ctx, s, "note_edits_taken")) == 1
    # The next export writes them with their ids, once.
    _export(ctx, s, vault)
    text = _text(path)
    assert text.count("Book the room") == 1 and text.count("Send the final deck") == 1
    assert "My own paragraph." in text


@pytest.mark.parametrize(
    "ticked",
    [
        f"- [x] Send the deck ([[Me]]) ✅ 2026-10-03{DECK}",  # as Obsidian Tasks writes it
        f"- [x] Send the deck ([[Me]]){DECK} ✅ 2026-10-03",  # typed after the block id
    ],
)
def test_the_done_date_stays_before_the_block_id_after_a_merge(ctx, vault, sidecar, ticked):
    s = _meeting(ctx, vault)
    path = note_path(ctx, s, str(vault))
    _edit(path, f"- [ ] Send the deck ([[Me]]){DECK}", ticked)
    _edit(path, "## Action Items\n", "## Action Items\n\nMy own paragraph.\n")
    _change(ctx, s, summary="Second summary.")
    _export(ctx, s, vault)
    text = _text(path)
    assert f"- [x] Send the deck ([[Me]]) ✅ 2026-10-03{DECK}\n" in text
    assert _item(ctx, s, "aaaa0002").done is True


def test_without_a_record_of_the_last_export_nothing_is_taken(ctx, vault, sidecar):
    s = _meeting(ctx, vault)
    path = note_path(ctx, s, str(vault))
    vault_sync._update_vault(ctx, s.id, lambda v: v.__setitem__("written", {}))
    _edit(path, "Send the deck ([[Me]])", "Send the final deck ([[Me]])")
    _edit(path, f"{DECK}\n", f"{DECK}\n  - a sub point\n")
    assert apply_vault_edits(ctx, ctx.sessions.get_session(s.id), str(vault)) is False
    assert _item(ctx, s, "aaaa0002").text == "Send the deck"
    assert vault_sync.vault_annex(ctx, s.id) == {"aaaa0002": ["  - a sub point"]}


def test_a_failing_sidecar_takes_nothing(ctx, vault, sidecar, monkeypatch):
    s = _meeting(ctx, vault)
    path = note_path(ctx, s, str(vault))
    _edit(path, "Send the deck ([[Me]])", "Send the final deck ([[Me]])")
    monkeypatch.setenv("FAKE_MERGE_FAIL", "1")
    assert apply_vault_edits(ctx, ctx.sessions.get_session(s.id), str(vault)) is False
    assert _item(ctx, s, "aaaa0002").text == "Send the deck"


# ---- with links to the vault's notes (obsidian_link_notes) ---------------------------------


def _linked(ctx, vault):
    """A meeting whose body, an item and a decision name a note of the vault ("Postgres")."""
    ctx.settings.obsidian_link_notes = True
    (vault / "Postgres.md").write_text("# Postgres\n", encoding="utf-8")
    s = _meeting(ctx, vault, export=False)
    full = ctx.sessions.get_session(s.id)
    full.summary = "We use Postgres for the store.\n\nThe rest is fine."
    full.summary_data.action_items[1].text = "Send the Postgres deck"
    full.summary_data.decisions = ["Keep Postgres"]
    full.summary_data.decision_ids = ["dddd0001"]
    ctx.repo.save(full)
    _export(ctx, s, vault)
    return s, note_path(ctx, s, str(vault))


def test_linked_blocks_are_written_as_exact(ctx, vault, sidecar):
    s, path = _linked(ctx, vault)
    text = _text(path)
    assert "We use [[Postgres]] for the store." in text
    assert f"- [ ] Send the [[Postgres]] deck ([[Me]]){DECK}\n" in text
    assert "- Keep [[Postgres]] ^m-dddd0001\n" in text
    written = summary_doc.ensure_doc(ctx, s.id).vault["written"]
    assert written["body_exact"] is True
    assert written["body"] == "We use Postgres for the store.\n\nThe rest is fine."
    assert written["blocks"]["aaaa0002"]["text"] == "Send the Postgres deck"
    assert written["blocks"]["aaaa0002"]["exact"] is True
    assert written["blocks"]["dddd0001"]["exact"] is True


@pytest.mark.parametrize("link", ["[[Postgres]]", "Postgres"])
def test_a_linked_item_reworded_flows_back_unlinked(ctx, vault, sidecar, link):
    s, path = _linked(ctx, vault)
    _edit(path, "Send the [[Postgres]] deck", f"Send the new {link} deck")
    _edit(path, "- Keep [[Postgres]]", f"- Keep {link} for now")
    _export(ctx, s, vault)
    data = _data(ctx, s)
    assert _item(ctx, s, "aaaa0002").text == "Send the new Postgres deck"
    assert data.decisions == ["Keep Postgres for now"]
    text = _text(path)
    assert f"- [ ] Send the new [[Postgres]] deck ([[Me]]){DECK}\n" in text
    assert "- Keep [[Postgres]] for now ^m-dddd0001\n" in text
    assert text.count("deck") == 1 and text.count("Keep") == 1
    _export(ctx, s, vault)
    assert _text(path) == text
    assert len(_events(ctx, s, "note_edits_taken")) == 1


def test_a_linked_body_paragraph_edit_flows_back(ctx, vault, sidecar):
    s, path = _linked(ctx, vault)
    _edit(path, "The rest is fine.", "The rest is fine, says [[Postgres]] Ana.")
    _export(ctx, s, vault)
    session = ctx.sessions.get_session(s.id)
    assert (
        session.summary == "We use Postgres for the store.\n\nThe rest is fine, says Postgres Ana."
    )
    text = _text(path)
    assert text.count("We use [[Postgres]] for the store.") == 1
    assert text.count("The rest is fine") == 1
    _change(ctx, s, summary=None)
    _export(ctx, s, vault)
    assert _text(path) == text
    assert len(_events(ctx, s, "note_edits_taken")) == 1


# ---- a task a later meeting continues ---------------------------------------------------------

CONTINUED = "- Send the deck ([[Me]]) ↩ continued in [[2026-10-20-Planning]] ^m-aaaa0002\n"


def test_parse_reads_a_continued_bullet_as_the_same_item():
    note = parse(
        "## Action Items\n\n"
        "- [ ] Write the guide ([[Corey Petty]]) 📅 2026-10-01 ^m-aaaa0001\n"
        + CONTINUED
        + "- Fix the build ([[Ana]]) ↩ continued in [[Later]] ^m-aaaa0003\n"
        "- No owner ↩ continued in [[Later]] ^m-aaaa0004\n"
        "    - my own annex line\n"
    )
    rows = {e.id: e for e in note.sections["action_items"]}
    assert list(rows) == ["aaaa0001", "aaaa0002", "aaaa0003", "aaaa0004"]
    deck = rows["aaaa0002"]
    assert (deck.text, deck.owner, deck.box, deck.done) == ("Send the deck", "Me", False, None)
    assert (rows["aaaa0003"].text, rows["aaaa0003"].owner) == ("Fix the build", "Ana")
    assert (rows["aaaa0004"].text, rows["aaaa0004"].owner) == ("No owner", None)
    assert rows["aaaa0004"].annex == ["    - my own annex line"]


def test_a_note_with_a_continued_bullet_pulls_back_nothing(ctx, vault, sidecar):
    s = _meeting(ctx, vault, export=False)
    full = ctx.sessions.get_session(s.id)
    full.summary_data.action_items[1].task_id = "abcdef012345"
    ctx.repo.save(full)
    newer = Session(
        name="Planning",
        created_at=datetime(2026, 10, 20, 9),
        summary="s",
        summary_data=SummaryData(
            action_items=[
                ActionItem(id="bbbb0001", text="Send the deck", owner="Me", task_id="abcdef012345")
            ]  # fmt: skip
        ),
    )
    ctx.repo.save(newer)
    _export(ctx, s, vault)
    path = note_path(ctx, s, str(vault))
    assert CONTINUED in _text(path)
    before = _data(ctx, s)
    text = _text(path)
    path.write_text(text + "\nMy own thoughts.\n", encoding="utf-8")  # the sha differs
    assert apply_vault_edits(ctx, ctx.sessions.get_session(s.id), str(vault)) is False
    assert _data(ctx, s) == before
    assert not summary_doc.ensure_doc(ctx, s.id).tombstones
    _export(ctx, s, vault)  # still a continued bullet, nothing doubled or lost
    after = _text(path)
    assert after.count("^m-aaaa0002") == 1 and CONTINUED in after
