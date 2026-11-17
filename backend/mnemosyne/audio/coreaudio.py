"""Recording on a Mac: Core Audio through the `mnemosyne-capture` sidecar (capture/, Rust).

Where Linux has `pw-dump` and `pw-record`, a Mac has the sidecar, which keeps pw-record's
contract (a placeholder-header WAV that grows, stopped with SIGTERM), so levels, capture health,
the live transcript, recovery and the Opus conversion do not change. This module only builds its
commands and reads its JSON: audio/capture.py calls it on darwin.

The sidecar is `MNEMOSYNE_CAPTURE_BIN`, else `mnemosyne-capture` on PATH (the desktop shell puts
the app's Contents/MacOS there). Device `name` is the Core Audio UID (stable, what the UI saves);
`id` is the AudioObjectID; the extra entry named "system" is all apps' output.

Echo cancellation on a Mac is Apple's voice processing on the microphone itself: while it is on
(audio/echo_cancel.py `set_voice_processing`), microphones are recorded with
`--voice-processing`. There is no separate echo-cancelled source as with PipeWire.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
from pathlib import Path

from .capture import AudioDevice

logger = logging.getLogger(__name__)

BIN_NAME = "mnemosyne-capture"
SYSTEM_TARGET = "system"  # the sidecar's entry for all apps' output
LOG_NAME = "capture.log"
LOG_ROTATE_BYTES = 2_000_000  # capture.log becomes capture.log.1 at the next recording after this

# Where recorders append their event lines (`<data_dir>/logs/capture.log`): set once the data
# directory is known (AppContext.build). Until then recorders run without a log.
_data_dir: Path | None = None


# Echo cancellation is on: microphones are recorded through Apple's voice processing. Set by
# EchoCancelManager on darwin; read when a recorder's command is built.
_voice_processing = False

# The sidecar's `doctor` answer about voice processing, per sidecar file (path, mtime).
_vp_checked: dict[tuple[str, int], bool] = {}


def configure(data_dir: Path | None) -> None:
    global _data_dir
    _data_dir = Path(data_dir) if data_dir else None


def set_voice_processing(on: bool) -> None:
    global _voice_processing
    _voice_processing = bool(on)


def voice_processing() -> bool:
    """Microphones are recorded through voice processing (echo cancellation is on)."""
    return _voice_processing


def voice_processing_supported() -> tuple[bool, str | None]:
    """Whether this Mac's sidecar can record a microphone through voice processing, and why
    not. Asks the sidecar's `doctor` once per sidecar file."""
    binary = sidecar()
    if binary is None:
        return False, "The capture helper (mnemosyne-capture) is not available"
    try:
        key = (binary, Path(binary).stat().st_mtime_ns)
    except OSError:
        key = (binary, 0)
    if key not in _vp_checked:
        report = doctor()
        if report is None:  # not remembered: it may answer next time
            return False, "The capture helper did not answer"
        _vp_checked[key] = report.get("voice_processing") is True
    if _vp_checked[key]:
        return True, None
    return False, "Apple's voice processing is not available on this Mac"


def log_path(data_dir: Path) -> Path:
    return Path(data_dir) / "logs" / LOG_NAME


def sidecar() -> str | None:
    """The sidecar's path, or None when this build does not have it."""
    env = os.environ.get("MNEMOSYNE_CAPTURE_BIN")
    if env:
        return env if Path(env).is_file() else None
    return shutil.which(BIN_NAME)


def _run_json(subcommand: str, timeout: float):
    binary = sidecar()
    if binary is None:
        raise FileNotFoundError("the capture helper (mnemosyne-capture) was not found")
    out = subprocess.run(
        [binary, subcommand], capture_output=True, text=True, timeout=timeout, check=False
    )
    if out.returncode != 0:
        raise RuntimeError(f"exit {out.returncode}: {(out.stderr or '').strip()[:300]}")
    return json.loads(out.stdout)


def list_devices() -> list[AudioDevice]:
    """The sidecar's `devices`. Never raises: no sidecar, a crash or bad JSON give [] and a
    logged warning (the UI then shows no devices, the system info says why)."""
    try:
        data = _run_json("devices", timeout=10)
        if not isinstance(data, list):
            raise ValueError("devices did not print a list")
    except FileNotFoundError:
        logger.warning("Cannot list audio devices: the capture helper is not available")
        return []
    except Exception as e:
        logger.warning("Cannot list audio devices: %s: %s", e.__class__.__name__, e)
        return []
    devices = []
    for item in data:
        try:
            devices.append(
                AudioDevice(
                    id=int(item["id"]),
                    name=str(item["name"]),
                    description=str(item.get("description") or item["name"]),
                    media_class=str(item["media_class"]),
                    is_monitor=bool(item.get("is_monitor", False)),
                    is_echo_cancelled=bool(item.get("is_echo_cancelled", False)),
                )
            )
        except (KeyError, TypeError, ValueError):
            logger.warning("Ignoring a device the capture helper listed oddly: %r", item)
    return devices


def capture_processes(timeout: float = 5) -> list[dict]:
    """The sidecar's `apps`: processes with audio input running, `[{pid, bundle_id, name}]`
    (auto-record, audio/streams.py). Raises when the sidecar is missing or fails: the poll
    keeps its last answer and tries again."""
    data = _run_json("apps", timeout=timeout)
    if not isinstance(data, list):
        raise ValueError("apps did not print a list")
    return [item for item in data if isinstance(item, dict)]


def doctor() -> dict | None:
    """The sidecar's `doctor` JSON (diagnostics, the system info), or None if it cannot run."""
    try:
        data = _run_json("doctor", timeout=10)
    except Exception as e:
        logger.warning("The capture helper's doctor failed: %s: %s", e.__class__.__name__, e)
        return None
    return data if isinstance(data, dict) else None


def real_capture() -> bool:
    """This build can record a real microphone and system audio (not only the test sources)."""
    report = doctor()
    return bool(report and report.get("real_capture") is True)


def _recorder_log() -> Path | None:
    if _data_dir is None:
        return None
    path = log_path(_data_dir)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists() and path.stat().st_size > LOG_ROTATE_BYTES:
            path.replace(path.with_name(LOG_NAME + ".1"))
    except OSError:
        pass  # the sidecar tolerates a log it cannot open: the recording matters more
    return path


def build_record_command(
    device: AudioDevice,
    output_path: Path,
    sample_rate: int,
    channels: int,
    format: str = "s16",
    *,
    log: bool = True,
    voice_processing: bool = False,
) -> list[str]:
    """The sidecar command that records one device (`--target` takes the UID, or "system").

    The output is always 16-bit PCM; `format` exists for capture.build_record_command's
    signature. `log=False` for short measurements (level meters) that would only add noise.
    `voice_processing` records a microphone through Apple's voice processing (echo
    cancellation); it never applies to the system audio or a fake source.

    `MNEMOSYNE_CAPTURE_FAKE` (test-only, e.g. `tone` or `silence`) records the sidecar's fake
    source in place of the device, so the whole recording machinery can run without hardware.
    """
    cmd = [sidecar() or BIN_NAME, "record"]
    fake = os.environ.get("MNEMOSYNE_CAPTURE_FAKE")
    cmd += ["--fake", fake] if fake else ["--target", device.name or str(device.id)]
    if voice_processing and not fake and device.is_input and device.name != SYSTEM_TARGET:
        cmd.append("--voice-processing")
    cmd += ["--rate", str(sample_rate), "--channels", str(channels)]
    log_file = _recorder_log() if log else None
    if log_file is not None:
        cmd += ["--log", str(log_file)]
    cmd.append(str(output_path))
    return cmd
