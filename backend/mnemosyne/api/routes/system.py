"""What this machine can do, for the setup wizard."""

import asyncio
import ctypes
import importlib.util
import platform
import shutil

from fastapi import APIRouter, Depends

from ...models.base import ApiModel
from ..context import AppContext, get_ctx

router = APIRouter(prefix="/api", tags=["system"])


class SystemInfo(ApiModel):
    gpu_driver: bool  # an NVIDIA driver: nvidia-smi on PATH, or libcuda loadable
    gpu_stack: bool  # torch and WhisperX are installed (the gpu extra)
    parakeet: bool  # onnx-asr is installed (the onnx extra)
    pipewire: bool  # pw-record and pw-dump are available
    ffmpeg: bool
    hf_token: bool  # needed for speaker identification (pyannote)
    platform: str


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
    return SystemInfo(
        gpu_driver=_gpu_driver(),
        gpu_stack=_has("torch") and _has("whisperx"),
        parakeet=_has("onnx_asr"),
        pipewire=bool(shutil.which("pw-record") and shutil.which("pw-dump")),
        ffmpeg=bool(shutil.which("ffmpeg")),
        hf_token=bool(ctx.settings.hf_token),
        platform=f"{platform.system()} {platform.release()}",
    )


class Diagnostics(ApiModel):
    text: str
    log_file: str  # where the backend log is, on the backend's machine


@router.get("/system/diagnostics", response_model=Diagnostics)
async def diagnostics(ctx: AppContext = Depends(get_ctx)):
    """Versions, GPU, engines, jobs, settings without secrets and the log tail, as text."""
    from ...logs import log_path
    from ...services.diagnostics import build_report

    text = await asyncio.to_thread(build_report, ctx)
    return Diagnostics(text=text, log_file=str(log_path(ctx.settings.data_dir)))
