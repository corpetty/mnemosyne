"""Obsidian notes list only your action items as checkboxes (obsidian_task_scope)."""

from datetime import date, datetime

from mnemosyne.api.routes.export import task_predicate
from mnemosyne.export.daily_note import write_daily_entry
from mnemosyne.export.people_notes import render_person
from mnemosyne.export.templates import render_meeting_note
from mnemosyne.models.session import ActionItem, NotesItem, Session, SummaryData
from mnemosyne.services.people import person_detail
from mnemosyne.services.tasks import mine_matcher
from tests.conftest import drain_until_job

ITEMS = [
    ActionItem(text="Write the guide", owner="Corey Petty", due=date(2026, 10, 1)),
    ActionItem(text="Send the deck", owner="Me"),
    ActionItem(text="Fix the build", owner="Jonny Zerah"),
    ActionItem(text="Book the room", owner="Ana", done=True),
    ActionItem(text="Review the plan", owner="Ana", follow=True),
    ActionItem(text="Decide the date"),
]


def _mine():
    matcher = mine_matcher(["Corey Petty"], ["Jonny Zerah", "Ana"])

    def mine(item):
        return not (item.owner or "").strip() or matcher(item.owner, True)

    return lambda item: item.follow or mine(item)


def _note(**kw) -> str:
    return render_meeting_note(
        "Sync",
        datetime(2026, 10, 1),
        [],
        [],
        "s",
        "",
        summary_data=SummaryData(action_items=ITEMS),
        **kw,
    )


def test_every_item_is_a_task_without_a_predicate():
    note = _note()
    assert "### Others" not in note
    assert "- [ ] Write the guide ([[Corey Petty]]) 📅 2026-10-01" in note
    assert "- [ ] Fix the build ([[Jonny Zerah]])" in note
    assert "- [x] Book the room ([[Ana]])" in note


def test_only_your_items_are_tasks_in_scope_mine():
    note = _note(is_mine=_mine())
    section = note.split("## Action Items\n")[1]
    tasks, others = section.split("### Others\n")
    assert "- [ ] Write the guide ([[Corey Petty]]) 📅 2026-10-01" in tasks
    assert "- [ ] Send the deck ([[Me]])" in tasks  # "Me" is the recorder
    assert "- [ ] Review the plan ([[Ana]])" in tasks  # followed
    assert "- [ ] Decide the date" in tasks  # no owner: the recorder's
    assert "Fix the build" not in tasks
    assert [line for line in others.strip().splitlines() if line] == [
        "- [[Jonny Zerah]]: Fix the build",
        "- [[Ana]]: Book the room (done)",
    ]
    assert "- [ ] Fix" not in note and "- [x] Book" not in note


def test_others_link_free_without_link_people_and_no_tasks_at_all():
    note = render_meeting_note(
        "Sync",
        datetime(2026, 10, 1),
        [],
        [],
        "s",
        "",
        summary_data=SummaryData(action_items=[ActionItem(text="Theirs", owner="Ana")]),
        link_people=False,
        is_mine=lambda item: False,
    )
    assert "## Action Items\n\n### Others\n\n- Ana: Theirs" in note
    assert "[ ]" not in note


def test_task_predicate_follows_the_setting(ctx):
    ctx.settings.local_speaker_name = "Corey Petty"
    session = Session(name="m")
    ctx.settings.obsidian_task_scope = "all"
    assert task_predicate(ctx, session) is None
    ctx.settings.obsidian_task_scope = "mine"
    pred = task_predicate(ctx, session)
    assert pred(ActionItem(text="a", owner="Me")) and pred(ActionItem(text="a", owner="Corey"))
    assert not pred(ActionItem(text="a", owner="Ana"))
    assert pred(ActionItem(text="a", owner="Ana", follow=True))


def _person_detail(ctx):
    ctx.repo.save(
        Session(
            name="Sync",
            created_at=datetime(2026, 10, 1),
            participants=["Ana"],
            summary="s",
            summary_data=SummaryData(
                action_items=[
                    ActionItem(text="Fix it", owner="Ana"),
                    ActionItem(text="Review it", owner="Ana", follow=True),
                    ActionItem(text="Done it", owner="Ana", done=True),
                ]
            ),
        )
    )
    return person_detail(ctx.repo, "Ana")


def test_person_note_open_tasks_are_plain_bullets_except_followed(ctx):
    d = _person_detail(ctx)
    stems = {m.id: "2026-10-01-Sync" for m in d.meetings}
    mine = render_person(d, stems, task_scope="mine")
    assert "## Open tasks\n\n- Fix it · [[2026-10-01-Sync|Sync]]\n" in mine
    assert "## Waiting on\n\n- [ ] Review it · [[2026-10-01-Sync|Sync]]\n" in mine
    assert "- [ ] Fix it" not in mine and "- [x] Done it" in mine
    everything = render_person(d, stems, task_scope="all")
    assert "- [ ] Fix it" in everything and "- [ ] Review it" in everything
    assert "Waiting on" not in everything


def test_daily_note_counts_me_as_you(tmp_path):
    session = Session(
        name="Plan",
        created_at=datetime(2026, 10, 1, 10),
        summary="Went well.",
        summary_data=SummaryData(
            action_items=[
                ActionItem(text="Mine by name", owner="Corey Petty"),
                ActionItem(text="Mine as me", owner="Jonny Zerah and Me"),
                ActionItem(text="Not mine", owner="Ana"),
            ]
        ),
    )
    matcher = mine_matcher(["Corey Petty"], ["Jonny Zerah", "Ana"])
    path = write_daily_entry(
        tmp_path, session, "2026-10-01-Plan", "Corey Petty", is_mine=lambda i: matcher(i.owner)
    )
    text = path.read_text()
    assert "    - [ ] Mine by name" in text and "    - [ ] Mine as me" in text
    assert "Not mine" not in text


def _summarized(name, day, owner="Ana"):
    return Session(
        name=name,
        created_at=datetime(2026, 10, day, 9),
        summary="s",
        summary_data=SummaryData(
            action_items=[
                ActionItem(text="Mine", owner="Me"),
                ActionItem(text="Theirs", owner=owner),
            ]
        ),
    )


def test_export_all_meetings_again_job(client, ctx, tmp_path):
    vault = tmp_path / "vault"
    vault.mkdir()
    ctx.settings.obsidian_vault_path = str(vault)
    ctx.settings.obsidian_subfolder = "notes"
    ctx.settings.local_speaker_name = "Corey Petty"
    ctx.repo.save(_summarized("Alpha", 1))
    ctx.repo.save(_summarized("Beta", 2))
    ctx.repo.save(Session(name="No summary", created_at=datetime(2026, 10, 3)))

    with client.websocket_connect("/ws") as ws:
        assert ws.receive_json()["type"] == "hello"
        job = client.post("/api/export/obsidian/all").json()
        assert job["kind"] == "obsidian_export_all"
        events = drain_until_job(ws, job["id"])
    done = client.get(f"/api/jobs/{job['id']}").json()
    assert done["status"] == "completed"
    assert done["result"] == {"exported": 2, "failed": 0, "total": 2}
    messages = [e["job"]["message"] for e in events if e["type"] == "job"]
    assert any("(1 of 2)" in m for m in messages) and any("(2 of 2)" in m for m in messages)
    notes = sorted((vault / "notes").glob("*.md"))
    assert [n.name for n in notes] == ["2026-10-01-Alpha.md", "2026-10-02-Beta.md"]
    text = notes[0].read_text()
    assert "- [ ] Mine ([[Me]])" in text and "### Others\n\n- [[Ana]]: Theirs" in text


def test_export_all_needs_a_vault(client, ctx):
    ctx.settings.obsidian_vault_path = ""
    assert client.post("/api/export/obsidian/all").status_code == 400


def test_scope_all_is_a_setting(client):
    values = client.get("/api/settings").json()["values"]
    assert values["obsidian_task_scope"] == "mine"
    res = client.put("/api/settings", json={"obsidian_task_scope": "all"})
    assert res.json()["values"]["obsidian_task_scope"] == "all"
    assert client.put("/api/settings", json={"obsidian_task_scope": "x"}).status_code == 422


# ---- action items typed in the notes (services/notes_items.py) -------------------------------

ADA = "[@Ada Lovelace](mnemosyne:person/Ada%20Lovelace)"
BOB = "[@Bob](mnemosyne:person/Bob)"
NOTES = (
    "# Plan\n\n"
    f"- [ ] {ADA} send the deck by [12:34](mnemosyne:t/0/754)\n"
    "- [ ] buy milk\n"
    f"- [x] {BOB}: book the room\n"
    f"    - [ ] {ADA} a nested one\n"
)


def _typed(item_id, text, owner, done=False, **kw):
    return NotesItem(id=item_id * 16, line=item_id * 64, text=text, owner=owner, done=done, **kw)


TYPED = [
    _typed("a", "send the deck by", "Ada Lovelace"),
    _typed("b", "book the room", "Bob", done=True),
]


def _noted(summary_data=None, notes=NOTES, items=TYPED, **kw) -> str:
    return render_meeting_note(
        "Sync",
        datetime(2026, 10, 1),
        [],
        [],
        "s",
        notes,
        summary_data=summary_data,
        notes_items=items,
        **kw,
    )


def test_notes_items_are_listed_after_the_summarys_with_block_ids():
    note = _noted(SummaryData(action_items=[ActionItem(id="1234abcd", text="Write the guide")]))
    section = note.split("## Action Items\n")[1].split("\n## ")[0]
    assert [line for line in section.splitlines() if line] == [
        "- [ ] Write the guide ^m-1234abcd",
        f"- [ ] send the deck by ([[Ada Lovelace]]) ^m-{'a' * 16}",
        f"- [x] book the room ([[Bob]]) ^m-{'b' * 16}",
    ]


def test_a_meeting_without_a_summary_still_lists_its_notes_items():
    note = _noted(None)
    assert "## Action Items\n" in note and f"^m-{'a' * 16}" in note
    assert note.index("## Action Items") < note.index("## Notes")


def test_notes_items_follow_the_task_scope_and_due_dates():
    only_bob = lambda item: item.owner == "Bob"  # noqa: E731
    items = [TYPED[0].model_copy(update={"due": date(2026, 10, 9)}), TYPED[1]]
    note = _noted(None, items=items, is_mine=only_bob)
    tasks, others = note.split("## Action Items\n")[1].split("### Others\n")
    assert f"- [x] book the room ([[Bob]]) ^m-{'b' * 16}" in tasks
    assert f"- [[Ada Lovelace]]: send the deck by ^m-{'a' * 16}" in others
    assert "- [ ] send" not in note
    assert "📅" not in note
    due = _noted(None, items=items)
    assert f"- [ ] send the deck by ([[Ada Lovelace]]) 📅 2026-10-09 ^m-{'a' * 16}" in due


def test_a_notes_item_continued_later_is_a_bullet_like_a_summary_item():
    note = _noted(None, continued={"a" * 16: "2026-10-08-Next"})
    assert (
        f"- send the deck by ([[Ada Lovelace]]) ↩ continued in [[2026-10-08-Next]] ^m-{'a' * 16}"
        in note
    )


def test_the_task_line_of_a_notes_item_is_a_plain_bullet_in_the_notes_section():
    note = _noted(SummaryData(action_items=[]))
    notes = note.split("## Notes\n")[1]
    assert "- [[Ada Lovelace]] send the deck by 12:34\n" in notes
    assert "- [[Bob]]: book the room\n" in notes
    assert "    - [ ] [[Ada Lovelace]] a nested one" in notes  # a task line that is no item: a box
    assert "- [ ] buy milk" in notes  # a checklist line stays a task
    assert "- [ ] [[Ada" not in notes.split("    - [ ]")[0]


def test_the_notes_bullet_rule_matches_owner_and_words_one_line_per_item():
    items = [_typed("a", "send the deck by", "Ada Lovelace")]
    twice = (
        f"- [ ] {ADA} send the deck by [12:34](mnemosyne:t/0/754)\n"
        f"- [ ] {ADA} send the deck by [13:00](mnemosyne:t/0/780)\n"
        f"- [ ] {BOB} send the deck by\n"
    )
    notes = _noted(None, notes=twice, items=items).split("## Notes\n")[1].strip()
    assert notes.splitlines()[0] == "- [[Ada Lovelace]] send the deck by 12:34"
    assert notes.splitlines()[1].startswith("- [ ] [[Ada Lovelace]] send the deck by 13:00")
    assert notes.splitlines()[2].startswith("- [ ] [[Bob]] send the deck by")


def test_the_notes_section_is_as_before_without_notes_items():
    note = _noted(None, items=[])
    assert "- [ ] [[Ada Lovelace]] send the deck by 12:34" in note
    assert "## Action Items" not in note
