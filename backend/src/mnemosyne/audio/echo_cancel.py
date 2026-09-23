"""PipeWire echo cancellation without touching the user's PipeWire config.

`libpipewire-module-echo-cancel` (WebRTC AEC) is loaded into a long-lived
`pw-cli` child in *monitor mode*: the default output's monitor is the
reference signal, so nothing has to be rerouted. It exposes a virtual source
("Mnemosyne: mic (echo cancelled)") that users pick as their microphone. The
module lives exactly as long as the child process; stopping it (or the
backend exiting) removes the source again.
"""

from __future__ import annotations

import asyncio
import json
import logging
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

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


def load_command(source_name: str = SOURCE_NODE, description: str = SOURCE_DESCRIPTION) -> str:
    return (
        "load-module libpipewire-module-echo-cancel { monitor.mode = true "
        f'capture.props = {{ node.name = "{CAPTURE_NODE}" '
        'node.description = "Mnemosyne AEC capture" } '
        f'source.props = {{ node.name = "{source_name}" node.description = "{description}" }} '
        "}\n"
    )


@dataclass
class EchoCancelStatus:
    supported: bool
    reason: str | None
    active: bool
    source_node_id: int | None


class EchoCancelManager:
    def __init__(self, pw_cli: str = "pw-cli"):
        self.pw_cli = pw_cli
        self._proc: asyncio.subprocess.Process | None = None

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

    async def start(self) -> EchoCancelStatus:
        ok, reason = self.supported()
        if not ok:
            return EchoCancelStatus(False, reason, False, None)
        if self.active:
            return await self.status()
        logger.info("Loading PipeWire echo-cancel module (monitor mode)")
        self._proc = await asyncio.create_subprocess_exec(
            self.pw_cli,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        assert self._proc.stdin is not None
        self._proc.stdin.write(load_command().encode())
        await self._proc.stdin.drain()
        # Give PipeWire a moment to create the nodes, then verify.
        for _ in range(20):
            await asyncio.sleep(0.1)
            node = find_source_node()
            if node is not None:
                return EchoCancelStatus(True, None, True, node)
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
        node = find_source_node() if ok else None
        return EchoCancelStatus(ok, reason, self.active and node is not None, node)


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
