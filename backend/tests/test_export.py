"""Obsidian export templates, exporter, and endpoint."""

from datetime import datetime

from mnemosyne.export.obsidian import ObsidianExporter
from mnemosyne.export.templates import render_meeting_note
from mnemosyne.models.session import Session
from mnemosyne.models.transcript import TranscriptSegment


def test_render_meeting_note_structure():
    note = render_meeting_note(
        title="Weekly Sync",
        date=datetime(2026, 9, 22, 10, 0),
        participants=["SPEAKER_00", "SPEAKER_01"],
        transcript_segments=[{"speaker": "SPEAKER_00", "start": 61, "text": "hi"}],
        summary="- point",
        notes="my notes",
    )
    assert note.startswith('---\ntitle: "Weekly Sync"\ndate: 2026-09-22\n')
    assert 'participants: ["SPEAKER_00", "SPEAKER_01"]' in note
    assert "## Summary\n\n- point" in note
    assert "## Notes\n\nmy notes" in note
    assert "**[01:01] SPEAKER_00:** hi" in note


def test_render_omits_empty_sections():
    note = render_meeting_note("T", datetime(2026, 1, 1), [], [], "", "")
    for heading in ("## Summary", "## Participants", "## Notes", "## Transcript"):
        assert heading not in note


def test_exporter_writes_sanitized_filename(tmp_path):
    session = Session(
        name='Q3: "plan" / review?',
        created_at=datetime(2026, 9, 22),
        transcript=[TranscriptSegment(text="x", speaker="S", start=0, end=1)],
        participants=["S"],
    )
    path = ObsidianExporter(str(tmp_path), "meetings").export(session)
    assert path == tmp_path / "meetings" / "2026-09-22-Q3 plan  review.md"
    assert path.read_text().startswith("---\n")


def test_exporter_requires_existing_vault(tmp_path):
    import pytest

    with pytest.raises(FileNotFoundError):
        ObsidianExporter(str(tmp_path / "missing")).export(Session())


def test_export_endpoint_uses_settings(client, ctx, tmp_path, transcribed_session):
    vault = tmp_path / "vault"
    vault.mkdir()
    resp = client.put(
        "/api/settings", json={"obsidian_vault_path": str(vault), "obsidian_subfolder": "notes"}
    )
    assert resp.json()["obsidian_vault_exists"] is True

    resp = client.post(f"/api/sessions/{transcribed_session['id']}/export/obsidian")
    assert resp.status_code == 200
    assert any(p.suffix == ".md" for p in (vault / "notes").iterdir())


def test_export_without_vault_configured_is_400(client, ctx, transcribed_session):
    ctx.settings.obsidian_vault_path = ""
    resp = client.post(f"/api/sessions/{transcribed_session['id']}/export/obsidian")
    assert resp.status_code == 400


def test_export_missing_vault_is_400(client, ctx, tmp_path, transcribed_session):
    ctx.settings.obsidian_vault_path = str(tmp_path / "missing")
    resp = client.post(f"/api/sessions/{transcribed_session['id']}/export/obsidian")
    assert resp.status_code == 400


def test_block_ids_only_for_items_with_ids():
    from mnemosyne.models.session import ActionItem, SummaryData

    def render(data):
        return render_meeting_note(
            "T", datetime(2026, 1, 1), [], [], "s", "", summary_data=data,
            is_mine=lambda i: i.owner != "Ana",
        )  # fmt: skip

    data = SummaryData(
        decisions=["Go"],
        open_questions=["Why?"],
        action_items=[ActionItem(text="Do it", owner="Me"), ActionItem(text="Ask", owner="Ana")],
    )
    assert "^m-" not in render(data)  # old data: as before
    data.decision_ids, data.question_ids = ["d1"], ["q1"]
    data.action_items[0].id, data.action_items[1].id = "a1", "a2"
    note = render(data)
    lines = note.splitlines()
    for line in (
        "- Go ^m-d1",
        "- Why? ^m-q1",
        "- [ ] Do it ([[Me]]) ^m-a1",
        "- [[Ana]]: Ask ^m-a2",
    ):
        assert line in lines


def test_a_continued_task_is_a_plain_bullet_with_a_link_to_the_later_note():
    from mnemosyne.models.session import ActionItem, SummaryData

    items = [
        ActionItem(id="aaaa0001", text="Update the docs", owner="Ann", due=None),
        ActionItem(id="aaaa0002", text="Send the deck", owner="Ann"),
    ]
    data = SummaryData(action_items=items)
    args = ("T", datetime(2026, 10, 1), [], [], "s", "")
    plain = render_meeting_note(*args, summary_data=data)
    assert "- [ ] Update the docs ([[Ann]]) ^m-aaaa0001\n" in plain
    assert render_meeting_note(*args, summary_data=data, continued={}) == plain
    assert render_meeting_note(*args, summary_data=data, continued={"zzzz": "x"}) == plain
    note = render_meeting_note(
        *args, summary_data=data, continued={"aaaa0001": "2026-10-08-Planning"}
    )
    assert (
        "- Update the docs ([[Ann]]) ↩ continued in [[2026-10-08-Planning]] ^m-aaaa0001\n" in note
    )
    assert "- [ ] Send the deck ([[Ann]]) ^m-aaaa0002\n" in note
    people_off = render_meeting_note(
        *args, summary_data=data, link_people=False, continued={"aaaa0001": "N"}
    )
    assert "- Update the docs (Ann) ↩ continued in [[N]] ^m-aaaa0001\n" in people_off
