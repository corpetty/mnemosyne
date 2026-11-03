"""This computer's models, lent to the person's other computers (services/peer_compute.py).

Only a computer paired with this one for library sync may call these (its DESKTOP token, the
caller found as for /api/sync), and never on a team server. The caller is the client of
summarization/peer.py and transcription/peer.py.
"""

import asyncio
import json
import logging
import re
import shutil

import httpx
from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile

from ... import access
from ...models.base import ApiModel
from ...services import peer_compute
from ...services.sync_peers import SyncPeer
from ..context import AppContext, get_ctx

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/compute", tags=["compute"])
ADMIN = [Depends(access.require_admin)]


class LentProvider(ApiModel):
    provider: str
    models: list[str]
    max_chars: int  # transcript characters one request may carry; 0 = no limit


class LentModels(ApiModel):
    providers: list[LentProvider]


class CompleteRequest(ApiModel):
    provider: str = ""  # blank: this computer's default provider (when lent)
    model: str = ""  # blank: that provider's default or first model
    system: str
    user: str
    think: bool = True


class CompleteResponse(ApiModel):
    text: str
    provider: str
    model: str


class TranscribeStarted(ApiModel):
    id: str


class TranscribeStatus(ApiModel):
    id: str
    state: str  # queued | running | done | failed | cancelled
    message: str
    progress: float | None
    engine: str
    error: str | None
    # When done: {segments: [TranscriptSegment + "source": kind], embeddings, dropped_echo,
    # dropped_noise}
    result: dict | None = None


def _caller(request: Request, ctx: AppContext) -> SyncPeer:
    if ctx.settings.team_mode:
        raise HTTPException(409, "A team server does not lend its models")
    peer = ctx.sync.calling_peer(getattr(request.state, "device_id", None))
    if peer is None:
        raise HTTPException(403, "Only a computer synced with this one may use its models")
    return peer


@router.get("/models", response_model=LentModels, dependencies=ADMIN)
async def lent_models(request: Request, ctx: AppContext = Depends(get_ctx)):
    """The providers and models this computer lends, its default first."""
    _caller(request, ctx)
    return LentModels(providers=[LentProvider(**p) for p in await peer_compute.catalog(ctx)])


@router.post("/complete", response_model=CompleteResponse, dependencies=ADMIN)
async def complete(body: CompleteRequest, request: Request, ctx: AppContext = Depends(get_ctx)):
    """One chat turn with one of this computer's lent providers."""
    peer = _caller(request, ctx)
    try:
        provider = peer_compute.lend_provider(ctx, body.provider)
        model = body.model
        if not model and provider == ctx.settings.default_provider:
            model = ctx.settings.default_model
        model = await ctx.summarizer.resolve_model(provider, model)
        text = await ctx.summarizer.complete(
            body.system, body.user, provider, model, think=body.think
        )
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    except httpx.HTTPError as e:
        logger.warning("Completion for %s with %s failed: %s", peer.name, body.provider, e)
        raise HTTPException(502, f"{provider} failed here: {e or type(e).__name__}") from e
    return CompleteResponse(text=text, provider=provider, model=model)


_SUFFIX = re.compile(r"^\.[A-Za-z0-9]{1,8}$")


@router.post("/transcribe", response_model=TranscribeStarted, dependencies=ADMIN)
async def transcribe(
    request: Request,
    spec: str = Form(...),
    files: list[UploadFile] = File(...),
    ctx: AppContext = Depends(get_ctx),
):
    """Transcribe (and diarize) audio for the calling computer with this computer's engine.
    `spec` is JSON: {sources: [{kind, speaker_label}], language, min_speakers, ...}, one source
    per file in order. Answers at once; GET /api/compute/transcribe/{id} tells how it goes."""
    peer = _caller(request, ctx)
    try:
        parsed = json.loads(spec)
        sources = list(parsed.get("sources") or [])
    except (ValueError, AttributeError, TypeError) as e:
        raise HTTPException(400, "spec is not valid JSON") from e
    if not sources or len(sources) != len(files) or len(files) > peer_compute.MAX_SOURCES:
        raise HTTPException(400, "Send one file per source (at most 8)")
    for s in sources:
        if not isinstance(s, dict) or s.get("kind") not in peer_compute.SOURCE_KINDS:
            raise HTTPException(400, "Each source needs a kind: mic, system or mixed")
        label = s.get("speaker_label")
        if label is not None and not isinstance(label, str):
            raise HTTPException(400, "speaker_label must be text")
    options = {k: parsed[k] for k in peer_compute.OPTION_KEYS if k in parsed}

    host = ctx.compute_host
    task = host.new_task(peer)
    try:
        for n, upload in enumerate(files):
            suffix = "." + (upload.filename or "").rsplit(".", 1)[-1] if upload.filename else ""
            path = task.folder / f"source-{n}{suffix if _SUFFIX.match(suffix) else '.audio'}"

            def save(upload=upload, path=path) -> None:
                with path.open("wb") as out:
                    shutil.copyfileobj(upload.file, out, 1 << 20)

            await asyncio.to_thread(save)
            task.files.append(path)
        host.start(task, sources, options)
    except Exception:
        await host.cancel(task)
        raise
    logger.info("Transcribing %d source(s) for %s", len(files), peer.name)
    return TranscribeStarted(id=task.id)


@router.get("/transcribe/{task_id}", response_model=TranscribeStatus, dependencies=ADMIN)
async def transcribe_status(task_id: str, request: Request, ctx: AppContext = Depends(get_ctx)):
    peer = _caller(request, ctx)
    task = ctx.compute_host.get(task_id, peer)
    if task is None:
        raise HTTPException(404, "No such transcription")
    return TranscribeStatus(**ctx.compute_host.status(task))


@router.delete("/transcribe/{task_id}", dependencies=ADMIN)
async def transcribe_cancel(task_id: str, request: Request, ctx: AppContext = Depends(get_ctx)):
    """Stop it (when it still runs) and forget it with its result."""
    peer = _caller(request, ctx)
    task = ctx.compute_host.get(task_id, peer)
    if task is None:
        raise HTTPException(404, "No such transcription")
    await ctx.compute_host.cancel(task)
    return {"ok": True}
