"""Public tunnels for guest notes: a way for people outside this network to reach a local port.

Two kinds, tried in this order (`open_tunnel`):
- Tailscale Funnel, when Tailscale runs on this computer: `https://<machine>.<tailnet>.ts.net`.
- A Cloudflare quick tunnel (`cloudflared tunnel --url ...`), `https://<random>.trycloudflare.com`,
  with a pinned cloudflared fetched on first use (services/downloads.py, checked by SHA-256).

A tunnel only carries bytes; the rooms behind it are end-to-end encrypted. Failures are
`TunnelError` with a message that can be shown to the person.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import platform
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any, Literal, Protocol

from . import downloads, team_host
from .downloads import Download, DownloadError

logger = logging.getLogger(__name__)

START_TIMEOUT = 30.0
STOP_GRACE = 5.0

# Pinned 2026-10-25: the newest release at least two weeks old (2026.10.0, published 2026-10-05).
# The digests are the `digest` fields of the GitHub release assets.
CLOUDFLARED_VERSION = "2026.10.0"
_BASE = f"https://github.com/cloudflare/cloudflared/releases/download/{CLOUDFLARED_VERSION}"
CLOUDFLARED: dict[str, Download] = {
    "amd64": Download(
        url=f"{_BASE}/cloudflared-linux-amd64",
        sha256="d33ff2d14475178d2012c2c56beba87389ac5ded27649519f198a7d3134a99db",
        size=40_129_756,
        name=f"cloudflared-{CLOUDFLARED_VERSION}-amd64",
    ),
    "arm64": Download(
        url=f"{_BASE}/cloudflared-linux-arm64",
        sha256="e6422b9d4f72d3194bc5a38676f13667c06666523217b842a877d72a80b5ac08",
        size=37_687_584,
        name=f"cloudflared-{CLOUDFLARED_VERSION}-arm64",
    ),
}
_ARCH = {"x86_64": "amd64", "amd64": "amd64", "aarch64": "arm64", "arm64": "arm64"}

URL_RE = re.compile(r"https://[a-z0-9-]+\.trycloudflare\.com")


class TunnelError(RuntimeError):
    pass


class Tunnel(Protocol):
    url: str
    kind: Literal["funnel", "cloudflare"]

    async def stop(self) -> None: ...


def cloudflared_download() -> Download | None:
    """The pinned cloudflared for this machine (None: Linux x86_64 and arm64 only)."""
    if platform.system() != "Linux":
        return None
    return CLOUDFLARED.get(_ARCH.get(platform.machine().lower(), ""))


def ensure_cloudflared(app: Any, progress: downloads.ProgressFn | None = None) -> Path:
    """The cloudflared binary under `<data_dir>/tools`, downloaded and verified when missing
    (blocking: run in a thread). `app` is the AppContext (anything with `.settings.data_dir`)."""
    item = cloudflared_download()
    if item is None:
        raise TunnelError("cloudflared is not available on this computer")
    folder = Path(app.settings.data_dir) / "tools"
    try:
        path = downloads.fetch(folder, item, progress)
    except DownloadError as e:
        raise TunnelError(str(e)) from e
    path.chmod(0o755)
    return path


class QuickTunnel:
    """A Cloudflare quick tunnel to 127.0.0.1:<port>, run as a child process."""

    kind: Literal["funnel", "cloudflare"] = "cloudflare"

    def __init__(self, binary: str | Path, port: int):
        self.binary = str(binary)
        self.port = port
        self.url = ""
        self._proc: asyncio.subprocess.Process | None = None
        self._log: asyncio.Task | None = None

    async def start(self) -> str:
        try:
            self._proc = await asyncio.create_subprocess_exec(
                self.binary,
                "tunnel",
                "--url",
                f"http://127.0.0.1:{self.port}",
                "--no-autoupdate",
                # an empty config, so a ~/.cloudflared/config.yml cannot turn this into a named
                # tunnel (quick tunnels refuse to start beside one)
                "--config",
                "/dev/null",
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.PIPE,
            )
        except OSError as e:
            raise TunnelError(f"Could not start cloudflared: {e}") from e
        found: asyncio.Future[str] = asyncio.get_running_loop().create_future()
        self._log = asyncio.create_task(self._read_stderr(self._proc, found))
        try:
            self.url = await asyncio.wait_for(found, START_TIMEOUT)
        except TimeoutError:
            await self.stop()
            raise TunnelError(
                "The tunnel did not come up in time. Check this computer's internet connection."
            ) from None
        except TunnelError:
            await self.stop()
            raise
        return self.url

    @staticmethod
    async def _read_stderr(proc: asyncio.subprocess.Process, found: asyncio.Future[str]) -> None:
        tail: list[str] = []
        while line := await proc.stderr.readline():
            text = line.decode(errors="replace").rstrip()
            logger.debug("cloudflared: %s", text)
            tail = [*tail, text][-5:]
            if not found.done() and (m := URL_RE.search(text)):
                found.set_result(m.group(0))
        if not found.done():
            code = await proc.wait()
            detail = next((t for t in reversed(tail) if t), "")
            found.set_exception(
                TunnelError(
                    f"cloudflared stopped (code {code})" + (f": {detail}" if detail else "")
                )
            )

    def alive(self) -> bool:
        return self._proc is not None and self._proc.returncode is None

    async def stop(self) -> None:
        proc, self._proc = self._proc, None
        if proc is not None and proc.returncode is None:
            with contextlib.suppress(ProcessLookupError):
                proc.terminate()
            try:
                await asyncio.wait_for(proc.wait(), STOP_GRACE)
            except TimeoutError:
                with contextlib.suppress(ProcessLookupError):
                    proc.kill()
                await proc.wait()
        if self._log is not None:
            self._log.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._log
            self._log = None


def _tailscale(args: list[str], timeout: float = 30) -> subprocess.CompletedProcess:
    return subprocess.run(["tailscale", *args], capture_output=True, text=True, timeout=timeout)


class Funnel:
    """Tailscale Funnel to 127.0.0.1:<port> on HTTPS port 443."""

    kind: Literal["funnel", "cloudflare"] = "funnel"

    def __init__(self, port: int):
        self.port = port
        self.url = ""

    @staticmethod
    def possible() -> bool:
        if not shutil.which("tailscale"):
            return False
        try:
            out = _tailscale(["status", "--json"], timeout=5)
            return out.returncode == 0 and json.loads(out.stdout).get("BackendState") == "Running"
        except (OSError, subprocess.TimeoutExpired, ValueError, AttributeError):
            return False

    def _start(self) -> str:
        name = team_host.tailscale_name().rstrip(".")
        if not name:
            raise TunnelError(
                "Tailscale has no HTTPS name for this computer. Turn on HTTPS certificates "
                "in the Tailscale admin console."
            )
        try:
            out = _tailscale(["funnel", "--bg", "--https=443", f"http://127.0.0.1:{self.port}"])
        except (OSError, subprocess.TimeoutExpired) as e:
            raise TunnelError(f"tailscale funnel failed: {e}") from e
        if out.returncode != 0:
            detail = (out.stderr or out.stdout).strip()
            raise TunnelError(f"Tailscale Funnel refused: {detail or 'unknown error'}")
        self.url = f"https://{name}"
        return self.url

    async def start(self) -> str:
        return await asyncio.to_thread(self._start)

    async def stop(self) -> None:
        def off() -> None:
            try:
                _tailscale(["funnel", "--https=443", "off"])
            except (OSError, subprocess.TimeoutExpired) as e:
                logger.warning("tailscale funnel off failed: %s", e)

        await asyncio.to_thread(off)
        self.url = ""


async def open_tunnel(app: Any, port: int) -> Tunnel:
    """A Funnel when Tailscale runs, else a Cloudflare quick tunnel. Raises TunnelError."""
    if await asyncio.to_thread(Funnel.possible):
        funnel = Funnel(port)
        await funnel.start()
        return funnel
    binary = await asyncio.to_thread(ensure_cloudflared, app)
    quick = QuickTunnel(binary, port)
    await quick.start()
    return quick
