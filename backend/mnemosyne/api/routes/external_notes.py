"""Notes from other assistants (Gemini, Zoom, Otter, Teams Copilot...) for a meeting
(services/external_notes.py)."""

import tempfile
from pathlib import Path

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile

from ...models.base import ApiModel
from ...models.session import ExternalNotes
from ...services.assets import extract_text
from ...services.external_notes import clean, detect_source
from ..context import AppContext, get_ctx

router = APIRouter(prefix="/api/sessions/{session_id}/external-notes", tags=["external-notes"])


class NotesCreate(ApiModel):
    text: str
    source: str = ""  # "" = recognized from the text


def _add(ctx: AppContext, session_id: str, text: str, source: str, filename=None):
    if ctx.sessions.get_session(session_id) is None:
        raise HTTPException(status_code=404, detail="Session not found")
    text = clean(text)
    if not text:
        raise HTTPException(status_code=400, detail="The notes are empty")
    notes = ExternalNotes(
        source=source.strip() or detect_source(text), text=text, filename=filename
    )
    ctx.repo.add_external_notes(session_id, notes)
    ctx.bus.publish({"type": "assets", "session_id": session_id})
    return notes


@router.post("", response_model=ExternalNotes)
async def paste(session_id: str, request: NotesCreate, ctx: AppContext = Depends(get_ctx)):
    """Notes pasted as text (e.g. copied from the "Notes by Gemini" doc or Zoom's email)."""
    return _add(ctx, session_id, request.text, request.source)


@router.post("/file", response_model=ExternalNotes)
async def upload(
    session_id: str,
    file: UploadFile = File(...),
    source: str = Form(""),
    ctx: AppContext = Depends(get_ctx),
):
    """Notes as a file: Markdown, text, HTML, Word (.docx), PDF (with pdftotext)."""
    name = Path(file.filename or "notes.txt").name
    suffix = Path(name).suffix.lower()
    with tempfile.NamedTemporaryFile(delete=False, dir=ctx.settings.data_dir, suffix=suffix) as tmp:
        tmp.write(await file.read(20 * 1024 * 1024 + 1))
    try:
        if Path(tmp.name).stat().st_size > 20 * 1024 * 1024:
            raise HTTPException(status_code=413, detail="Notes files up to 20 MB")
        text = extract_text(Path(tmp.name), name)
    finally:
        Path(tmp.name).unlink(missing_ok=True)
    if text is None:
        raise HTTPException(
            status_code=400,
            detail="Could not read that file: use text, Markdown, HTML, Word or PDF",
        )
    return _add(ctx, session_id, text, source, name)


@router.delete("/{notes_id}")
async def delete(session_id: str, notes_id: str, ctx: AppContext = Depends(get_ctx)):
    if not ctx.repo.delete_external_notes(session_id, notes_id):
        raise HTTPException(status_code=404, detail="Notes not found")
    ctx.bus.publish({"type": "assets", "session_id": session_id})
    return {"deleted": True}
