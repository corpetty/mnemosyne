"""Remote access: runs the `mnemosyne-link home` sidecar (link/ in the repo) while the
`remote_access` setting is on.

The sidecar holds this machine's iroh key (<data_dir>/link.key), accepts end-to-end encrypted
connections from computers paired as "desktop" devices (services/pairing.py, which it reads
from paired_devices.json), and forwards them to this backend on 127.0.0.1. Devices elsewhere
reach it through relays when a direct connection is not possible: n0's public ones, or those in
the `remote_relays` setting (iroh-relay servers we or the user run). Relays only see encrypted
packets. On start it prints one JSON line: its endpoint id and ticket.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import shutil
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path

from ..config import backend_dir

logger = logging.getLogger(__name__)

START_TIMEOUT = 30.0
RESTART_DELAY = 5.0
REQUEST_TIMEOUT = 30.0


class LinkError(Exception):
    """A control request the sidecar refused, or could not be sent or answered."""


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


def endpoint_id_from_ticket(ticket: str) -> str:
    """The endpoint id (64 hex characters) an endpoint ticket names: `endpoint` + lowercase
    base32 of a postcard value whose first byte is the variant (0) and then the 32-byte id."""
    import base64
    import binascii

    text = ticket.strip()
    if not text.startswith("endpoint"):
        raise ValueError("Not an endpoint ticket")
    body = text[len("endpoint") :].upper()
    try:
        raw = base64.b32decode(body + "=" * (-len(body) % 8))
    except (binascii.Error, ValueError) as e:
        raise ValueError("Not an endpoint ticket") from e
    if len(raw) < 33 or raw[0] != 0:
        raise ValueError("Not an endpoint ticket")
    return raw[1:33].hex()


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
        self.relays: list[str] = []  # blank: n0's public relays
        self.status = LinkStatus()
        self._proc: asyncio.subprocess.Process | None = None
        self._task: asyncio.Task | None = None
        self._pending: dict[str, asyncio.Future] = {}
        # Called (in the background) each time the sidecar is ready: sync peers are dialed again.
        self.on_ready: list[Callable[[], Awaitable[None]]] = []
        self._hooks: set[asyncio.Task] = set()

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
        for hook in list(self._hooks):
            hook.cancel()
        self.status = LinkStatus()

    async def request(self, op: str, **args) -> dict:
        """Send a control request to the running sidecar (link/README.md, "Control channel") and
        wait for its reply. Raises LinkError when it is not running, refuses (`ok: false`) or
        does not answer within REQUEST_TIMEOUT."""
        proc = self._proc
        if not self.status.running or proc is None or proc.stdin is None or proc.returncode:
            raise LinkError("Remote access is not running")
        request_id = uuid.uuid4().hex
        future: asyncio.Future = asyncio.get_running_loop().create_future()
        self._pending[request_id] = future
        try:
            line = json.dumps({"id": request_id, "op": op, **args}) + "\n"
            try:
                proc.stdin.write(line.encode())
                await proc.stdin.drain()
            except (ConnectionError, OSError) as e:
                raise LinkError("Remote access is not running") from e
            try:
                reply = await asyncio.wait_for(future, REQUEST_TIMEOUT)
            except TimeoutError as e:
                raise LinkError(f"Remote access did not answer {op} in time") from e
        finally:
            self._pending.pop(request_id, None)
        if not reply.get("ok"):
            raise LinkError(str(reply.get("error") or f"{op} failed"))
        return reply

    def _dispatch(self, line: bytes) -> None:
        try:
            reply = json.loads(line)
        except ValueError:
            reply = None
        future = self._pending.get(reply.get("id")) if isinstance(reply, dict) else None
        if future is None:
            logger.info("link: unexpected output: %s", line.decode(errors="replace").rstrip())
        elif not future.done():
            future.set_result(reply)

    def _fail_pending(self, message: str) -> None:
        for future in list(self._pending.values()):
            if not future.done():
                future.set_exception(LinkError(message))

    async def _kill(self) -> None:
        proc, self._proc = self._proc, None
        self._fail_pending("Remote access stopped")
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
            *[arg for url in self.relays for arg in ("--relay", url)],
            stdin=asyncio.subprocess.PIPE,
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
            for hook in self.on_ready:
                task = asyncio.create_task(self._run_hook(hook))
                self._hooks.add(task)
                task.add_done_callback(self._hooks.discard)
            while line := await self._proc.stdout.readline():
                self._dispatch(line)
            code = await self._proc.wait()
            raise RuntimeError(f"mnemosyne-link exited with code {code}")
        finally:
            stderr.cancel()

    @staticmethod
    async def _run_hook(hook: Callable[[], Awaitable[None]]) -> None:
        try:
            await hook()
        except Exception as e:  # noqa: BLE001 - a failed redial must not stop remote access
            logger.warning("After remote access started: %s", e)

    @staticmethod
    async def _log_stderr(proc: asyncio.subprocess.Process) -> None:
        while line := await proc.stderr.readline():
            logger.info("link: %s", line.decode(errors="replace").rstrip())
