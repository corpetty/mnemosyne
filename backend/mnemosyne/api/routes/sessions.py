"""Session management endpoints."""

from fastapi import APIRouter, Depends, HTTPException

from ... import access
from ...jobs import Job
from ...models.base import ApiModel
from ...models.session import (
    DEFAULT_SESSION_NAME,
    AgendaItem,
    CopilotNotes,
    Session,
    SessionSummary,
)
from ...services import contacts, history, shared_notes, shared_with_me, summary_edit
from ...services.combine import combine_runner
from ...services.copilot import copilot_ask_runner
from ...services.origin import require_local_audio
from ...services.pipeline import transcribe_session
from ...services.stats import MeetingStats, meeting_stats
from ...services.summary_edit import SummaryEdit
from ..context import AppContext, get_ctx

router = APIRouter(prefix="/api/sessions", tags=["sessions"])


class CreateSessionRequest(ApiModel):
    name: str = DEFAULT_SESSION_NAME


class RenameRequest(ApiModel):
    name: str


class NotesRequest(ApiModel):
    notes: str


def _require(ctx: AppContext, session_id: str) -> Session:
    session = ctx.sessions.get_session(session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="Session not found")
    return session


@router.get("", response_model=list[SessionSummary])
async def list_sessions(ctx: AppContext = Depends(get_ctx)):
    return ctx.sessions.list_sessions()


@router.post("", response_model=Session)
async def create_session(request: CreateSessionRequest, ctx: AppContext = Depends(get_ctx)):
    """Create a session; an untitled one made during a meeting is named after it."""
    from .audio import _apply_calendar

    return await _apply_calendar(ctx, ctx.sessions.create_session(request.name))


@router.get("/{session_id}", response_model=Session)
async def get_session(session_id: str, ctx: AppContext = Depends(get_ctx)):
    session = _require(ctx, session_id)
    history.log_access(ctx, session_id, "viewed")
    return session


@router.get("/{session_id}/stats", response_model=MeetingStats)
async def get_stats(session_id: str, ctx: AppContext = Depends(get_ctx)):
    """Talk time per speaker, turns and a who-spoke-when timeline."""
    return meeting_stats(_require(ctx, session_id).transcript)


@router.get("/{session_id}/copilot", response_model=CopilotNotes | None)
async def copilot_notes(session_id: str, ctx: AppContext = Depends(get_ctx)):
    """The running notes of the current (or last) recording of this session, if any."""
    session = _require(ctx, session_id)
    return ctx.copilot_notes.get(session_id) or session.copilot_notes


class CopilotAskRequest(ApiModel):
    question: str


@router.post("/{session_id}/copilot/ask", response_model=Job)
async def copilot_ask(
    session_id: str, request: CopilotAskRequest, ctx: AppContext = Depends(get_ctx)
):
    """Queue a `copilot_ask` job: a short answer from the meeting so far (the live transcript
    while recording). Result: `{question, answer, asked_at}`."""
    _require(ctx, session_id)
    question = request.question.strip()
    if not question:
        raise HTTPException(status_code=400, detail="Question must not be empty")
    return ctx.jobs.submit(
        "copilot_ask", copilot_ask_runner(ctx, session_id, question[:1000]), session_id=session_id
    )


class LocalOnlyRequest(ApiModel):
    local_only: bool


@router.put("/{session_id}/local-only", response_model=Session)
async def set_local_only(
    session_id: str, request: LocalOnlyRequest, ctx: AppContext = Depends(get_ctx)
):
    """Local-only meetings are never sent to a cloud LLM provider (OpenAI, Anthropic)."""
    _require(ctx, session_id)
    session = ctx.repo.update_fields(session_id, local_only=request.local_only)
    ctx.bus.publish({"type": "session", "session_id": session_id, "status": session.status.value})
    return session


@router.patch("/{session_id}", response_model=Session)
async def rename_session(
    session_id: str, request: RenameRequest, ctx: AppContext = Depends(get_ctx)
):
    session = ctx.sessions.rename_session(session_id, request.name)
    if session is None:
        raise HTTPException(status_code=404, detail="Session not found")
    return session


@router.delete("/{session_id}")
async def delete_session(session_id: str, reason: str = "", ctx: AppContext = Depends(get_ctx)):
    """Delete a meeting. Within the records period an admin gives a `reason`; never on legal
    hold; logged either way (services/records.py)."""
    session = _require(ctx, session_id)
    # Not under a recorder, an encoder or a transcription writing into it.
    if session_id in ctx.active_recordings or session_id in ctx.starting:
        raise HTTPException(status_code=409, detail="Stop the recording first")
    busy = [
        j for j in ctx.jobs.list(session_id=session_id, active_only=True) if j.kind != "copilot"
    ]
    if busy:
        raise HTTPException(
            status_code=409, detail="The meeting is busy; try again when it is done"
        )
    from ...services.records import check_delete

    try:
        check_delete(ctx, session, "meeting", reason)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    # A meeting someone shared with me: this removes only my copy (the repository leaves no
    # tombstone for it), so it is the app's own write. While the share is live, the next pull
    # brings it back.
    if session.shared_from:
        with access.internally():
            deleted = ctx.sessions.delete_session(session_id)
    else:
        deleted = ctx.sessions.delete_session(session_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="Session not found")
    return {"message": "Session deleted"}


@router.post("/{session_id}/notes", response_model=Session)
async def update_notes(session_id: str, request: NotesRequest, ctx: AppContext = Depends(get_ctx)):
    # The notes written whole. With live shared notes this goes through the meeting's document
    # (author "api"), broadcast to whoever has it open; without, straight to `Session.notes`.
    # Who may: as in the live notes (access.can_edit_notes). A meeting someone shared with me:
    # only when its notes were shared, only through the document and as me (its owner's hub
    # takes the frames, never Session.notes). (A comment, not a docstring: that would change
    # the generated API types.)
    if not ctx.repo.exists(session_id):
        raise HTTPException(status_code=404, detail="Session not found")
    shared = shared_with_me.shared_from(ctx, session_id)
    if not shared_notes.may_edit(ctx, session_id):
        if shared:
            raise access.Forbidden("This meeting's notes are not shared with you")
        raise access.Forbidden("You can only read these notes")
    author_id, author = access.user_id(), shared_notes.API_AUTHOR
    if shared and access.principal() is None:
        me = contacts.my_principal(ctx)
        author_id, author = me.user_id, me.name
    try:
        through_doc = await shared_notes.set_markdown(
            ctx, session_id, request.notes, author_id, author
        )
    except shared_notes.NotesWriteFailed as e:
        raise HTTPException(
            status_code=503, detail="The notes could not be saved; try again"
        ) from e
    if not through_doc and shared:
        raise HTTPException(
            status_code=409,
            detail="The notes of a meeting shared with you are written live, which is not"
            " possible now; try again later",
        )
    if not through_doc:
        with shared_notes.unrestricted():
            ctx.sessions.update_notes(session_id, request.notes)
    session = ctx.sessions.get_session(session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="Session not found")
    return session


@router.put("/{session_id}/summary", response_model=Session)
async def edit_summary(session_id: str, request: SummaryEdit, ctx: AppContext = Depends(get_ctx)):
    """Save a summary edited by hand: text, topics, decisions, open questions, action items,
    chapters and client facts together (services/summary_edit.py)."""
    session = summary_edit.edit_summary(ctx, session_id, request)
    if session is None:
        raise HTTPException(status_code=404, detail="Session not found")
    return session


class FollowupRequest(ApiModel):
    text: str


@router.put("/{session_id}/followup", response_model=Session)
async def save_followup(
    session_id: str, request: FollowupRequest, ctx: AppContext = Depends(get_ctx)
):
    """Keep the follow-up draft as edited (a new draft replaces it)."""
    session = summary_edit.save_followup(ctx, session_id, request.text)
    if session is None:
        raise HTTPException(status_code=404, detail="No summary to keep a follow-up with")
    return session


class MeetingTypeChoice(ApiModel):
    name: str  # a type's name, or "none"


@router.put("/{session_id}/meeting-type", response_model=Session)
async def set_meeting_type(
    session_id: str, request: MeetingTypeChoice, ctx: AppContext = Depends(get_ctx)
):
    from ...services.meeting_types import NONE, available_types

    if request.name != NONE and request.name not in {t.name for t in available_types(ctx.settings)}:
        raise HTTPException(status_code=400, detail="No such meeting type")
    session = ctx.sessions.set_meeting_type(session_id, request.name)
    if session is None:
        raise HTTPException(status_code=404, detail="Session not found")
    return session


class AgendaUpdate(ApiModel):
    items: list[AgendaItem]  # in order; a point keeps `covered` as sent


@router.put("/{session_id}/agenda", response_model=list[AgendaItem])
async def set_agenda(session_id: str, request: AgendaUpdate, ctx: AppContext = Depends(get_ctx)):
    """The points to get through: before the meeting, or during it (the copilot then marks
    the ones that came up)."""
    items = [a.model_copy(update={"text": a.text.strip()}) for a in request.items if a.text.strip()]
    session = ctx.repo.update_fields(session_id, agenda=items[:30])
    if session is None:
        raise HTTPException(status_code=404, detail="Session not found")
    ctx.bus.publish({"type": "session", "session_id": session_id, "status": session.status.value})
    return session.agenda


class CombineRequest(ApiModel):
    other_id: str  # this meeting joins the one in the path, and is then deleted


@router.post("/{session_id}/combine", response_model=Job)
async def combine(session_id: str, request: CombineRequest, ctx: AppContext = Depends(get_ctx)):
    """Combine two meetings (services/combine.py): their parts in recording order, one audio
    and transcript, the other meeting's speakers matched by voice. A `combine` job."""
    if request.other_id == session_id:
        raise HTTPException(status_code=400, detail="Choose another meeting")
    for sid in (session_id, request.other_id):
        session = ctx.sessions.get_session(sid)
        if session is None:
            raise HTTPException(status_code=404, detail="Meeting not found")
        require_local_audio(ctx, session)
        if not session.audio_file:
            raise HTTPException(status_code=400, detail=f"“{session.name}” has no audio")
        if sid in ctx.active_recordings:
            raise HTTPException(status_code=409, detail=f"“{session.name}” is being recorded")
        if ctx.jobs.list(session_id=sid, active_only=True):
            raise HTTPException(status_code=409, detail=f"“{session.name}” is busy; try again soon")
        if session.legal_hold:
            raise HTTPException(status_code=409, detail=f"“{session.name}” is on legal hold")
    return ctx.jobs.submit("combine", combine_runner(ctx, session_id, request.other_id), session_id)


@router.post("/{session_id}/transcribe", response_model=Job)
async def transcribe(session_id: str, ctx: AppContext = Depends(get_ctx)):
    """Queue a transcription job for the session's recorded audio."""
    session = _require(ctx, session_id)
    if not session.audio_file:
        raise HTTPException(status_code=400, detail="Session has no audio to transcribe")
    if ctx.jobs.list(session_id=session_id, active_only=True):
        raise HTTPException(status_code=409, detail="Session already has an active job")
    return ctx.jobs.submit("transcribe", transcribe_session(ctx, session_id), session_id=session_id)
