"""Meetings in Obsidian's daily note (export/daily_note.py)."""

import json
from datetime import date, datetime

from mnemosyne.export.daily_note import BEGIN, END, moment_format, write_daily_entry
from mnemosyne.models.session import ActionItem, Session, SummaryData


def _meeting(name, hour, items=()):
    return Session(
        name=name,
        created_at=datetime(2026, 9, 28, hour, 5),
        summary=f"**{name}** went well. Then more detail.",
        summary_data=SummaryData(action_items=list(items)),
    )


def test_moment_formats():
    d = datetime(2026, 9, 8)
    assert moment_format("YYYY-MM-DD", d) == "2026-09-08"
    assert moment_format("YYYY/MMMM/D [daily]", d) == "2026/September/8 daily"
    assert moment_format("dddd, MMM D", d) == "Tuesday, Sep 8"


def test_entries_land_in_the_vaults_daily_note(tmp_path):
    vault = tmp_path / "vault"
    (vault / ".obsidian").mkdir(parents=True)
    (vault / ".obsidian" / "daily-notes.json").write_text(
        json.dumps({"folder": "Journal", "format": "YYYY-MM-DD", "template": "Templates/Day"})
    )
    (vault / "Templates").mkdir()
    (vault / "Templates" / "Day.md").write_text("# {{date}}\n\nMy own notes.\n")

    late = _meeting("Planning", 15, [
        ActionItem(text="Write the guide", owner="Me", due=date(2026, 10, 1)),
        ActionItem(text="Someone else's", owner="Ana"),
        ActionItem(text="Already done", owner="Me", done=True),
    ])  # fmt: skip
    path = write_daily_entry(vault, late, "2026-09-28-Planning", "Me")
    assert path == vault / "Journal" / "2026-09-28.md"
    early = _meeting("Standup", 9)
    write_daily_entry(vault, early, "2026-09-28-Standup", "Me")
    write_daily_entry(vault, late, "2026-09-28-Planning", "Me")  # again: replaced, not added

    text = path.read_text()
    assert text.startswith("# 2026-09-28\n\nMy own notes.\n\n## Meetings\n")
    section = text.split(BEGIN)[1].split(END)[0].strip().splitlines()
    assert section == [
        "- 09:05 [[2026-09-28-Standup|Standup]]: Standup went well.",
        "- 15:05 [[2026-09-28-Planning|Planning]]: Planning went well.",
        "    - [ ] Write the guide 📅 2026-10-01",
    ]


def test_an_existing_note_keeps_its_text(tmp_path):
    vault = tmp_path / "v"
    vault.mkdir()
    note = vault / "Daily" / "2026-09-28.md"
    note.parent.mkdir()
    note.write_text("Thoughts\n- a list of mine\n")
    write_daily_entry(vault, _meeting("Sync", 11), "2026-09-28-Sync", "Me", folder="Daily")
    text = note.read_text()
    assert text.startswith("Thoughts\n- a list of mine\n\n## Meetings\n" + BEGIN)
    assert text.endswith(END + "\n")


def test_export_writes_the_daily_note(client, ctx, tmp_path):
    vault = tmp_path / "vault"
    vault.mkdir()
    ctx.settings.obsidian_vault_path = str(vault)
    ctx.settings.obsidian_daily_notes = True
    s = ctx.sessions.create_session("Retro")
    ctx.sessions.set_summary(s.id, "We looked back.", SummaryData())
    assert client.post(f"/api/sessions/{s.id}/export/obsidian").status_code == 200
    today = ctx.sessions.get_session(s.id).created_at.strftime("%Y-%m-%d")
    assert "[[" in (vault / f"{today}.md").read_text()
