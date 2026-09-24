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
        path = build_exporter(ctx, vault_path).export(session)
        return ExportResponse(path=str(path), message="Exported successfully")
    except FileNotFoundError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    except Exception as e:
        logger.exception("Export failed")
        raise HTTPException(status_code=500, detail=f"Export failed: {e}") from e
