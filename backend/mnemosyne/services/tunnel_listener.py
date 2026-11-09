"""The tunnel listener: a loopback port of its own for everything the link sidecar carries in.

Paired computers, sync peers and other people's computers (contacts) reach this backend through
`mnemosyne-link home` (services/link.py), which opens a TCP connection on this machine for each of
their streams. Those connections must never count as this machine's own requests (the desktop
window, which needs no token). They used to be told apart by their source address, 127.0.0.2,
but where that cannot be bound (macOS: only 127.0.0.1 is routed on lo0) the sidecar fell back to
127.0.0.1 and a remote peer became this computer. So the sidecar now connects to this listener
instead, and every request arriving here is marked `scope["tunnel"] = True` by which server it
came in on, whatever its source address (api/auth.py `is_tunnelled`). Nothing falls back: when
the listener is not running, the sidecar is not started, and a stream it cannot connect fails.

A second uvicorn server on the same app (like the team listener, services/team_host.py), bound
to 127.0.0.1 on a port the OS picks: nothing else needs to know it but the sidecar, which is
handed it on its command line, and an OS-picked port never collides with another backend (the
e2e tests run several) or with the shell's own remote-access port (8009).
"""

from __future__ import annotations

import asyncio
import logging
import socket

from starlette.types import ASGIApp, Receive, Scope, Send

logger = logging.getLogger(__name__)


class Tunnelled:
    """Marks every connection it serves as tunnelled: `scope["tunnel"] = True`."""

    def __init__(self, app: ASGIApp):
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        scope["tunnel"] = True
        await self.app(scope, receive, send)


class TunnelListener:
    def __init__(self):
        self._server = None
        self._task: asyncio.Task | None = None
        self.port = 0  # 0 while not listening
        self.error = ""

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done() and self.port != 0

    async def ensure(self, app) -> int:
        """Listen if not listening yet; returns the port. Raises RuntimeError when it cannot
        (no app to serve, or the socket cannot be bound)."""
        if self.running:
            return self.port
        await self.stop()
        if app is None:
            raise RuntimeError("the tunnel listener has no app to serve")
        import uvicorn

        try:
            sock = socket.create_server(("127.0.0.1", 0))
        except OSError as e:
            raise RuntimeError(f"cannot listen for tunnelled connections: {e}") from e
        config = uvicorn.Config(
            Tunnelled(app),
            interface="asgi3",
            lifespan="off",  # the app is already running: this is only another door into it
            log_level="warning",
            proxy_headers=False,  # the sidecar never forwards headers; never trust any here
            ws_max_size=16 * 1024 * 1024,
        )
        server = uvicorn.Server(config)
        server.install_signal_handlers = lambda: None  # the main server owns the signals
        self._server = server
        self.error = ""
        self._task = asyncio.create_task(self._serve(server, sock))
        for _ in range(100):  # until it listens, or gives up
            await asyncio.sleep(0.05)
            if server.started or self._task.done():
                break
        if not server.started:
            await self.stop()
            sock.close()
            raise RuntimeError(self.error or "the tunnel listener did not start")
        self.port = sock.getsockname()[1]
        logger.info("Tunnelled connections arrive on 127.0.0.1:%d", self.port)
        return self.port

    async def _serve(self, server, sock: socket.socket) -> None:
        try:
            await server.serve(sockets=[sock])
        except (OSError, SystemExit) as e:
            self.error = f"the tunnel listener stopped: {e}"
            logger.warning(self.error)
        finally:
            if self._server is server:  # died on its own (stop() already cleared the port)
                self.port = 0

    async def stop(self) -> None:
        server, task, self._server, self._task = self._server, self._task, None, None
        self.port = 0
        if server is not None:
            server.should_exit = True
        if task is not None:
            try:
                await asyncio.wait_for(task, timeout=5)
            except (TimeoutError, asyncio.CancelledError):
                task.cancel()
