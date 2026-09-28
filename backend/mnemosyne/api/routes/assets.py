"""Resources: a library of links and files, attached to meetings (services/assets.py)."""

import asyncio
import shutil
import tempfile
from pathlib import Path

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse

from ...models.base import ApiModel
from ...models.session import Asset
from ...services.assets import file_path, link_title, store_file
from ...storage.crypto import is_encrypted
from ..context import AppContext, get_ctx

router = APIRouter(prefix="/api", tags=["assets"])

MAX_UPLOAD = 200 * 1024 * 1024


class LibraryAsset(ApiModel):
    asset: Asset
    used: int  # meetings it is attached to


class LinkCreate(ApiModel):
    url: str
    title: str = ""
    session_id: str | None = None  # attach it to this meeting too


class AssetUpdate(ApiModel):
    title: str


class AttachRequest(ApiModel):
    asset_id: str


def _changed(ctx: AppContext, session_id: str | None) -> None:
    if session_id:
        ctx.bus.publish({"type": "assets", "session_id": session_id})


def _check_session(ctx: AppContext, session_id: str | None) -> None:
    if session_id and ctx.sessions.get_session(session_id) is None:
        raise HTTPException(status_code=404, detail="Session not found")


@router.get("/assets", response_model=list[LibraryAsset])
async def library(q: str = "", limit: int = 100, ctx: AppContext = Depends(get_ctx)):
    """Every resource, newest first (to attach one from an earlier meeting)."""
    return [LibraryAsset(asset=a, used=n) for a, n in ctx.repo.list_assets(q, min(limit, 500))]


@router.post("/assets/link", response_model=Asset)
async def add_link(request: LinkCreate, ctx: AppContext = Depends(get_ctx)):
    url = request.url.strip()
    if not url.lower().startswith(("http://", "https://")) or " " in url:
        raise HTTPException(status_code=400, detail="Give a web address (https://...)")
    _check_session(ctx, request.session_id)
    asset = ctx.repo.find_link(url)
    if asset is None:
        asset = ctx.repo.add_asset(
            Asset(kind="link", url=url, title=request.title.strip() or link_title(url))
        )
    if request.session_id:
        ctx.repo.attach_asset(request.session_id, asset.id)
        _changed(ctx, request.session_id)
    return asset


@router.post("/assets/file", response_model=Asset)
async def add_file(
    file: UploadFile = File(...),
    title: str = Form(""),
    session_id: str | None = Form(None),
    ctx: AppContext = Depends(get_ctx),
):
    _check_session(ctx, session_id)
    name = Path(file.filename or "file").name
    with tempfile.NamedTemporaryFile(delete=False, dir=ctx.settings.data_dir) as tmp:
        size = 0
        while chunk := await file.read(1024 * 1024):
            size += len(chunk)
            if size > MAX_UPLOAD:
                Path(tmp.name).unlink(missing_ok=True)
                raise HTTPException(status_code=413, detail="Files up to 200 MB")
            tmp.write(chunk)
    if size == 0:
        Path(tmp.name).unlink(missing_ok=True)
        raise HTTPException(status_code=400, detail="The file is empty")
    asset = Asset(kind="file", title=title.strip() or name, filename=name, size=size)
    # Reading a PDF's text and encrypting take a while: in a thread, not on the event loop.
    stored, text = await asyncio.to_thread(store_file, ctx, asset, Path(tmp.name))
    asset = ctx.repo.add_asset(asset, path=stored, text=text)
    if session_id:
        ctx.repo.attach_asset(session_id, asset.id)
        _changed(ctx, session_id)
    return asset


@router.get("/assets/{asset_id}/file")
async def download(asset_id: str, request: Request, ctx: AppContext = Depends(get_ctx)):
    asset = ctx.repo.get_asset(asset_id)
    relative, _ = ctx.repo.asset_file(asset_id)
    if asset is None or not relative:
        raise HTTPException(status_code=404, detail="No such file")
    path = file_path(ctx, relative)
    if not path.exists():
        raise HTTPException(status_code=404, detail="The file is missing")
    if is_encrypted(path):
        from .audio import encrypted_response

        return encrypted_response(request, path, ctx.file_key, asset.filename or "file")
    return FileResponse(path, filename=asset.filename or path.name)


@router.patch("/assets/{asset_id}", response_model=Asset)
async def rename(asset_id: str, request: AssetUpdate, ctx: AppContext = Depends(get_ctx)):
    if not request.title.strip():
        raise HTTPException(status_code=400, detail="Give a title")
    asset = ctx.repo.update_asset(asset_id, title=request.title.strip())
    if asset is None:
        raise HTTPException(status_code=404, detail="No such resource")
    return asset


@router.delete("/assets/{asset_id}")
async def delete(asset_id: str, ctx: AppContext = Depends(get_ctx)):
    """Remove a resource from the library and from every meeting (its file too)."""
    if ctx.repo.get_asset(asset_id) is None:
        raise HTTPException(status_code=404, detail="No such resource")
    ctx.repo.delete_asset(asset_id)
    shutil.rmtree(ctx.settings.assets_dir / asset_id, ignore_errors=True)
    return {"deleted": True}


@router.post("/sessions/{session_id}/assets", response_model=list[Asset])
async def attach(session_id: str, request: AttachRequest, ctx: AppContext = Depends(get_ctx)):
    """Attach a resource from the library (e.g. one from an earlier meeting)."""
    _check_session(ctx, session_id)
    if ctx.repo.get_asset(request.asset_id) is None:
        raise HTTPException(status_code=404, detail="No such resource")
    ctx.repo.attach_asset(session_id, request.asset_id)
    _changed(ctx, session_id)
    return ctx.sessions.get_session(session_id).assets


@router.delete("/sessions/{session_id}/assets/{asset_id}", response_model=list[Asset])
async def detach(session_id: str, asset_id: str, ctx: AppContext = Depends(get_ctx)):
    """Take a resource off a meeting (it stays in the library)."""
    if not ctx.repo.detach_asset(session_id, asset_id):
        raise HTTPException(status_code=404, detail="Not attached")
    _changed(ctx, session_id)
    return ctx.sessions.get_session(session_id).assets
