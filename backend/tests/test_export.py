"""Obsidian export templates, exporter, and endpoint."""

from datetime import datetime

from src.mnemosyne.export.obsidian import ObsidianExporter
from src.mnemosyne.export.templates import render_meeting_note
from src.mnemosyne.models.session import Session
from src.mnemosyne.models.transcript import TranscriptSegment


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


def test_vault_config_roundtrip_and_export_endpoint(client, tmp_path, session_with_transcript):
    sid = session_with_transcript["id"]

    resp = client.post(
        "/api/settings/obsidian", json={"vault_path": str(tmp_path), "subfolder": "notes"}
    )
    assert resp.json() == {"vault_path": str(tmp_path), "subfolder": "notes", "exists": True}
    assert client.get("/api/settings/obsidian").json()["vault_path"] == str(tmp_path)

    resp = client.post(f"/api/sessions/{sid}/export/obsidian")
    assert resp.status_code == 200
    assert (tmp_path / "notes").is_dir()
    assert any(p.suffix == ".md" for p in (tmp_path / "notes").iterdir())


def test_export_without_vault_configured_is_400(client, monkeypatch, session_with_transcript):
    monkeypatch.delenv("OBSIDIAN_VAULT_PATH", raising=False)
    resp = client.post(f"/api/sessions/{session_with_transcript['id']}/export/obsidian")
    assert resp.status_code == 400
