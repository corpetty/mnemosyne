"""Remote access: runs the `mnemosyne-link home` sidecar (link/ in the repo) while the
`remote_access` setting is on.

The sidecar holds this machine's iroh key (<data_dir>/link.key), accepts end-to-end encrypted
connections from computers paired as "desktop" devices (services/pairing.py, which it reads
from paired_devices.json), and forwards them to this backend on 127.0.0.1. Devices elsewhere
reach it through n0's public relays when a direct connection is not possible; relays only see
encrypted packets. On start it prints one JSON line: its endpoint id and ticket.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import shutil
from dataclasses import dataclass
from pathlib import Path

from ..config import backend_dir

logger = logging.getLogger(__name__)

START_TIMEOUT = 30.0
RESTART_DELAY = 5.0


def find_binary() -> str | None:
    """MNEMOSYNE_LINK_BIN, then PATH, then a build in the repo's link/ (development)."""
    env = os.environ.get("MNEMOSYNE_LINK_BIN")
    if env:
        return env if Path(env).is_file() else None
    found = shutil.which("mnemosyne-link")
    if found:
        return found
    for profile in ("release", "debug"):
        dev = backend_dir().parent / "link" / "target" / profile / "mnemosyne-link"
        if dev.is_file():
            return str(dev)
    return None


@dataclass
class LinkStatus:
    running: bool = False
    endpoint_id: str | None = None
    ticket: str | None = None
    error: str | None = None


class LinkService:
    def __init__(self, data_dir: Path, port: int, binary: str | None = None):
        self.data_dir = data_dir
        self.port = port
        self.binary = binary
        self.status = LinkStatus()
        self._proc: asyncio.subprocess.Process | None = None
        self._task: asyncio.Task | None = None

    @property
    def active(self) -> bool:
        return self._task is not None

    def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._supervise())

    async def stop(self) -> None:
        task, self._task = self._task, None
        if task is not None:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        await self._kill()
        self.status = LinkStatus()

    async def _kill(self) -> None:
        proc, self._proc = self._proc, None
        if proc is not None and proc.returncode is None:
            proc.terminate()
            try:
                await asyncio.wait_for(proc.wait(), 5)
            except TimeoutError:
                proc.kill()
                await proc.wait()

    async def _supervise(self) -> None:
        while True:
            binary = self.binary or find_binary()
            if binary is None:
                self.status = LinkStatus(
                    error="mnemosyne-link is not installed (set MNEMOSYNE_LINK_BIN)"
                )
                return
            try:
                await self._run(binary)
                self.status = LinkStatus(error="remote access stopped; restarting")
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001 - report and retry
                logger.warning("Remote access failed: %s", e)
                self.status = LinkStatus(error=str(e))
            finally:
                await self._kill()
            await asyncio.sleep(RESTART_DELAY)

    async def _run(self, binary: str) -> None:
        self._proc = await asyncio.create_subprocess_exec(
            binary,
            "home",
            "--backend",
            f"127.0.0.1:{self.port}",
            "--data-dir",
            str(self.data_dir),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stderr = asyncio.create_task(self._log_stderr(self._proc))
        try:
            line = await asyncio.wait_for(self._proc.stdout.readline(), START_TIMEOUT)
            if not line:
                raise RuntimeError("mnemosyne-link exited before it was ready")
            info = json.loads(line)
            self.status = LinkStatus(
                running=True, endpoint_id=info["endpoint_id"], ticket=info["ticket"]
            )
            logger.info("Remote access on, endpoint %s", info["endpoint_id"])
            code = await self._proc.wait()
            raise RuntimeError(f"mnemosyne-link exited with code {code}")
        finally:
            stderr.cancel()

    @staticmethod
    async def _log_stderr(proc: asyncio.subprocess.Process) -> None:
        while line := await proc.stderr.readline():
            logger.info("link: %s", line.decode(errors="replace").rstrip())
