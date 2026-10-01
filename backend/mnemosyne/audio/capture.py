"""PipeWire audio capture: device enumeration and multi-source recording.

A browser can be the recorder instead (a team server, api/routes/record.py): its sources are
recording processes like pw-record's, with a BrowserProcess and negative device ids, and the
WebSocket that receives its audio writes the same kind of growing WAV file. Everything after
(levels, health, live transcription, saving, recovery) reads the files and cannot tell.
"""

import asyncio
import json
import struct
import subprocess
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class AudioDevice:
    id: int
    name: str
    description: str
    media_class: str  # "Audio/Source", "Audio/Sink"
    is_monitor: bool = False
    is_echo_cancelled: bool = False  # our virtual AEC source (audio/echo_cancel.py)

    @property
    def is_input(self) -> bool:
        return "Source" in self.media_class

    @property
    def is_output(self) -> bool:
        return "Sink" in self.media_class


@dataclass
class RecordingProcess:
    device_id: int
    process: asyncio.subprocess.Process  # or a BrowserProcess
    output_path: Path
    # A browser's source ("mic" or "system") and what it called it; None for a PipeWire device,
    # whose kind and description come from the device list.
    source: str | None = None
    label: str | None = None


class BrowserProcess:
    """Stands in for pw-record when a browser sends the audio: the recording WebSocket appends
    to the file while `returncode` is None; stopping the capture ends it."""

    def __init__(self):
        self.returncode: int | None = None

    def terminate(self) -> None:
        self.returncode = 0

    def kill(self) -> None:
        self.returncode = -9

    async def wait(self) -> int | None:
        return self.returncode


BROWSER_SOURCES = {"mic": -1, "system": -2}  # device ids of a browser's sources


def is_browser(recording: "RecordingSession") -> bool:
    return any(isinstance(p.process, BrowserProcess) for p in recording.processes)


def source_of(proc: RecordingProcess, devices: dict) -> str:
    """ "system" for what the computer plays (a sink, or a browser's shared call audio)."""
    if proc.source is not None:
        return proc.source
    device = devices.get(proc.device_id)
    return "system" if (device is not None and device.is_output) else "mic"


def label_of(proc: RecordingProcess, devices: dict) -> str:
    if proc.label is not None:
        return proc.label
    device = devices.get(proc.device_id)
    return device.description if device else str(proc.device_id)


def wav_header(sample_rate: int, channels: int = 1) -> bytes:
    """A 16-bit PCM WAV header for a file still being written: sizes are fixed on stop
    (services/recovery.py repair_wav), and readers here go by the file size."""
    return (
        b"RIFF"
        + struct.pack("<I", 0xFFFFFFFF)
        + b"WAVEfmt "
        + struct.pack(
            "<IHHIIHH", 16, 1, channels, sample_rate, sample_rate * channels * 2, channels * 2, 16
        )
        + b"data"
        + struct.pack("<I", 0xFFFFFFFF)
    )


def start_browser_recording(
    sources: list[str], output_dir: Path, sample_rate: int, labels: dict[str, str]
) -> "RecordingSession":
    """A recording whose sources a browser sends (16-bit mono PCM at `sample_rate`)."""
    output_dir.mkdir(parents=True, exist_ok=True)
    recording = RecordingSession(session_id=str(uuid.uuid4())[:8], output_dir=output_dir)
    defaults = {"mic": "Browser microphone", "system": "Call audio (browser)"}
    for source in dict.fromkeys(sources):
        path = output_dir / f"{recording.session_id}_browser_{source}.wav"
        path.write_bytes(wav_header(sample_rate))
        recording.processes.append(
            RecordingProcess(
                device_id=BROWSER_SOURCES[source],
                process=BrowserProcess(),
                output_path=path,
                source=source,
                label=(labels.get(source) or defaults[source])[:80],
            )
        )
    recording.is_recording = True
    return recording


@dataclass
class RecordingSession:
    session_id: str
    output_dir: Path
    processes: list[RecordingProcess] = field(default_factory=list)
    is_recording: bool = False
    part: int = 0  # which part of its meeting this recording is (services/parts.py)
    started_at: float = field(default_factory=time.time)  # wall clock, for a UI that reconnects
    # Per device id: its PipeWire node name (ids change when a node is recreated, names do
    # not: a restart finds the device again by name) and its description, for messages.
    node_names: dict[int, str] = field(default_factory=dict)
    labels: dict[int, str] = field(default_factory=dict)
    problems: dict[int, str] = field(default_factory=dict)  # audio/health.py: device -> state


def list_devices() -> list[AudioDevice]:
    """List available PipeWire audio devices using pw-dump."""
    try:
        result = subprocess.run(["pw-dump"], capture_output=True, text=True, timeout=5)
    except subprocess.TimeoutExpired as e:
        raise RuntimeError("PipeWire is not answering (pw-dump timed out)") from e
    if result.returncode != 0:
        raise RuntimeError(f"pw-dump failed: {result.stderr}")

    data = json.loads(result.stdout)
    devices = []

    for obj in data:
        if obj.get("type") != "PipeWire:Interface:Node":
            continue
        props = obj.get("info", {}).get("props", {})
        media_class = props.get("media.class", "")

        if "Audio" not in media_class:
            continue
        if "Source" not in media_class and "Sink" not in media_class:
            continue

        node_name = props.get("node.name", "")
        is_monitor = ".monitor" in node_name or "Monitor" in props.get("node.description", "")

        devices.append(
            AudioDevice(
                id=obj["id"],
                name=node_name,
                description=props.get("node.description", props.get("node.name", "unknown")),
                media_class=media_class,
                is_monitor=is_monitor,
                is_echo_cancelled=node_name == "mnemosyne_aec_source",
            )
        )

    return devices


def build_record_command(
    device: AudioDevice,
    output_path: Path,
    sample_rate: int,
    channels: int,
    format: str,
    node_name: str | None = None,
) -> list[str]:
    """pw-record command for one device.

    Sinks are captured from their monitor ports with `stream.capture.sink=true`
    targeting the sink node itself. (`<sink>.monitor` is a PulseAudio name that
    PipeWire does not resolve: pw-record then silently falls back to the default
    microphone.) Sources are targeted by node name, which survives restarts.
    """
    cmd = ["pw-record", f"--rate={sample_rate}", f"--channels={channels}", f"--format={format}"]
    # Named so the meeting-app monitor (audio/streams.py) can tell our streams apart.
    props = ["application.name=Mnemosyne"]
    if device.is_output:
        props.append("stream.capture.sink=true")
    if node_name:  # lets pw-link output be matched to this exact recorder
        props.append(f"node.name={node_name}")
    cmd += ["-P", "{ " + " ".join(props) + " }"]
    cmd += ["--target", device.name or str(device.id), str(output_path)]
    return cmd


async def start_recording(
    device_ids: list[int],
    output_dir: Path,
    sample_rate: int = 48000,
    channels: int = 1,
    format: str = "s16",
) -> RecordingSession:
    """Start recording from one or more PipeWire devices.

    For sink devices (outputs), we automatically use their monitor source
    to capture system audio.
    """
    output_dir.mkdir(parents=True, exist_ok=True)
    session_id = str(uuid.uuid4())[:8]
    session = RecordingSession(session_id=session_id, output_dir=output_dir)

    devices = {d.id: d for d in await asyncio.to_thread(list_devices)}

    for device_id in device_ids:
        device = devices.get(device_id)
        if device is None:
            continue

        output_path = output_dir / f"{session_id}_device_{device_id}.wav"

        # Build pw-record command
        cmd = build_record_command(device, output_path, sample_rate, channels, format)

        # Nothing reads pw-record's output: a pipe would fill up with its warnings over a long
        # meeting and then block it, stopping the recording.
        process = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )

        session.processes.append(
            RecordingProcess(
                device_id=device_id,
                process=process,
                output_path=output_path,
            )
        )

    session.is_recording = True
    return session


async def convert_to_opus(wav_path: Path, bitrate: str = "64k") -> Path:
    """Convert a WAV file to OGG/Opus and remove the original WAV."""
    opus_path = wav_path.with_suffix(".ogg")
    process = await asyncio.create_subprocess_exec(
        "ffmpeg",
        "-y",
        "-i",
        str(wav_path),
        "-c:a",
        "libopus",
        "-b:a",
        bitrate,
        str(opus_path),
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.PIPE,
    )
    _, stderr = await process.communicate()
    if process.returncode != 0:
        raise RuntimeError(f"ffmpeg conversion failed: {stderr.decode()}")
    wav_path.unlink()
    return opus_path


async def _terminate(rec: RecordingProcess) -> None:
    if rec.process.returncode is None:
        rec.process.terminate()
        try:
            await asyncio.wait_for(rec.process.wait(), timeout=5.0)
        except TimeoutError:
            rec.process.kill()
            await rec.process.wait()


async def stop_capture(session: RecordingSession) -> None:
    """Stop every recorder now (the click is the end of the recording). Idempotent."""
    await asyncio.gather(*(_terminate(rec) for rec in session.processes))
    session.is_recording = False


async def stop_recording(session: RecordingSession) -> list[Path | None]:
    """Stop all recording processes, convert each WAV to Opus (in parallel), return the
    Opus paths in process order: None for a device that recorded nothing (so the others keep
    their own device and label)."""
    await stop_capture(session)

    async def one(rec: RecordingProcess) -> Path | None:
        path = rec.output_path
        if not path.exists() or path.stat().st_size == 0:
            return None
        if isinstance(rec.process, BrowserProcess):
            from ..services.recovery import repair_wav

            if await asyncio.to_thread(repair_wav, path) <= 0:  # the header only: no audio
                path.unlink(missing_ok=True)
                return None
        return await convert_to_opus(path)

    return list(await asyncio.gather(*(one(rec) for rec in session.processes)))
