"""PipeWire audio capture: device enumeration and multi-source recording."""

import asyncio
import json
import subprocess
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional


@dataclass
class AudioDevice:
    id: int
    name: str
    description: str
    media_class: str  # "Audio/Source", "Audio/Sink"
    is_monitor: bool = False

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


def list_devices() -> list[AudioDevice]:
    """List available PipeWire audio devices using pw-dump."""
    result = subprocess.run(
        ["pw-dump"], capture_output=True, text=True, timeout=5
    )
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
        is_monitor = ".monitor" in node_name or "Monitor" in props.get(
            "node.description", ""
        )

        devices.append(
            AudioDevice(
                id=obj["id"],
                name=node_name,
                description=props.get(
                    "node.description", props.get("node.name", "unknown")
                ),
                media_class=media_class,
                is_monitor=is_monitor,
            )
        )

    return devices


def get_monitor_name_for_sink(sink_name: str) -> str:
    """Get the monitor source name for a given sink."""
    return f"{sink_name}.monitor"


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
    session = RecordingSession(
        session_id=session_id, output_dir=output_dir
    )

    devices = {d.id: d for d in list_devices()}

    for device_id in device_ids:
        device = devices.get(device_id)
        if device is None:
            continue

        output_path = output_dir / f"{session_id}_device_{device_id}.wav"

        # Build pw-record command
        cmd = [
            "pw-record",
            f"--rate={sample_rate}",
            f"--channels={channels}",
            f"--format={format}",
            "--target",
        ]

        if device.is_output:
            # For sinks, record from their monitor source
            cmd.append(get_monitor_name_for_sink(device.name))
        else:
            # For sources, record directly
            cmd.append(str(device_id))

        cmd.append(str(output_path))

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
        "ffmpeg", "-y", "-i", str(wav_path),
        "-c:a", "libopus", "-b:a", bitrate,
        str(opus_path),
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.PIPE,
    )
    _, stderr = await process.communicate()
    if process.returncode != 0:
        raise RuntimeError(f"ffmpeg conversion failed: {stderr.decode()}")
    wav_path.unlink()
    return opus_path


async def stop_recording(session: RecordingSession) -> list[Path]:
    """Stop all recording processes, convert to Opus, return output paths."""
    output_files = []

    for rec in session.processes:
        if rec.process.returncode is None:
            rec.process.terminate()
            try:
                await asyncio.wait_for(rec.process.wait(), timeout=5.0)
            except asyncio.TimeoutError:
                rec.process.kill()
                await rec.process.wait()

        if rec.output_path.exists() and rec.output_path.stat().st_size > 0:
            opus_path = await convert_to_opus(rec.output_path)
            output_files.append(opus_path)

    session.is_recording = False
    return output_files
