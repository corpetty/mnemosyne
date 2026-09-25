"""A plain-text diagnostics report to paste into an issue (Settings → Copy diagnostics).

People paste this in public, so it holds no secrets and little that is personal: text settings
are shown only for engine and model choices, everything else as <set>/<unset>, and the home
directory becomes ~. The log tail is included as is (it can name meetings); the UI says so.
"""

from __future__ import annotations

import importlib.metadata
import importlib.util
import os
import platform
import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING

from ..logs import log_path, tail

if TYPE_CHECKING:
    from ..api.context import AppContext

# Text settings safe to show: which engine, model or style, never names, hosts, URLs or keys.
SHOWN_TEXT = {
    "transcriber",
    "diarizer",
    "language",
    "live_transcriber",
    "whisper_model_size",
    "whisper_compute_type",
    "whisper_vad",
    "parakeet_model",
    "parakeet_quantization",
    "onnx_provider",
    "remote_stt_model",
    "diarization_model",
    "default_provider",
    "default_model",
    "embedding_model",
    "summary_style",
    "auto_record",
    "auto_record_ignore_apps",
    "jira_issue_type",
}


def _redact_home(text: str) -> str:
    home = str(Path.home())
    return text.replace(home, "~") if home not in ("", "/") else text


def _run(cmd: list[str], timeout: float = 2.0) -> str:
    if not shutil.which(cmd[0]):
        return "not found"
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired) as e:
        return f"failed ({e.__class__.__name__})"
    lines = (out.stdout or out.stderr).strip().splitlines()
    return lines[0] if lines else f"exit {out.returncode}"


def _os_name() -> str:
    try:
        for line in Path("/etc/os-release").read_text().splitlines():
            if line.startswith("PRETTY_NAME="):
                return line.split("=", 1)[1].strip('"')
    except OSError:
        pass
    return platform.platform()


def _install_kind() -> str:
    if Path("/.flatpak-info").exists():
        return "Flatpak"
    if os.environ.get("APPIMAGE"):
        return "AppImage"
    backend = Path(__file__).resolve().parents[2]
    if (backend.parent / ".git").exists():
        return "source checkout"
    return "deb/rpm"


def _version(dist: str) -> str:
    try:
        return importlib.metadata.version(dist)
    except importlib.metadata.PackageNotFoundError:
        return "not installed"


def _gpu() -> list[str]:
    query = ["nvidia-smi", "--query-gpu=name,driver_version,memory.total", "--format=csv,noheader"]
    lines = ["nvidia-smi: " + _run(query)]
    torch = sys.modules.get("torch")  # never import it just for this (slow)
    if torch is not None:
        try:
            cuda = torch.cuda.is_available()
        except Exception:
            cuda = False
        lines.append(f"torch {torch.__version__}, CUDA available: {cuda}")
    elif importlib.util.find_spec("torch"):
        lines.append("torch: installed, not loaded")
    else:
        lines.append("torch: not installed")
    return lines


def _engine_loaded(app: AppContext) -> str:
    engine = app.models._engine  # the property would build it
    if engine is None:
        return "no"
    try:
        return "yes" if engine.is_loaded() else "no"
    except Exception:
        return "unknown"


def _settings_lines(app: AppContext) -> list[str]:
    lines = []
    for name, value in app.settings.model_dump().items():
        if isinstance(value, str) and name not in SHOWN_TEXT:
            shown = "<set>" if value else "<unset>"
        elif isinstance(value, (str, Path)):
            shown = repr(_redact_home(str(value)))
        else:
            shown = repr(value)
        lines.append(f"{name} = {shown}")
    return lines


def build_report(app: AppContext, log_lines: int = 200) -> str:
    settings = app.settings
    jobs = app.jobs.list()
    active = [j for j in jobs if not j.is_terminal]
    failed = [j for j in jobs if j.status == "failed"][-5:]
    session = (
        f"{os.environ.get('XDG_SESSION_TYPE', '?')} / {os.environ.get('XDG_CURRENT_DESKTOP', '?')}"
    )
    out = [
        "## Mnemosyne diagnostics",
        f"generated {datetime.now().isoformat(timespec='seconds')}",
        "",
        "### System",
        f"backend {_version('mnemosyne-backend')} ({_install_kind()})",
        f"python {platform.python_version()}",
        f"os {_os_name()}, kernel {platform.release()}",
        f"session {session}",
        *_gpu(),
        f"whisperx {_version('whisperx')}, onnx-asr {_version('onnx-asr')}, "
        f"pyannote.audio {_version('pyannote.audio')}, nemo-toolkit {_version('nemo-toolkit')}",
        f"pw-record: {'found' if shutil.which('pw-record') else 'not found'}, "
        f"ffmpeg: {_run(['ffmpeg', '-hide_banner', '-version'])}",
        "",
        "### Engines",
        f"transcriber {settings.transcriber}, diarizer {settings.diarizer}, "
        f"live {settings.live_transcriber if settings.live_transcription else 'off'}",
        f"summaries {settings.default_provider}/{settings.default_model or 'default'}",
        f"transcription models loaded: {_engine_loaded(app)}",
        "",
        "### Jobs",
        *([f"active: {j.kind} {j.status} {j.message!r}" for j in active] or ["no active jobs"]),
        *[f"failed: {j.kind}: {j.error}" for j in failed],
        f"recordings in progress: {len(app.active_recordings)}",
        "",
        "### Settings (text values hidden except engine and model choices)",
        *_settings_lines(app),
        "",
        f"### Backend log (last {log_lines} lines)",
        *(tail(log_path(settings.data_dir), log_lines) or ["(no log file yet)"]),
    ]
    return _redact_home("\n".join(out))
