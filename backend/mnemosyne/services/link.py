"""Remote access: runs the `mnemosyne-link home` sidecar (link/ in the repo) while the
`remote_access` setting is on.

The sidecar holds this machine's iroh key (<data_dir>/link.key), accepts end-to-end encrypted
connections from computers paired as "desktop" devices (services/pairing.py, which it reads
from paired_devices.json), and forwards them to this backend on 127.0.0.1. Devices elsewhere
reach it through relays when a direct connection is not possible: those in the `remote_relays`
setting (iroh-relay servers the user runs), else Mnemosyne's own (services/ours.py), else none
(`--no-relay`: direct connections only). n0's public relays are never used. Relays only see
encrypted packets. On start it prints one JSON line: its endpoint id and ticket.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import secrets
import shutil
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path

from ..config import backend_dir
from .ours import parse_relays  # noqa: F401 - re-exported: callers import it from here

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


def relay_args(relays: list[str]) -> list[str]:
    """The sidecar's relay arguments: `--relay URL` for each, or `--no-relay` for none (it would
    use n0's public relays if given nothing)."""
    if not relays:
        return ["--no-relay"]
    return [arg for url in relays for arg in ("--relay", url)]


def child_env() -> dict[str, str]:
    """The sidecar's environment: ours without PUBLISH_*, the config of the publish host the
    sidecar embeds (`rooms`); a publish host the user runs elsewhere must not leak into it."""
    return {k: v for k, v in os.environ.items() if not k.startswith("PUBLISH_")}


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
    """Supervises one `mnemosyne-link` process. This class runs `home`; RoomsLink (below) runs
    `rooms` by overriding `_argv` and `_ready`."""

    label = "Remote access"  # what errors call the sidecar

    def __init__(self, data_dir: Path, port: int, binary: str | None = None):
        self.data_dir = data_dir
        self.port = port
        self.binary = binary
        self.relays: list[str] = []  # services/ours.py relays(); none: no relay at all
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
            raise LinkError(f"{self.label} is not running")
        request_id = uuid.uuid4().hex
        future: asyncio.Future = asyncio.get_running_loop().create_future()
        self._pending[request_id] = future
        try:
            line = json.dumps({"id": request_id, "op": op, **args}) + "\n"
            try:
                proc.stdin.write(line.encode())
                await proc.stdin.drain()
            except (ConnectionError, OSError) as e:
                raise LinkError(f"{self.label} is not running") from e
            try:
                reply = await asyncio.wait_for(future, REQUEST_TIMEOUT)
            except TimeoutError as e:
                raise LinkError(f"{self.label} did not answer {op} in time") from e
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
        self._fail_pending(f"{self.label} stopped")
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

    def _argv(self) -> list[str]:
        """The arguments after the binary."""
        return [
            "home",
            "--backend",
            f"127.0.0.1:{self.port}",
            "--data-dir",
            str(self.data_dir),
            *relay_args(self.relays),
        ]

    def _ready(self, info: dict) -> LinkStatus:
        """The status once the ready line (already parsed) has been read."""
        logger.info("Remote access on, endpoint %s", info["endpoint_id"])
        return LinkStatus(running=True, endpoint_id=info["endpoint_id"], ticket=info["ticket"])

    async def _run(self, binary: str) -> None:
        self._proc = await asyncio.create_subprocess_exec(
            binary,
            *self._argv(),
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=child_env(),
        )
        stderr = asyncio.create_task(self._log_stderr(self._proc))
        try:
            line = await asyncio.wait_for(self._proc.stdout.readline(), START_TIMEOUT)
            if not line:
                raise RuntimeError("mnemosyne-link exited before it was ready")
            info = json.loads(line)
            self.status = self._ready(info)
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


def ensure_upload_token(path: Path) -> str:
    """The token the rooms sidecar's embedded host wants in `X-Upload-Token`, in a 0600 file the
    sidecar reads (it refuses a missing, empty or group/world-readable one). Created if absent,
    reused otherwise; a file with looser permissions is tightened, an empty one refilled."""
    path.parent.mkdir(parents=True, exist_ok=True)
    token = ""
    with contextlib.suppress(OSError):
        token = path.read_text().strip()
    if not token:
        token = secrets.token_urlsafe(32)
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            f.write(token + "\n")
    path.chmod(0o600)
    return token


class RoomsLink(LinkService):
    """`mnemosyne-link rooms`: the guest notes rooms (link/README.md). It embeds a publish host
    on a loopback port (`listen`) that the guest notes service uploads to with `upload_token`,
    and serves each room over iroh from one endpoint per room. Not tied to remote access.

    `relays` (services/ours.py `relays()`) is read when the process starts; a running one keeps
    the relays it started with. Closing our end of its stdin would keep it serving; stopping it
    closes every room but keeps their keys, so a restarted room has the same endpoint id."""

    label = "Guest rooms"

    def __init__(
        self,
        data_dir: Path,
        guest_dir: Path,
        viewer_dir: Path,
        binary: str | None = None,
    ):
        super().__init__(data_dir, 0, binary)
        self.guest_dir = guest_dir
        self.viewer_dir = viewer_dir
        self.rooms_dir = data_dir / "guest_rooms"
        self.token_file = self.rooms_dir / "upload.token"
        self.listen: str | None = None
        self.relay_kind: str | None = None
        # Called (in the background) when the sidecar is ready again after a crash: it serves no
        # room until each is started again (services/guest_notes.py re-serves the live ones).
        self.on_restart: list[Callable[[], Awaitable[None]]] = []
        self.readies = 0  # ready lines since start()

    @property
    def upload_token(self) -> str:
        return ensure_upload_token(self.token_file)

    @property
    def base_url(self) -> str | None:
        """The embedded host, once the sidecar is ready."""
        return f"http://{self.listen}" if self.listen and self.status.running else None

    def _argv(self) -> list[str]:
        ensure_upload_token(self.token_file)
        return [
            "rooms",
            "--data-dir",
            str(self.data_dir),
            "--guest-dir",
            str(self.guest_dir),
            "--viewer-dir",
            str(self.viewer_dir),
            "--upload-token-file",
            str(self.token_file),
            *relay_args(self.relays),
        ]

    def _ready(self, info: dict) -> LinkStatus:
        self.listen = info["listen"]
        self.relay_kind = info.get("relay_kind", "none")
        logger.info("Guest rooms on, host %s (%s relays)", self.listen, self.relay_kind)
        self.readies += 1
        if self.readies > 1:
            for hook in self.on_restart:
                task = asyncio.create_task(self._run_hook(hook))
                self._hooks.add(task)
                task.add_done_callback(self._hooks.discard)
        return LinkStatus(running=True)

    def start(self) -> None:
        if self._task is not None and self._task.done():
            self._task = None  # it gave up (no binary): a new start tries again
        if self._task is None:
            self.status = LinkStatus()
            self.readies = 0
        super().start()

    async def stop(self) -> None:
        await super().stop()
        self.listen = self.relay_kind = None

    async def wait_ready(self, timeout: float = START_TIMEOUT + 5) -> None:
        """Wait for the ready line. Raises LinkError when the sidecar is missing or dies first."""
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while not self.status.running:
            if self._task is None:
                raise LinkError(f"{self.label} are not started")
            if self.status.error:
                raise LinkError(self.status.error)
            if loop.time() > deadline:
                raise LinkError(f"{self.label} did not start in time")
            await asyncio.sleep(0.02)

    @staticmethod
    def _fields(reply: dict) -> dict:
        return {k: v for k, v in reply.items() if k not in ("id", "op", "ok")}

    async def room_start(self, room: str, relay: str | None = None) -> dict:
        """Serve a room (16 hex) over iroh: `{endpoint_id, relay}`. A room already served is
        returned unchanged."""
        args = {"room": room}
        if relay:
            args["relay"] = relay
        return self._fields(await self.request("room_start", **args))

    async def room_stop(self, room: str) -> bool:
        """Stop serving a room and delete its key (started again, it gets a new id)."""
        return bool((await self.request("room_stop", room=room)).get("closed"))

    async def room_list(self) -> list[dict]:
        """The rooms being served: `[{room, endpoint_id, relay}]`."""
        return list((await self.request("room_list")).get("rooms") or [])
