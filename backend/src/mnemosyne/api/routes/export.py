"""Export endpoints for Obsidian integration."""

import logging
import os

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from ...export.obsidian import ObsidianExporter
from ...services.session_service import session_service

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api", tags=["export"])


class ExportResponse(BaseModel):
    path: str
    message: str


class VaultConfigRequest(BaseModel):
    vault_path: str
    subfolder: str = "meetings/mnemosyne"


class VaultConfigResponse(BaseModel):
    vault_path: str
    subfolder: str
    exists: bool


@router.post("/sessions/{session_id}/export/obsidian", response_model=ExportResponse)
async def export_to_obsidian(session_id: str):
    """Export a session as a markdown file to the configured Obsidian vault."""
    vault_path = os.environ.get("OBSIDIAN_VAULT_PATH", "")
    subfolder = os.environ.get("OBSIDIAN_SUBFOLDER", "meetings/mnemosyne")

    if not vault_path:
        raise HTTPException(status_code=400, detail="Obsidian vault path not configured")

    session = session_service.get_session(session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="Session not found")

    try:
        exporter = ObsidianExporter(vault_path=vault_path, subfolder=subfolder)
        path = exporter.export(session)
        return ExportResponse(path=str(path), message="Exported successfully")
    except FileNotFoundError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.exception("Export failed")
        raise HTTPException(status_code=500, detail=f"Export failed: {e}")


@router.get("/settings/obsidian", response_model=VaultConfigResponse)
async def get_vault_config():
    """Get the current Obsidian vault configuration."""
    vault_path = os.environ.get("OBSIDIAN_VAULT_PATH", "")
    subfolder = os.environ.get("OBSIDIAN_SUBFOLDER", "meetings/mnemosyne")
    from pathlib import Path

    return VaultConfigResponse(
        vault_path=vault_path,
        subfolder=subfolder,
        exists=Path(vault_path).exists() if vault_path else False,
    )


@router.post("/settings/obsidian", response_model=VaultConfigResponse)
async def set_vault_config(request: VaultConfigRequest):
    """Update the Obsidian vault configuration (runtime only)."""
    os.environ["OBSIDIAN_VAULT_PATH"] = request.vault_path
    os.environ["OBSIDIAN_SUBFOLDER"] = request.subfolder
    from pathlib import Path

    return VaultConfigResponse(
        vault_path=request.vault_path,
        subfolder=request.subfolder,
        exists=Path(request.vault_path).exists(),
    )
