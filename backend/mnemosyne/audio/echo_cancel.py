"""PipeWire echo cancellation without touching the user's PipeWire config.

`libpipewire-module-echo-cancel` (WebRTC AEC) is loaded into a long-lived
`pw-cli` child in *monitor mode*: the default output's monitor is the
reference signal, so nothing has to be rerouted. It exposes a virtual source
("Mnemosyne: mic (echo cancelled)") that users pick as their microphone. The
module lives exactly as long as the child process; stopping it (or the
backend exiting) removes the source again.

With noise suppression (audio/noise_suppression.py) the same child also loads DeepFilterNet
behind the canceller: the canceller's source is then the internal "mnemosyne_aec_raw" and the
visible source, still "mnemosyne_aec_source", comes out of the noise suppressor.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

from .noise_suppression import RAW_SOURCE_NODE, Denoise, filter_chain_command

logger = logging.getLogger(__name__)

SOURCE_NODE = "mnemosyne_aec_source"
CAPTURE_NODE = "mnemosyne_aec_capture"
SOURCE_DESCRIPTION = "Mnemosyne: mic (echo cancelled)"

MODULE_PATHS = (
    "/usr/lib64/pipewire-0.3/libpipewire-module-echo-cancel.so",
    "/usr/lib/pipewire-0.3/libpipewire-module-echo-cancel.so",
    "/usr/lib/x86_64-linux-gnu/pipewire-0.3/libpipewire-module-echo-cancel.so",
    "/usr/lib/aarch64-linux-gnu/pipewire-0.3/libpipewire-module-echo-cancel.so",
)


# PipeWire node names are plain identifiers; anything else is not put into the module args.
_NODE_NAME = re.compile(r"^[A-Za-z0-9_.:@+-]+$")


def valid_mic(mic: str | None) -> str | None:
    """The mic node name to capture from, or None for the default source. Never our own
    echo-cancelled source (it would feed on itself), never a malformed name."""
    if not mic:
        return None
    if mic.startswith("mnemosyne_aec") or not _NODE_NAME.match(mic):
        raise ValueError(f"Cannot use {mic!r} as the echo canceller's microphone")
    return mic


def load_command(
    source_name: str = SOURCE_NODE,
    description: str = SOURCE_DESCRIPTION,
    mic: str | None = None,
    capture_name: str = CAPTURE_NODE,
    denoise: Denoise | None = None,
) -> str:
    """pw-cli commands loading the module. With `mic` (a node.name) the capture side is pinned
    to that microphone and never moved; without it WirePlumber picks the default source.
    With `denoise` the canceller works in mono without WebRTC's own (milder) noise suppression
    and DeepFilterNet's filter chain becomes `source_name`."""
    mic = valid_mic(mic)
    target = f' target.object = "{mic}" node.dont-reconnect = true' if mic else ""
    aec_source, aec_description, extra = source_name, description, ""
    if denoise is not None:
        aec_source, aec_description = RAW_SOURCE_NODE, "Mnemosyne: echo canceller (internal)"
        extra = (
            "audio.channels = 1 audio.position = [ MONO ] "
            "aec.args = { webrtc.noise_suppression = false } "
        )
    command = (
        f"load-module libpipewire-module-echo-cancel {{ monitor.mode = true {extra}"
        f'capture.props = {{ node.name = "{capture_name}" '
        f'node.description = "Mnemosyne AEC capture"{target} }} '
        f'source.props = {{ node.name = "{aec_source}" node.description = "{aec_description}" }} '
        "}\n"
    )
    if denoise is not None:
        command += filter_chain_command(denoise, aec_source, source_name)
    return command


@dataclass
class EchoCancelStatus:
    supported: bool
    reason: str | None
    active: bool
    source_node_id: int | None
    mic: str | None = None  # node.name the running module captures from (None: default source)
    denoise: Denoise | None = None  # the noise suppressor it runs with, if any
    # Noise suppression was asked for and did not start (the canceller runs without it).
    denoise_error: str | None = None


class EchoCancelManager:
    def __init__(self, pw_cli: str = "pw-cli"):
        self.pw_cli = pw_cli
        self._proc: asyncio.subprocess.Process | None = None
        self.mic: str | None = None  # what the running module captures from
        # The noise suppressor asked for behind it; denoise_error when it did not start.
        self.denoise: Denoise | None = None
        self.denoise_error: str | None = None
        self._watch_task: asyncio.Task | None = None

    # ---- capability ----------------------------------------------------

    def supported(self) -> tuple[bool, str | None]:
        if shutil.which(self.pw_cli) is None:
            return False, "pw-cli not found (PipeWire tools not installed)"
        if not any(Path(p).is_file() for p in MODULE_PATHS):
            return (
                False,
                "libpipewire-module-echo-cancel.so not installed (pipewire-module-* package)",
            )
        return True, None

    # ---- lifecycle -----------------------------------------------------

    @property
    def active(self) -> bool:
        return self._proc is not None and self._proc.returncode is None

    async def start(
        self, mic: str | None = None, denoise: Denoise | None = None
    ) -> EchoCancelStatus:
        """Load the module capturing from `mic` (a node.name; None = the default source), with
        DeepFilterNet behind it when `denoise` is given. Already running otherwise: restart it.
        When the noise suppressor does not come up, the canceller starts without it."""
        mic = valid_mic(mic)
        ok, reason = self.supported()
        if not ok:
            return EchoCancelStatus(False, reason, False, None)
        if self.active:
            if mic == self.mic and denoise == self.denoise:
                return await self.status()
            await self.stop()
        st = await self._load(mic, denoise)
        error = None
        if denoise is not None and not st.active:
            logger.warning("Noise suppression did not start; echo cancellation without it")
            error = "The noise suppressor did not start"
            st = await self._load(mic, None)
        self.mic, self.denoise, self.denoise_error = mic, denoise, error
        st.denoise_error = error if st.active else None
        if st.denoise is not None and self._proc is not None:
            self._watch_task = asyncio.create_task(self._watch(self._proc))
        return st

    async def _watch(self, proc: asyncio.subprocess.Process) -> None:
        """DeepFilterNet aborts the process it runs in once it falls a second behind (a computer
        too busy for it): bring the mic back without it, and say so."""
        await proc.wait()
        if proc is not self._proc:  # stopped or restarted on purpose
            return
        logger.warning(
            "The noise suppressor's pw-cli exited (%s); echo cancellation without it",
            proc.returncode,
        )
        self._proc = None
        mic, denoise = self.mic, self.denoise
        await self._load(mic, None)
        self.mic, self.denoise = mic, denoise  # what was asked: not retried until it changes
        self.denoise_error = "The noise suppressor stopped (the computer was too busy for it)"

    async def _load(self, mic: str | None, denoise: Denoise | None) -> EchoCancelStatus:
        logger.info(
            "Loading PipeWire echo-cancel module (monitor mode, mic %s%s)",
            mic or "default source",
            f", DeepFilterNet {denoise.limit_db} dB" if denoise else "",
        )
        self._proc = await asyncio.create_subprocess_exec(
            self.pw_cli,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        assert self._proc.stdin is not None
        self._proc.stdin.write(load_command(mic=mic, denoise=denoise).encode())
        self.mic = mic
        await self._proc.stdin.drain()
        # Give PipeWire a moment to create the nodes, then verify.
        for _ in range(20):
            await asyncio.sleep(0.1)
            node = await asyncio.to_thread(find_source_node)
            if node is not None:
                return EchoCancelStatus(True, None, True, node, mic, denoise)
        await self.stop()
        return EchoCancelStatus(True, "module loaded but no source appeared", False, None)

    async def stop(self) -> None:
        proc, self._proc = self._proc, None
        if proc is None or proc.returncode is not None:
            return
        logger.info("Unloading PipeWire echo-cancel module")
        try:
            if proc.stdin:
                proc.stdin.close()
            proc.terminate()
            await asyncio.wait_for(proc.wait(), timeout=3.0)
        except (TimeoutError, ProcessLookupError):
            proc.kill()
            await proc.wait()

    async def status(self) -> EchoCancelStatus:
        ok, reason = self.supported()
        node = await asyncio.to_thread(find_source_node) if ok else None
        active = self.active and node is not None
        return EchoCancelStatus(
            ok,
            reason,
            active,
            node,
            self.mic if active else None,
            self.denoise if active and not self.denoise_error else None,
            self.denoise_error if active else None,
        )


def find_source_node(node_name: str = SOURCE_NODE) -> int | None:
    """Return the PipeWire node id of the echo-cancelled source, if present."""
    try:
        result = subprocess.run(["pw-dump"], capture_output=True, text=True, timeout=5)
        if result.returncode != 0:
            return None
        for obj in json.loads(result.stdout):
            if obj.get("type") != "PipeWire:Interface:Node":
                continue
            props = obj.get("info", {}).get("props", {})
            if props.get("node.name") == node_name:
                return obj["id"]
    except Exception:
        logger.debug("pw-dump failed", exc_info=True)
    return None
