"""Notes from other assistants: Gemini, Zoom, Otter... (services/external_notes.py)."""

import zipfile

from mnemosyne.export.obsidian import ObsidianExporter
from mnemosyne.services.external_notes import detect_source
from tests.conftest import run_summarize

GEMINI = """📝 Notes
Sep 28, 2026
Atlas launch sync
Invited Ana Lima Daniel Okafor
Notes by Gemini

Summary
Ana and Daniel agreed to hold the beta on October 20.

Suggested next steps
Daniel will land the batching change."""

REPLY = (
    '{"summary": "Beta held.", "title": "Atlas launch sync", "decisions": ["Beta on Oct 20"],'
    ' "decision_at": ["00:03"], "action_items": [{"text": "Land batching", "owner": "Daniel",'
    ' "at": "00:05"}], "open_questions": [], "chapters": [{"start": "00:00", "title": "x"}]}'
)


def test_the_source_is_recognized():
    assert detect_source(GEMINI) == "Gemini"
    assert detect_source("Meeting summary for Weekly sync\nQuick recap\n...") == "Zoom"
    assert detect_source("Otter.ai summary") == "Otter"
    assert detect_source("Some notes I took") == "Other"


def test_paste_upload_and_remove(client, ctx, tmp_path):
    sid = ctx.sessions.create_session("m").id
    pasted = client.post(f"/api/sessions/{sid}/external-notes", json={"text": GEMINI}).json()
    assert pasted["source"] == "Gemini"
    docx = tmp_path / "Zoom summary.docx"
    with zipfile.ZipFile(docx, "w") as z:
        z.writestr(
            "word/document.xml", "<w:p><w:t>Quick recap</w:t></w:p><w:p><w:t>Ship</w:t></w:p>"
        )
    up = client.post(
        f"/api/sessions/{sid}/external-notes/file",
        files={"file": (docx.name, docx.read_bytes(), "application/octet-stream")},
    ).json()
    assert (
        up["source"] == "Zoom" and up["text"] == "Quick recap\nShip" and up["filename"] == docx.name
    )
    notes = client.get(f"/api/sessions/{sid}").json()["external_notes"]
    assert [n["source"] for n in notes] == ["Gemini", "Zoom"]
    assert client.delete(f"/api/sessions/{sid}/external-notes/{up['id']}").status_code == 200
    bad = client.post(f"/api/sessions/{sid}/external-notes/file", files={"file": ("a.png", b"x")})
    assert bad.status_code == 400
    empty = client.post(f"/api/sessions/{sid}/external-notes", json={"text": "  "})
    assert empty.status_code == 400
    note = ObsidianExporter(str(tmp_path)).render(ctx.sessions.get_session(sid))
    assert "> [!note]- Notes by Gemini\n> 📝 Notes" in note


def test_the_summary_reads_them_and_the_transcript_wins(
    client, ctx, fake_provider, transcribed_session
):
    sid = transcribed_session["id"]
    client.post(f"/api/sessions/{sid}/external-notes", json={"text": GEMINI})
    assert run_summarize(client, sid, {"provider": "fake"})["status"] == "completed"
    prompt = fake_provider.calls[-1]["system_prompt"]
    assert "--- Notes by Gemini ---" in prompt and "the transcript wins" in prompt


def test_a_meeting_with_only_notes_is_summarized_from_them(client, ctx, fake_provider):
    ctx.settings.auto_name_sessions = True
    sid = client.post("/api/sessions", json={}).json()["id"]  # untitled, no recording
    client.post(f"/api/sessions/{sid}/external-notes", json={"text": GEMINI})
    fake_provider.summary = REPLY
    assert run_summarize(client, sid, {"provider": "fake"})["status"] == "completed"
    call = fake_provider.calls[-1]
    assert "This meeting has no recording" in call["system_prompt"]
    assert "Gemini notes: Summary\nAna and Daniel agreed" in call["transcript"]
    s = client.get(f"/api/sessions/{sid}").json()
    assert s["summary"] == "Beta held." and s["name"] == "Atlas launch sync"
    data = s["summary_data"]
    assert data["decision_at"] == [None] and data["action_items"][0]["at"] is None
    assert data["chapters"] == [] and s["summary_stale"] is False


def test_nothing_to_summarize(client, ctx):
    sid = client.post("/api/sessions", json={}).json()["id"]
    assert client.post(f"/api/sessions/{sid}/summarize", json={}).status_code == 400
