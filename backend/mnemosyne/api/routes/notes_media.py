"""Images in the notes: their bytes, uploaded by an editor and read by anyone who may read the
meeting (services/notes_media.py). The image line itself travels in the notes document."""

import asyncio

from fastapi import APIRouter, Depends, HTTPException, Request, Response

from ... import access
from ...models.base import ApiModel
from ...services import notes_media, shared_notes, shared_with_me
from ..context import AppContext, get_ctx

router = APIRouter(prefix="/api/sessions", tags=["notes"])


class StoredMedia(ApiModel):
    sha256: str
    ext: str  # "webp" | "jpeg"
    size: int  # bytes


async def _body(request: Request, limit: int) -> bytes:
    """The raw body, refused with 413 past `limit` bytes (before reading it when it says so)."""
    length = request.headers.get("content-length", "")
    if length.isdigit() and int(length) > limit:
        raise HTTPException(status_code=413, detail="An image may be at most 2 MB")
    data = bytearray()
    async for chunk in request.stream():
        data += chunk
        if len(data) > limit:
            raise HTTPException(status_code=413, detail="An image may be at most 2 MB")
    return bytes(data)


@router.put(
    "/{session_id}/notes/media/{sha}",
    response_model=StoredMedia,
    status_code=201,
    responses={200: {"model": StoredMedia, "description": "It was stored already"}},
)
async def put_media(
    session_id: str,
    sha: str,
    request: Request,
    response: Response,
    ctx: AppContext = Depends(get_ctx),
):
    """Store an image of the meeting's notes: the raw WebP or JPEG bytes (Content-Type
    image/webp or image/jpeg, matching them), `sha` their SHA-256. Who may: as for the notes
    (an editor). 201 when stored, 200 when it was here; 400 a wrong hash, 413 over 2 MB or the
    meeting's 50 MB, 415 another type."""
    if not notes_media.SHA.match(sha):
        raise HTTPException(status_code=400, detail="Not an image hash")
    if not ctx.repo.exists(session_id):
        raise HTTPException(status_code=404, detail="Session not found")
    if not shared_notes.may_edit(ctx, session_id):
        raise access.Forbidden("You can only read these notes")
    if shared_with_me.shared_from(ctx, session_id):
        raise HTTPException(
            status_code=409, detail="Images of a meeting shared with you go to its owner"
        )
    kind = request.headers.get("content-type", "").split(";")[0].strip().lower()
    if kind not in notes_media.MEDIA_TYPES.values():
        raise HTTPException(status_code=415, detail="Send image/webp or image/jpeg")
    data = await _body(request, notes_media.IMAGE_MAX_BYTES)
    sniffed = notes_media.sniff(data)
    if sniffed is None or notes_media.MEDIA_TYPES[sniffed] != kind:
        raise HTTPException(status_code=415, detail="That is not the image type it says it is")
    try:
        info = await asyncio.to_thread(
            notes_media.store, ctx, session_id, sha, data, by=access.user_id()
        )
    except notes_media.MediaError as e:
        raise HTTPException(status_code=e.status, detail=str(e)) from e
    if not info.created:
        response.status_code = 200
    return StoredMedia(sha256=info.sha256, ext=info.ext, size=info.size)


@router.get(
    "/{session_id}/notes/media/{sha}",
    response_class=Response,
    responses={200: {"content": {"image/webp": {}, "image/jpeg": {}}}},
)
async def get_media(session_id: str, sha: str, ctx: AppContext = Depends(get_ctx)):
    """An image of the meeting's notes, for anyone who may read the meeting. 404 when its bytes
    are not here (yet: they may still be uploading or travelling)."""
    # The repository's read filter, as `_require` (a hidden meeting is not found), without
    # loading the whole meeting for each image.
    if not notes_media.SHA.match(sha) or not ctx.repo.exists(session_id):
        raise HTTPException(status_code=404, detail="Not found")
    file = notes_media.path(ctx, session_id, sha)
    if file is None:
        raise HTTPException(status_code=404, detail="That image is not here")
    try:
        data = await asyncio.to_thread(notes_media.read, ctx, session_id, sha)
    except FileNotFoundError as e:
        raise HTTPException(status_code=404, detail="That image is not here") from e
    except PermissionError as e:
        raise HTTPException(status_code=423, detail="Meetings are encrypted and locked") from e
    return Response(
        data,
        media_type=notes_media.MEDIA_TYPES[notes_media.ext_of(file)],
        headers={"Cache-Control": "private, max-age=31536000, immutable"},
    )
