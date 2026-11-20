"""Narration voices and narrations (services/voices.py, services/narration.py,
docs/plans/2026-11-20-narration-voices.md).

A voice is its owner's alone: every voice route answers 404 for someone else's. Checking clips,
the consent recording and narrating run as jobs (`voice_audio`, `voice_consent`, `narrate`).
"""

import asyncio
import tempfile
from pathlib import Path

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse

from ... import access
from ...jobs import Job
from ...models.base import ApiModel
from ...models.voice import (
    ConsentChallenge,
    Narration,
    NarrationRequest,
    NarrationSource,
    Voice,
    VoiceEngine,
)
from ...services import narration, tts_install, voices
from ...storage.crypto import is_encrypted
from ...synthesis import kitten
from ..context import AppContext, get_ctx

router = APIRouter(prefix="/api", tags=["voices"])
ADMIN = [Depends(access.require_admin)]
MAX_UPLOAD = 200 * 1024 * 1024


class VoiceCreate(ApiModel):
    name: str
    language: str = "en"


class VoiceUpdate(ApiModel):
    name: str


class ClipUpdate(ApiModel):
    text: str


class InstallRequest(ApiModel):
    accept_license: bool = False


class ChallengeRequest(ApiModel):
    name: str = ""  # how to introduce yourself in the statement (default: your name)


def _refuse(e: Exception) -> HTTPException:
    return HTTPException(status_code=getattr(e, "status", 400), detail=str(e))


async def _save_upload(ctx: AppContext, file: UploadFile) -> Path:
    suffix = Path(file.filename or "audio").suffix[:10]
    with tempfile.NamedTemporaryFile(delete=False, dir=ctx.settings.data_dir, suffix=suffix) as tmp:
        size = 0
        while chunk := await file.read(1024 * 1024):
            size += len(chunk)
            if size > MAX_UPLOAD:
                Path(tmp.name).unlink(missing_ok=True)
                raise HTTPException(status_code=413, detail="Recordings up to 200 MB")
            tmp.write(chunk)
    if size == 0:
        Path(tmp.name).unlink(missing_ok=True)
        raise HTTPException(status_code=400, detail="The recording is empty")
    return Path(tmp.name)


def _serve(request: Request, ctx: AppContext, path: Path, filename: str):
    if not path.exists():
        raise HTTPException(status_code=404, detail="The file is missing")
    if is_encrypted(path):
        from .audio import encrypted_response

        return encrypted_response(request, path, ctx.file_key, filename)
    return FileResponse(path, filename=filename)


# ---- the engine -------------------------------------------------------------


@router.get("/voices/engine", response_model=VoiceEngine)
async def engine_status(ctx: AppContext = Depends(get_ctx)):
    from ...synthesis.registry import resolve_synthesizer

    data_dir = ctx.settings.data_dir
    return VoiceEngine(
        engine=resolve_synthesizer(ctx.settings),
        installed=tts_install.installed(data_dir),
        installable=tts_install.uv_binary() is not None,
        install_bytes=await asyncio.to_thread(tts_install.size_bytes, data_dir),
        license_name=kitten.LICENSE_NAME,
        license_url=kitten.LICENSE_URL,
        attribution=kitten.ATTRIBUTION,
        can_check_voices=voices.can_check(ctx),
    )


@router.post("/voices/engine", response_model=Job, dependencies=ADMIN)
async def install_engine(request: InstallRequest, ctx: AppContext = Depends(get_ctx)):
    """Install KittenTTS 2 (an `install_voice_engine` job, about 3 GB). Its weights are not open
    source: the request must say the licence was accepted."""
    if not request.accept_license:
        raise HTTPException(
            status_code=400, detail=f"Accept the {kitten.LICENSE_NAME} to install KittenTTS 2"
        )
    if any(j.kind == "install_voice_engine" for j in ctx.jobs.list(active_only=True)):
        raise HTTPException(status_code=409, detail="The voice engine is already being installed")

    async def run(job) -> dict:
        await tts_install.install(
            ctx.settings.data_dir,
            lambda message, frac: job.update(message, progress=frac),
            accepted_license=True,
        )
        narration.forget_engine(ctx)
        return {"mb": round(tts_install.size_bytes(ctx.settings.data_dir) / 1e6)}

    return ctx.jobs.submit("install_voice_engine", run)


@router.delete("/voices/engine", dependencies=ADMIN)
async def remove_engine(ctx: AppContext = Depends(get_ctx)):
    """Remove the engine and its model (voices and narrations stay)."""
    synth = narration.engine(ctx)
    if synth is not None and synth.is_loaded():
        await synth.unload()
    narration.forget_engine(ctx)
    await asyncio.to_thread(tts_install.remove, ctx.settings.data_dir)
    return {"removed": True}


# ---- voices -------------------------------------------------------------------


@router.get("/voices/script", response_model=list[str])
async def guided_script():
    """Sentences to read, one take each, when recording a voice in the app."""
    return voices.GUIDED_SCRIPT


@router.get("/voices", response_model=list[Voice])
async def list_voices(ctx: AppContext = Depends(get_ctx)):
    return voices.list_own(ctx)


@router.post("/voices", response_model=Voice)
async def create_voice(request: VoiceCreate, ctx: AppContext = Depends(get_ctx)):
    try:
        return voices.create(ctx, request.name, request.language)
    except voices.VoiceError as e:
        raise _refuse(e) from e


@router.get("/voices/{voice_id}", response_model=Voice)
async def get_voice(voice_id: str, ctx: AppContext = Depends(get_ctx)):
    try:
        return voices.view(ctx, voices.get_own(ctx, voice_id))
    except voices.VoiceError as e:
        raise _refuse(e) from e


@router.patch("/voices/{voice_id}", response_model=Voice)
async def rename_voice(voice_id: str, request: VoiceUpdate, ctx: AppContext = Depends(get_ctx)):
    try:
        voices.get_own(ctx, voice_id)
    except voices.VoiceError as e:
        raise _refuse(e) from e
    if not request.name.strip():
        raise HTTPException(status_code=400, detail="Give the voice a name")
    return voices.view(ctx, ctx.repo.update_voice(voice_id, name=request.name.strip()[:80]))


@router.delete("/voices/{voice_id}")
async def delete_voice(voice_id: str, ctx: AppContext = Depends(get_ctx)):
    """The voice and every recording of it, the consent recording included."""
    try:
        voices.delete(ctx, voice_id)
    except voices.VoiceError as e:
        raise _refuse(e) from e
    return {"deleted": True}


@router.post("/voices/{voice_id}/audio", response_model=Job)
async def add_voice_audio(
    voice_id: str,
    file: UploadFile = File(...),
    text: str = Form(""),
    single: bool = Form(False),
    ctx: AppContext = Depends(get_ctx),
):
    """Add a recording: one take of a script sentence (`single`, with its `text`), or a longer
    upload cut into pieces. The checks run as a `voice_audio` job; its result is the voice."""
    try:
        voices.get_own(ctx, voice_id)
    except voices.VoiceError as e:
        raise _refuse(e) from e
    source = await _save_upload(ctx, file)

    async def run(job) -> dict:
        try:
            job.update("Checking the recording", 0.1)
            voice = await voices.add_audio(ctx, voice_id, source, text=text, single=single)
            return {"voice": voice.model_dump(mode="json")}
        finally:
            source.unlink(missing_ok=True)

    return ctx.jobs.submit("voice_audio", run)


@router.patch("/voices/{voice_id}/clips/{idx}", response_model=Voice)
async def correct_clip(
    voice_id: str, idx: int, request: ClipUpdate, ctx: AppContext = Depends(get_ctx)
):
    """Correct what is said in a recording (the engine reads it with the audio)."""
    try:
        return voices.set_clip_text(ctx, voice_id, idx, request.text)
    except voices.VoiceError as e:
        raise _refuse(e) from e


@router.delete("/voices/{voice_id}/clips/{idx}", response_model=Voice)
async def delete_clip(voice_id: str, idx: int, ctx: AppContext = Depends(get_ctx)):
    try:
        return voices.delete_clip(ctx, voice_id, idx)
    except voices.VoiceError as e:
        raise _refuse(e) from e


@router.get("/voices/{voice_id}/clips/{idx}/audio")
async def clip_audio(voice_id: str, idx: int, request: Request, ctx: AppContext = Depends(get_ctx)):
    """Listen back to one of your own recordings."""
    try:
        voices.get_own(ctx, voice_id)
    except voices.VoiceError as e:
        raise _refuse(e) from e
    clip = next((c for c in ctx.repo.voice_clips(voice_id) if c["idx"] == idx), None)
    if clip is None:
        raise HTTPException(status_code=404, detail="No such recording")
    return _serve(request, ctx, voices.stored(ctx, clip["path"]), f"recording-{idx}.wav")


@router.post("/voices/{voice_id}/consent/challenge", response_model=ConsentChallenge)
async def consent_challenge(
    voice_id: str, request: ChallengeRequest, ctx: AppContext = Depends(get_ctx)
):
    """The statement to read aloud now, with random words shown only this once."""
    try:
        return voices.challenge(ctx, voice_id, request.name)
    except voices.VoiceError as e:
        raise _refuse(e) from e


@router.post("/voices/{voice_id}/consent", response_model=Job)
async def consent(voice_id: str, file: UploadFile = File(...), ctx: AppContext = Depends(get_ctx)):
    """The live recording of the statement; checked as a `voice_consent` job whose result is
    a ConsentResult."""
    try:
        voices.get_own(ctx, voice_id)
    except voices.VoiceError as e:
        raise _refuse(e) from e
    source = await _save_upload(ctx, file)

    async def run(job) -> dict:
        try:
            job.update("Checking the statement", 0.2)
            result = await voices.check_consent(ctx, voice_id, source)
            return result.model_dump(mode="json")
        finally:
            source.unlink(missing_ok=True)

    return ctx.jobs.submit("voice_consent", run)


# ---- narrations ---------------------------------------------------------------


@router.post("/narrations", response_model=Job)
async def narrate(request: NarrationRequest, ctx: AppContext = Depends(get_ctx)):
    """Read a summary, notes, follow-up, digest or any text in your voice (a `narrate` job;
    its result names the narration)."""
    try:
        voice = voices.view(ctx, voices.get_own(ctx, request.voice_id))
        if not voice.ready:
            raise voices.VoiceError(f"The voice is not ready: it needs {voice.needs}")
        if narration.engine(ctx) is None:
            raise narration.NoEngine("No narration engine is installed (Settings -> Voices)")
        narration.plan_chunks(ctx, narration.source_text(ctx, request)[1])
    except (voices.VoiceError, narration.NarrationError) as e:
        raise _refuse(e) from e

    async def run(job) -> dict:
        return await narration.run(ctx, request, job)

    return ctx.jobs.submit(
        "narrate", run, session_id=request.source_id if request.source_kind != "digest" else None
    )


@router.get("/narrations", response_model=list[Narration])
async def list_narrations(
    source_kind: NarrationSource | None = None,
    source_id: str | None = None,
    limit: int = 100,
    ctx: AppContext = Depends(get_ctx),
):
    """Your narrations, newest first; of one meeting or digest with `source_kind`/`source_id`."""
    rows = ctx.repo.list_narration_rows(
        user_id=access.user_id(),
        source_kind=source_kind,
        source_id=source_id,
        limit=min(limit, 500),
    )
    return [narration.view(r) for r in rows]


@router.get("/narrations/{narration_id}", response_model=Narration)
async def get_narration(narration_id: str, ctx: AppContext = Depends(get_ctx)):
    try:
        return narration.view(narration.get_own(ctx, narration_id))
    except voices.VoiceError as e:
        raise _refuse(e) from e


@router.get("/narrations/{narration_id}/audio")
async def narration_audio(narration_id: str, request: Request, ctx: AppContext = Depends(get_ctx)):
    try:
        row = narration.get_own(ctx, narration_id)
    except voices.VoiceError as e:
        raise _refuse(e) from e
    safe = "".join(ch if ch.isalnum() or ch in " -_" else "_" for ch in row["title"])[:80]
    return _serve(request, ctx, voices.stored(ctx, row["path"]), f"{safe or 'narration'}.ogg")


@router.delete("/narrations/{narration_id}")
async def delete_narration(narration_id: str, ctx: AppContext = Depends(get_ctx)):
    try:
        narration.delete(ctx, narration_id)
    except voices.VoiceError as e:
        raise _refuse(e) from e
    return {"deleted": True}
