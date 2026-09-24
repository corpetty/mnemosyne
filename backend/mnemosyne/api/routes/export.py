"""Export endpoints."""

import logging

from fastapi import APIRouter, Depends, HTTPException

from ...export.obsidian import ObsidianExporter
from ...models.base import ApiModel
from ..context import AppContext, get_ctx

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api", tags=["export"])


class ExportResponse(ApiModel):
    path: str
    message: str


def build_exporter(ctx: AppContext, vault_path: str) -> ObsidianExporter:
    st = ctx.settings
    tags = [t.strip() for t in st.obsidian_tags.split(",") if t.strip()]
    return ObsidianExporter(
        vault_path,
        st.obsidian_subfolder,
        tags=tags or None,
        link_people=st.obsidian_link_people,
        include_transcript=st.obsidian_include_transcript,
    )


def export_session(ctx: AppContext, session, vault_path: str):
    """Write the meeting note and, when enabled, the notes of the people in it."""
    path = build_exporter(ctx, vault_path).export(session)
    st = ctx.settings
    if st.obsidian_people_notes:
        from pathlib import Path

        from ...export.people_notes import write_person_notes

        names = set(session.participants) | set(session.attendees)
        if session.summary_data:
            names |= {a.owner for a in session.summary_data.action_items if a.owner}
        try:
            write_person_notes(
                ctx.repo,
                Path(vault_path).expanduser(),
                st.obsidian_subfolder,
                names,
                (st.local_speaker_name, st.remote_speaker_name),
            )
        except Exception:
            logger.warning("Person notes failed for session %s", session.id, exc_info=True)
    return path


@router.get("/sessions/{session_id}/export/markdown")
async def export_markdown(session_id: str, ctx: AppContext = Depends(get_ctx)):
    """The note as it would be written to the vault (for preview / clipboard)."""
    session = ctx.sessions.get_session(session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="Session not found")
    return {
        "markdown": build_exporter(ctx, ctx.settings.obsidian_vault_path or ".").render(session)
    }


@router.post("/sessions/{session_id}/export/obsidian", response_model=ExportResponse)
async def export_to_obsidian(session_id: str, ctx: AppContext = Depends(get_ctx)):
    vault_path = ctx.settings.obsidian_vault_path
    if not vault_path:
        raise HTTPException(status_code=400, detail="Obsidian vault path not configured")

    session = ctx.sessions.get_session(session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="Session not found")

    try:
        path = export_session(ctx, session, vault_path)
        return ExportResponse(path=str(path), message="Exported successfully")
    except FileNotFoundError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    except Exception as e:
        logger.exception("Export failed")
        raise HTTPException(status_code=500, detail=f"Export failed: {e}") from e
