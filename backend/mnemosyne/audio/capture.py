"""PipeWire audio capture: device enumeration and multi-source recording."""

import asyncio
import json
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
    process: asyncio.subprocess.Process
    output_path: Path


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
    result = subprocess.run(["pw-dump"], capture_output=True, text=True, timeout=5)
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

    devices = {d.id: d for d in list_devices()}

    for device_id in device_ids:
        device = devices.get(device_id)
        if device is None:
            continue

        output_path = output_dir / f"{session_id}_device_{device_id}.wav"

        # Build pw-record command
        cmd = build_record_command(device, output_path, sample_rate, channels, format)

        process = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
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
        return await convert_to_opus(path)

    return list(await asyncio.gather(*(one(rec) for rec in session.processes)))
