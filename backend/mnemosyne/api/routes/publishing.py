"""Publishing a meeting (services/publishing.py): the owner's API under /api, and the open,
read-only pages and files readers load from this computer (/p/<id>, /pub/<id>), with the same
headers as a publish host (publish-host/src/app.rs)."""

from __future__ import annotations

import asyncio
import shutil
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response
from starlette.background import BackgroundTask

from ...jobs import Job
from ...models.base import ApiModel
from ...models.publication import (
    Publication,
    PublicationDefaults,
    PublicationListItem,
    PublicationSelection,
)
from ...services import publication_file, publishing
from ..context import AppContext, get_ctx

router = APIRouter(tags=["publishing"])

VIEWER_DIR = Path(__file__).resolve().parent.parent / "static" / "viewer"
CSP = (
    "default-src 'none'; script-src 'self'; style-src 'self'; connect-src 'self'; "
    "media-src blob:; img-src data: blob:; base-uri 'none'; form-action 'none'; "
    "frame-ancestors 'none'"
)
IMMUTABLE = "public, max-age=31536000, immutable"
SECURITY = {"X-Content-Type-Options": "nosniff", "Referrer-Policy": "no-referrer"}
PLACEHOLDER = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><title>Mnemosyne</title>
<meta name="viewport" content="width=device-width, initial-scale=1"></head>
<body><p>The viewer for published meetings is not built into this Mnemosyne.</p></body></html>
"""


def _session(ctx: AppContext, session_id: str):
    session = ctx.repo.get(session_id)  # a meeting the caller may not see: Hidden (404)
    if session is None:
        raise HTTPException(status_code=404, detail="Session not found")
    return session


def _base(request: Request) -> str:
    return str(request.base_url).rstrip("/")


@router.get("/api/sessions/{session_id}/publication", response_model=Publication | None)
async def get_publication(session_id: str, request: Request, ctx: AppContext = Depends(get_ctx)):
    """The meeting's live publication, or null. Its link only for its owner or an admin."""
    session = _session(ctx, session_id)
    row = ctx.repo.live_publication(session_id)
    if row is None:
        return None
    return publishing.to_model(ctx, row, session, _base(request))


@router.get("/api/sessions/{session_id}/publication/defaults", response_model=PublicationDefaults)
async def publication_defaults(session_id: str, ctx: AppContext = Depends(get_ctx)):
    """What to publish: the live publication's selection, else the defaults; and who could
    open a link to this computer (`local_reach`)."""
    session = _session(ctx, session_id)
    publishing.check_may_publish(ctx, session)
    return publishing.defaults(ctx, session)


@router.post("/api/sessions/{session_id}/publication/preview")
async def preview_publication(
    session_id: str, selection: PublicationSelection, ctx: AppContext = Depends(get_ctx)
) -> dict[str, Any]:
    """The manifest readers would get, unsealed; quotes' clips as `preview_url`."""
    session = _session(ctx, session_id)
    publishing.check_may_publish(ctx, session)
    return publishing.preview(ctx, session, selection)


@router.put("/api/sessions/{session_id}/publication", response_model=Job)
async def publish(
    session_id: str,
    selection: PublicationSelection,
    request: Request,
    ctx: AppContext = Depends(get_ctx),
):
    """Publish the meeting, or update its publication (same link): a `publish` job, whose
    result is {publication}."""
    session = _session(ctx, session_id)
    try:
        publishing.check_request(ctx, session, selection)
    except publishing.PublishError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    if any(j.kind == "publish" for j in ctx.jobs.list(session_id=session_id, active_only=True)):
        raise HTTPException(status_code=409, detail="This meeting is being published already")

    base = _base(request)

    async def run(job):
        return await publishing.publish(ctx, session_id, selection, job, base)

    return ctx.jobs.submit("publish", run, session_id=session_id)


class PublicationFileRequest(ApiModel):
    selection: PublicationSelection
    password: str | None = None  # at least 8 characters; null = a random key inside the file


@router.post("/api/sessions/{session_id}/publication/file", response_model=Job)
async def export_file(
    session_id: str, request: PublicationFileRequest, ctx: AppContext = Depends(get_ctx)
):
    """The publication as one HTML file: a `publication_file` job whose result is
    {token, filename, size}, downloaded once from .../publication/file/{token}. Not a
    publication (nothing to update or unpublish)."""
    session = _session(ctx, session_id)
    try:
        publication_file.check_request(ctx, session, request.selection, request.password)
    except publication_file.NoTemplate as e:
        raise HTTPException(status_code=503, detail=str(e)) from e
    except publishing.PublishError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e

    async def run(job):
        return await publication_file.build(
            ctx, session_id, request.selection, request.password, job
        )

    return ctx.jobs.submit("publication_file", run, session_id=session_id)


@router.get("/api/sessions/{session_id}/publication/file/{token}")
async def download_file(session_id: str, token: str, ctx: AppContext = Depends(get_ctx)):
    """The file a `publication_file` job made, as a download; it is deleted afterwards."""
    _session(ctx, session_id)
    found = publication_file.find(ctx, session_id, token)
    if found is None:
        raise HTTPException(status_code=404, detail="File not found")
    html, meta, info = found

    def gone() -> None:
        html.unlink(missing_ok=True)
        meta.unlink(missing_ok=True)

    return FileResponse(
        html,
        media_type="text/html",
        filename=info.get("filename") or "meeting.html",
        content_disposition_type="attachment",
        background=BackgroundTask(gone),
    )


class SavePublicationFileRequest(ApiModel):
    path: str  # where to write it, on this computer


@router.post("/api/sessions/{session_id}/publication/file/{token}/save")
async def save_file(
    session_id: str,
    token: str,
    body: SavePublicationFileRequest,
    request: Request,
    ctx: AppContext = Depends(get_ctx),
):
    """Write the made file to a path the desktop app's save dialog chose (as a clip's save,
    routes/clips.py), then delete it. Only for a client on this computer: in server mode the
    path would be on the server. Who may is the download's rule (its maker, or an admin)."""
    if request.client is None or request.client.host not in ("127.0.0.1", "::1", "testclient"):
        raise HTTPException(status_code=403, detail="Saving to a path only works on this computer")
    _session(ctx, session_id)
    found = publication_file.find(ctx, session_id, token)
    if found is None:
        raise HTTPException(status_code=404, detail="File not found")
    html, meta, _ = found
    target = Path(body.path).expanduser()
    if not target.parent.is_dir():
        raise HTTPException(status_code=400, detail="That folder does not exist")
    await asyncio.to_thread(shutil.copyfile, html, target)
    html.unlink(missing_ok=True)
    meta.unlink(missing_ok=True)
    return {"path": str(target)}


@router.delete("/api/sessions/{session_id}/publication", response_model=Publication)
async def unpublish(session_id: str, request: Request, ctx: AppContext = Depends(get_ctx)):
    """Unpublish: the hosted copy and this computer's files are deleted, the link dies."""
    session = _session(ctx, session_id)
    try:
        row = await publishing.unpublish(ctx, session_id)
    except publishing.PublishError as e:
        raise HTTPException(status_code=502, detail=str(e)) from e
    if row is None:
        raise HTTPException(status_code=404, detail="This meeting is not published")
    return publishing.to_model(ctx, row, session, _base(request))


@router.get("/api/publications", response_model=list[PublicationListItem])
async def list_publications(request: Request, ctx: AppContext = Depends(get_ctx)):
    """Live publications of the meetings the caller can see, newest first."""
    visible = ctx.repo.visible_ids()
    out = []
    for row in ctx.repo.publications():
        if visible is not None and row["session_id"] not in visible:
            continue
        session = ctx.repo.get(row["session_id"])
        if session is None:
            continue
        out.append(
            PublicationListItem(
                publication=publishing.to_model(ctx, row, session, _base(request)),
                session_name=session.name,
            )
        )
    return out


# ---- open, read-only: what readers load ------------------------------------------------


def _error(status: int, code: str) -> JSONResponse:
    return JSONResponse({"error": code}, status_code=status, headers=SECURITY)


def _etag_matches(request: Request, sha: str) -> bool:
    for tag in request.headers.get("if-none-match", "").split(","):
        tag = tag.strip().removeprefix("W/").strip('"')
        if tag and (tag == "*" or tag == sha):
            return True
    return False


@router.get("/pub/{pub_id}", include_in_schema=False)
async def read_manifest(pub_id: str, request: Request, ctx: AppContext = Depends(get_ctx)):
    try:
        meta, data = await asyncio.to_thread(publishing.local_manifest, ctx, pub_id)
    except publishing.Gone:
        return _error(410, "gone")
    except LookupError:
        return _error(404, "not_found")
    sha = meta.get("manifest_sha", "")
    headers = {
        **SECURITY,
        "ETag": f'"{sha}"',
        "Cache-Control": "no-cache",
        "X-Pub-Version": str(meta.get("version", 0)),
    }
    if meta.get("expires") is not None:
        headers["X-Pub-Expires"] = str(meta["expires"])
    if _etag_matches(request, sha):
        return Response(status_code=304, headers=headers)
    return Response(data, media_type="application/octet-stream", headers=headers)


@router.get("/pub/{pub_id}/{sha}", include_in_schema=False)
async def read_blob(pub_id: str, sha: str, ctx: AppContext = Depends(get_ctx)):
    try:
        path = publishing.local_blob_path(ctx, pub_id, sha)
    except publishing.Gone:
        return _error(410, "gone")
    except LookupError:
        return _error(404, "not_found")
    return FileResponse(
        path,
        media_type="application/octet-stream",
        headers={**SECURITY, "Cache-Control": IMMUTABLE, "ETag": f'"{sha}"'},
    )


def _viewer_headers(cache: str) -> dict[str, str]:
    return {**SECURITY, "Content-Security-Policy": CSP, "Cache-Control": cache}


@router.get("/p/{pub_id}", include_in_schema=False)
async def viewer_page(pub_id: str):
    """Any /p/<id> is the viewer: it reads the id and key from its own address and says "not
    found" itself, so this never tells which ids exist."""
    index = VIEWER_DIR / "index.html"
    if not index.is_file():
        return HTMLResponse(PLACEHOLDER, status_code=503, headers=_viewer_headers("no-store"))
    return FileResponse(index, media_type="text/html", headers=_viewer_headers("no-store"))


_TYPES = {
    ".js": "text/javascript",
    ".mjs": "text/javascript",
    ".css": "text/css",
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".ico": "image/x-icon",
    ".woff2": "font/woff2",
    ".json": "application/json",
    ".txt": "text/plain; charset=utf-8",
}


@router.get("/p/{folder}/{rest:path}", include_in_schema=False)
async def viewer_asset(folder: str, rest: str):
    """/p/<dir>/<path>: the viewer build's files (its assets/ folder)."""
    root = VIEWER_DIR.resolve()
    parts = Path(folder, rest).parts
    if any(p.startswith(".") or "\\" in p for p in parts):
        return _error(404, "not_found")
    path = root.joinpath(*parts).resolve()
    if not path.is_file() or not path.is_relative_to(root):
        return _error(404, "not_found")
    return FileResponse(
        path,
        media_type=_TYPES.get(path.suffix.lower(), "application/octet-stream"),
        headers=_viewer_headers("no-cache"),  # as the publish host does
    )
