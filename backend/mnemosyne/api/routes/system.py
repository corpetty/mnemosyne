"""What this machine can do, for the setup wizard."""

import asyncio
import ctypes
import importlib.util
import platform
import shutil
import sys
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Request

from ... import access
from ...config import dedicated_team_server, hosts_team, personal_allowed
from ...jobs import Job
from ...models.base import ApiModel
from ...services import link, phonon_install
from ..auth import require_owner_channel
from ..context import AppContext, get_ctx

router = APIRouter(prefix="/api", tags=["system"])
ADMIN = [Depends(access.require_admin)]  # team-wide: an admin's job (access.py)


MAC_CAPTURE_PROBLEM = (
    "Recording on a Mac comes in a later version. "
    "You can already import recordings and transcribe them."
)


def _capture() -> tuple[bool, str | None]:
    """Whether this computer can record, and if not, why."""
    if sys.platform == "darwin":
        return False, MAC_CAPTURE_PROBLEM
    if shutil.which("pw-record") and shutil.which("pw-dump"):
        return True, None
    return (
        False,
        "The PipeWire tools (pw-record and pw-dump) are missing, and recording needs them.",
    )


class SystemInfo(ApiModel):
    gpu_driver: bool  # an NVIDIA driver: nvidia-smi on PATH, or libcuda loadable
    gpu_stack: bool  # torch and WhisperX are installed (the gpu extra)
    parakeet: bool  # onnx-asr is installed (the onnx extra)
    capture: bool  # this computer can record (Linux: pw-record and pw-dump are available)
    capture_problem: str | None  # why not, in words for the person; None when it can
    ffmpeg: bool
    hf_token: bool  # needed for speaker identification (pyannote)
    onnx_diarizer: bool  # sherpa-onnx is installed: speakers told apart on the CPU
    # What this machine runs for the current settings (registry.resolve_*): "auto" and choices
    # whose packages are missing resolve to what works here.
    transcriber_in_use: str
    live_transcriber_in_use: str  # the live transcript's (CPU only: Phonon-2 or Parakeet for auto)
    diarizer_in_use: str
    platform: str
    problems: list[str]  # what went wrong when the backend started (config, recovery, ...)
    # For the desktop shell, which installs the gpu extra in the background: not before the
    # first-run setup is answered, and not when it was declined ("off").
    setup_complete: bool
    gpu_support: str  # auto | off
    # Phonon-2 (English): installed by Mnemosyne (services/phonon_install.py), its size on disk,
    # and whether it can be installed here (uv found).
    phonon: bool
    phonon_bytes: int
    phonon_installable: bool
    # mnemosyne-link with `notes merge` found: a re-summarize keeps hand edits
    # (services/summary_doc.py)
    summary_merge: bool


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
    from ...transcription.registry import (
        resolve_diarizer,
        resolve_live_transcriber,
        resolve_transcriber,
    )

    # May import torch to see whether CUDA works: not on the event loop.
    transcriber = await asyncio.to_thread(resolve_transcriber, ctx.settings)
    live = await asyncio.to_thread(resolve_live_transcriber, ctx.settings)
    diarizer = await asyncio.to_thread(resolve_diarizer, ctx.settings)
    capture, capture_problem = _capture()
    return SystemInfo(
        onnx_diarizer=_has("sherpa_onnx"),
        transcriber_in_use=transcriber,
        live_transcriber_in_use=live,
        diarizer_in_use=diarizer,
        gpu_driver=_gpu_driver(),
        gpu_stack=_has("torch") and _has("whisperx"),
        parakeet=_has("onnx_asr"),
        capture=capture,
        capture_problem=capture_problem,
        ffmpeg=bool(shutil.which("ffmpeg")),
        hf_token=bool(ctx.settings.hf_token),
        platform=f"{platform.system()} {platform.release()}",
        problems=ctx.startup_problems,
        setup_complete=ctx.settings.setup_complete,
        gpu_support=ctx.settings.gpu_support,
        phonon=phonon_install.installed(ctx.settings.data_dir),
        phonon_bytes=await asyncio.to_thread(phonon_install.size_bytes, ctx.settings.data_dir),
        phonon_installable=phonon_install.uv_binary() is not None,
        summary_merge=link.find_binary() is not None,
    )


class SystemRole(ApiModel):
    # "main": this computer holds the library and does the work. "team": several people sign in
    # to it (team_mode). "Uses my main computer" is a role of the desktop shell, which alone
    # knows it is connected to another backend; it is never answered here.
    role: Literal["main", "team"]
    hosts_team: bool  # a main computer that also hosts a team (role stays "main")
    personal: bool  # personal features (sync, pairing, contacts, sharing) exist here


@router.get("/system/role", response_model=SystemRole)
async def system_role(ctx: AppContext = Depends(get_ctx)):
    s = ctx.settings
    return SystemRole(
        role="team" if dedicated_team_server(s) else "main",
        hosts_team=hosts_team(s),
        personal=personal_allowed(s),
    )


class MainComputerStatus(ApiModel):
    connected: bool  # this computer uses a main computer (the shell said so)
    name: str  # its name ("" when none)
    url: str  # the desktop shell's tunnel to it ("" when none); the token is never returned


class MainComputerRequest(ApiModel):
    url: str  # the shell's tunnel, http://127.0.0.1:<port>
    token: str  # the API token the pairing returned
    name: str = ""


def _desktop_app_only(request: Request, ctx: AppContext = Depends(get_ctx)) -> None:
    """Only the desktop shell on this machine (auth.py `from_desktop_app`): never the network,
    the team listener or the tunnel, whatever token the request carries."""
    from ..auth import request_from_desktop_app

    if not request_from_desktop_app(ctx, request):
        raise HTTPException(status_code=403, detail="Only the desktop app on this computer")


SHELL_ONLY = [Depends(_desktop_app_only)]
# The main computer this one uses is the owner's own business: on a computer that hosts a team
# the guard comes first, then the shell check.
MAIN_ONLY = [Depends(require_owner_channel), Depends(_desktop_app_only)]


def _main_status(ctx: AppContext) -> MainComputerStatus:
    main = ctx.main_computer
    if main is None:
        return MainComputerStatus(connected=False, name="", url="")
    return MainComputerStatus(connected=True, name=main.name, url=main.url)


@router.get("/system/main", response_model=MainComputerStatus, dependencies=MAIN_ONLY)
async def get_main_computer(ctx: AppContext = Depends(get_ctx)):
    """The main computer this one uses, as the desktop shell last said."""
    return _main_status(ctx)


@router.put("/system/main", response_model=MainComputerStatus, dependencies=MAIN_ONLY)
async def set_main_computer(body: MainComputerRequest, ctx: AppContext = Depends(get_ctx)):
    """The desktop shell connected to the main computer through remote access (and at each
    launch): where its tunnel is, the token and the name (services/main_computer.py). Not
    while this computer is in a team (services/team_member.py): one or the other."""
    team = ctx.team_member.membership if ctx.team_member is not None else None
    if team is not None:
        raise HTTPException(
            status_code=409, detail=f"This computer is in {team.host_name}: leave the team first"
        )
    try:
        ctx.main_computer_store().set(body.url, body.token, body.name)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    from ...services import satellite

    satellite.service(ctx).kick()  # recordings waiting for it go now
    return _main_status(ctx)


@router.delete("/system/main", response_model=MainComputerStatus, dependencies=MAIN_ONLY)
async def clear_main_computer(ctx: AppContext = Depends(get_ctx)):
    """The desktop shell disconnected from the main computer (forgot it)."""
    ctx.main_computer_store().clear()
    return _main_status(ctx)


@router.post("/system/phonon", response_model=Job, dependencies=ADMIN)
async def install_phonon(ctx: AppContext = Depends(get_ctx)):
    """Install Phonon-2 for English transcription (an `install_phonon` job, about 1 GB)."""
    if any(j.kind == "install_phonon" for j in ctx.jobs.list(active_only=True)):
        raise HTTPException(status_code=409, detail="Phonon is already being installed")

    async def run(job) -> dict:
        await phonon_install.install(
            ctx.settings.data_dir, lambda message, frac: job.update(message, progress=frac)
        )
        # An engine built before (with Parakeet in Phonon's place) is rebuilt on next use.
        busy = ctx.active_recordings or any(
            j.kind == "transcribe" for j in ctx.jobs.list(active_only=True)
        )
        if not busy:
            await ctx.models.unload()
        return {"mb": round(phonon_install.size_bytes(ctx.settings.data_dir) / 1e6)}

    return ctx.jobs.submit("install_phonon", run)


@router.delete("/system/phonon", dependencies=ADMIN)
async def remove_phonon(ctx: AppContext = Depends(get_ctx)):
    """Remove Phonon (environment, model, caches); a transcriber set to it uses Parakeet."""
    if (
        any(j.kind == "transcribe" for j in ctx.jobs.list(active_only=True))
        or ctx.active_recordings
    ):
        raise HTTPException(status_code=409, detail="Not while recording or transcribing")
    await ctx.models.unload()
    await asyncio.to_thread(phonon_install.remove, ctx.settings.data_dir)
    return {"message": "Phonon removed"}


@router.post("/system/prepare", response_model=Job, dependencies=ADMIN)
async def prepare_models(ctx: AppContext = Depends(get_ctx)):
    """Download the speech and search models now (a `prepare_models` job), so the first
    recording, transcription and search do not wait for them: what setup does at the end."""
    from ...services.model_downloads import MB, SEARCH_MB, TRANSCRIBER_MB, hf_cache, watched
    from ...transcription.registry import resolve_live_transcriber, resolve_transcriber

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
        if (
            settings.live_transcription
            and await asyncio.to_thread(resolve_live_transcriber, settings) != final
        ):
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
