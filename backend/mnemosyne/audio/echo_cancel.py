"""PipeWire echo cancellation without touching the user's PipeWire config.

`libpipewire-module-echo-cancel` (WebRTC AEC) is loaded into a long-lived
`pw-cli` child in *monitor mode*: the default output's monitor is the
reference signal, so nothing has to be rerouted. It exposes a virtual source
("Mnemosyne: mic (echo cancelled)") that users pick as their microphone. The
module lives exactly as long as the child process; stopping it (or the
backend exiting) removes the source again.

With noise suppression (audio/noise_suppression.py) a second child loads DeepFilterNet behind
the canceller: the canceller's source is then the internal "mnemosyne_aec_raw" (what our
recordings read) and the visible source, still "mnemosyne_aec_source", comes out of the noise
suppressor. DeepFilterNet aborts its process when it falls behind; being its own child, that
leaves the canceller and any recording of it alone, and a plain copy takes the visible source over.
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
    """pw-cli command loading the module. With `mic` (a node.name) the capture side is pinned
    to that microphone and never moved; without it WirePlumber picks the default source.
    With `denoise` the canceller works in mono without WebRTC's own (milder) noise suppression
    and its source is the internal one the noise suppressor's chain (`chain_command`) reads."""
    mic = valid_mic(mic)
    target = f' target.object = "{mic}" node.dont-reconnect = true' if mic else ""
    extra = ""
    if denoise is not None:
        source_name, description = RAW_SOURCE_NODE, "Mnemosyne: echo canceller (internal)"
        extra = (
            "audio.channels = 1 audio.position = [ MONO ] "
            "aec.args = { webrtc.noise_suppression = false } "
        )
    return (
        f"load-module libpipewire-module-echo-cancel {{ monitor.mode = true {extra}"
        f'capture.props = {{ node.name = "{capture_name}" '
        f'node.description = "Mnemosyne AEC capture"{target} }} '
        f'source.props = {{ node.name = "{source_name}" node.description = "{description}" }} '
        "}\n"
    )


def chain_command(denoise: Denoise | None) -> str:
    """pw-cli command for the visible source behind the canceller's internal one: DeepFilterNet,
    or with None a plain copy (after the noise suppressor crashed)."""
    return filter_chain_command(denoise, RAW_SOURCE_NODE, SOURCE_NODE)


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
        self._proc: asyncio.subprocess.Process | None = None  # the canceller
        # The noise suppressor's chain (or the plain copy standing in for it), its own child.
        self._chain: asyncio.subprocess.Process | None = None
        self.mic: str | None = None  # what the running module captures from
        # The noise suppressor asked for behind it; denoise_error when it did not start.
        self.denoise: Denoise | None = None
        self.denoise_error: str | None = None
        # The running canceller's source is the internal one behind the chain (what recordings
        # read, so a crashed noise suppressor never touches them).
        self.raw_source = False
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
        When the noise suppressor does not come up, a plain copy of the canceller's audio is
        the visible source instead."""
        mic = valid_mic(mic)
        ok, reason = self.supported()
        if not ok:
            return EchoCancelStatus(False, reason, False, None)
        if self.active:
            if mic == self.mic and denoise == self.denoise:
                return await self.status()
            await self.stop()
        self.mic, self.denoise, self.denoise_error = mic, denoise, None
        if denoise is None:
            return await self._load(mic)
        st = await self._load(mic, denoise)
        if not st.active:  # the mono canceller did not come up: try it as it always ran
            logger.warning("Echo cancellation for noise suppression did not start; plain")
            self.denoise_error = "The noise suppressor did not start"
            st = await self._load(mic)
            st.denoise_error = self.denoise_error if st.active else None
            return st
        node = await self._load_chain(denoise)
        if node is None:
            logger.warning("Noise suppression did not start; echo cancellation without it")
            self.denoise_error = "The noise suppressor did not start"
            node = await self._load_chain(None)
            if node is None:
                await self.stop()
                return EchoCancelStatus(True, "module loaded but no source appeared", False, None)
            return EchoCancelStatus(True, None, True, node, mic, None, self.denoise_error)
        assert self._chain is not None
        self._watch_task = asyncio.create_task(self._watch(self._chain))
        return EchoCancelStatus(True, None, True, node, mic, denoise)

    async def _watch(self, chain: asyncio.subprocess.Process) -> None:
        """DeepFilterNet aborts the process it runs in once it falls a second behind (a computer
        too busy for it). The canceller, in its own process, keeps running, and so does any
        recording of it; a plain copy brings the visible source back, and the status says so."""
        await chain.wait()
        if chain is not self._chain or not self.active:  # stopped or restarted on purpose
            return
        logger.warning(
            "The noise suppressor's pw-cli exited (%s); echo cancellation without it",
            chain.returncode,
        )
        self._chain = None
        # What was asked stays in self.denoise: not retried until it changes.
        self.denoise_error = "The noise suppressor stopped (the computer was too busy for it)"
        if await self._load_chain(None) is None:
            logger.warning("The plain copy behind the echo canceller did not start either")

    async def _spawn(self, command: str) -> asyncio.subprocess.Process:
        proc = await asyncio.create_subprocess_exec(
            self.pw_cli,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        assert proc.stdin is not None
        proc.stdin.write(command.encode())
        await proc.stdin.drain()
        return proc

    async def _appears(self, node_name: str) -> int | None:
        """Give PipeWire a moment to create the node, then verify."""
        for _ in range(20):
            await asyncio.sleep(0.1)
            node = await asyncio.to_thread(find_source_node, node_name)
            if node is not None:
                return node
        return None

    async def _load(self, mic: str | None, denoise: Denoise | None = None) -> EchoCancelStatus:
        """The canceller; with `denoise` in mono with the internal source, chain not loaded yet."""
        logger.info(
            "Loading PipeWire echo-cancel module (monitor mode, mic %s%s)",
            mic or "default source",
            f", DeepFilterNet {denoise.limit_db} dB behind it" if denoise else "",
        )
        self._proc = await self._spawn(load_command(mic=mic, denoise=denoise))
        self.mic, self.raw_source = mic, denoise is not None
        node = await self._appears(RAW_SOURCE_NODE if denoise else SOURCE_NODE)
        if node is not None:
            return EchoCancelStatus(True, None, True, node, mic, denoise)
        await self.stop()
        return EchoCancelStatus(True, "module loaded but no source appeared", False, None)

    async def _load_chain(self, denoise: Denoise | None) -> int | None:
        """The visible source behind the canceller: DeepFilterNet, or with None a plain copy.
        Its node id, or None (and nothing left running) when it did not appear."""
        self._chain = await self._spawn(chain_command(denoise))
        node = await self._appears(SOURCE_NODE)
        if node is None:
            await self._end(self._chain)
            self._chain = None
        return node

    async def stop(self) -> None:
        chain, self._chain = self._chain, None
        proc, self._proc = self._proc, None
        if chain is not None:
            await self._end(chain)
        if proc is not None and proc.returncode is None:
            logger.info("Unloading PipeWire echo-cancel module")
            await self._end(proc)

    @staticmethod
    async def _end(proc: asyncio.subprocess.Process) -> None:
        if proc.returncode is not None:
            return
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
