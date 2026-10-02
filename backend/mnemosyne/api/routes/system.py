"""What this machine can do, for the setup wizard."""

import asyncio
import ctypes
import importlib.util
import platform
import shutil

from fastapi import APIRouter, Depends, HTTPException

from ... import access
from ...jobs import Job
from ...models.base import ApiModel
from ..context import AppContext, get_ctx

router = APIRouter(prefix="/api", tags=["system"])
ADMIN = [Depends(access.require_admin)]  # team-wide: an admin's job (access.py)


class SystemInfo(ApiModel):
    gpu_driver: bool  # an NVIDIA driver: nvidia-smi on PATH, or libcuda loadable
    gpu_stack: bool  # torch and WhisperX are installed (the gpu extra)
    parakeet: bool  # onnx-asr is installed (the onnx extra)
    pipewire: bool  # pw-record and pw-dump are available
    ffmpeg: bool
    hf_token: bool  # needed for speaker identification (pyannote)
    onnx_diarizer: bool  # sherpa-onnx is installed: speakers told apart on the CPU
    # What this machine runs for the current settings (registry.resolve_*): "auto" and choices
    # whose packages are missing resolve to what works here.
    transcriber_in_use: str
    diarizer_in_use: str
    platform: str
    problems: list[str]  # what went wrong when the backend started (config, recovery, ...)
    # For the desktop shell, which installs the gpu extra in the background: not before the
    # first-run setup is answered, and not when it was declined ("off").
    setup_complete: bool
    gpu_support: str  # auto | off


def _has(module: str) -> bool:
    try:
        return importlib.util.find_spec(module) is not None
    except (ImportError, ValueError):
        return False


def _gpu_driver() -> bool:
    if shutil.which("nvidia-smi"):
        return True
    try:
        ctypes.CDLL("libcuda.so.1")
        return True
    except OSError:
        return False


@router.get("/system", response_model=SystemInfo)
async def system_info(ctx: AppContext = Depends(get_ctx)):
    from ...transcription.registry import resolve_diarizer, resolve_transcriber

    # May import torch to see whether CUDA works: not on the event loop.
    transcriber = await asyncio.to_thread(resolve_transcriber, ctx.settings)
    diarizer = await asyncio.to_thread(resolve_diarizer, ctx.settings)
    return SystemInfo(
        onnx_diarizer=_has("sherpa_onnx"),
        transcriber_in_use=transcriber,
        diarizer_in_use=diarizer,
        gpu_driver=_gpu_driver(),
        gpu_stack=_has("torch") and _has("whisperx"),
        parakeet=_has("onnx_asr"),
        pipewire=bool(shutil.which("pw-record") and shutil.which("pw-dump")),
        ffmpeg=bool(shutil.which("ffmpeg")),
        hf_token=bool(ctx.settings.hf_token),
        platform=f"{platform.system()} {platform.release()}",
        problems=ctx.startup_problems,
        setup_complete=ctx.settings.setup_complete,
        gpu_support=ctx.settings.gpu_support,
    )


@router.post("/system/prepare", response_model=Job, dependencies=ADMIN)
async def prepare_models(ctx: AppContext = Depends(get_ctx)):
    """Download the speech and search models now (a `prepare_models` job), so the first
    recording, transcription and search do not wait for them: what setup does at the end."""
    from ...services.model_downloads import MB, SEARCH_MB, TRANSCRIBER_MB, hf_cache, watched
    from ...transcription.registry import resolve_transcriber

    if any(j.kind == "prepare_models" for j in ctx.jobs.list(active_only=True)):
        raise HTTPException(status_code=409, detail="The models are already being prepared")

    async def run(job) -> dict:
        def downloading(what: str, share: tuple[float, float]):
            def report(done: int, total: int) -> None:
                frac = share[0] + (share[1] - share[0]) * min(done / max(total, 1), 1.0)
                job.update(f"Downloading {what}: {done} of about {total} MB", progress=frac)

            return report

        job.update("Getting the speech models ready", progress=0.0)
        await ctx.models.ensure_loaded(on_download=downloading("the speech models", (0.0, 0.7)))
        settings = ctx.settings
        final = await asyncio.to_thread(resolve_transcriber, settings)
        if settings.live_transcription and settings.live_transcriber != final:
            # The live transcript's model (Parakeet on the CPU), when the final one is another.
            job.update("Getting the live transcript's model ready", progress=0.7)
            live = ctx.models.live_transcriber
            if not live.is_loaded():
                report = downloading("the live transcript's model", (0.7, 0.9))
                await watched(live.load(), [hf_cache()], TRANSCRIBER_MB["parakeet"], report)
            if not ctx.active_recordings:
                await ctx.models.unload_live()
        job.update("Getting the search model ready", progress=0.9)
        await watched(
            asyncio.to_thread(ctx.index.embedder),
            [hf_cache()],
            SEARCH_MB,
            downloading("the search model", (0.9, 0.99)),
        )
        return {"cache_mb": round(await asyncio.to_thread(_cache_bytes) / MB)}

    return ctx.jobs.submit("prepare_models", run)


def _cache_bytes() -> int:
    from ...services.model_downloads import folder_bytes, hf_cache

    return folder_bytes([hf_cache()])


class AttachRequest(ApiModel):
    pid: int


class AttachResponse(ApiModel):
    watching: bool  # this backend now belongs to that app and ends when it does


@router.post("/system/attach", response_model=AttachResponse)
async def attach(request: AttachRequest, ctx: AppContext = Depends(get_ctx)):
    """A relaunched desktop app takes over the backend its crashed predecessor left running
    (api/app_watch.py). A backend not started by the app (server mode) stays independent."""
    watch = ctx.app_watch
    return AttachResponse(watching=watch is not None and watch.attach(request.pid))


class Diagnostics(ApiModel):
    text: str
    log_file: str  # where the backend log is, on the backend's machine


@router.get("/system/diagnostics", response_model=Diagnostics, dependencies=ADMIN)
async def diagnostics(ctx: AppContext = Depends(get_ctx)):
    """Versions, GPU, engines, jobs, settings without secrets and the log tail, as text."""
    from ...logs import log_path
    from ...services.diagnostics import build_report

    text = await asyncio.to_thread(build_report, ctx)
    return Diagnostics(text=text, log_file=str(log_path(ctx.settings.data_dir)))
