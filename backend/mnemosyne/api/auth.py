"""Optional bearer-token auth for server mode.

Off unless `api_token` is set. Then every /api path and /ws needs the token in
`Authorization: Bearer <token>` or `?token=` (for <audio> elements and the
WebSocket, which cannot set headers). /health, /docs, /openapi.json, the phone page, the web
app's files and redeeming a pairing code stay open. A paired phone's own token
(services/pairing.py) opens only DEVICE_PATHS, what the phone page needs; a paired computer's
opens everything.
"""

from __future__ import annotations

import hmac
from urllib.parse import parse_qs

from starlette.types import ASGIApp, Receive, Scope, Send

from ..services.pairing import DESKTOP
from .context import AppContext

OPEN_PATHS = {
    "/health",
    "/docs",
    "/openapi.json",
    "/redoc",
    "/docs/oauth2-redirect",
    "/m",
    "/api/pairing/redeem",
}
DEVICE_PATHS = {"/api/audio/import", "/api/pairing/me"}


def is_open(path: str) -> bool:
    """Paths anyone may load: the list above, and everything outside the API and WebSocket,
    which is the web app's own files (api/web.py; the code is not a secret, the data is)."""
    return path in OPEN_PATHS or not (path.startswith("/api/") or path == "/ws")


class TokenAuthMiddleware:
    def __init__(self, app: ASGIApp, ctx: AppContext):
        self.app = app
        self.ctx = ctx

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] not in ("http", "websocket"):
            return await self.app(scope, receive, send)
        token = self.ctx.settings.api_token
        path = scope.get("path", "")
        if not token or is_open(path) or scope.get("method") == "OPTIONS":
            return await self.app(scope, receive, send)

        presented = ""
        headers = {k.decode().lower(): v.decode() for k, v in scope.get("headers", [])}
        auth = headers.get("authorization", "")
        if auth.lower().startswith("bearer "):
            presented = auth[7:].strip()
        if not presented:
            qs = parse_qs(scope.get("query_string", b"").decode())
            presented = (qs.get("token") or [""])[0]

        if hmac.compare_digest(presented.encode(), token.encode()):
            return await self.app(scope, receive, send)
        device = self.ctx.pairing.verify(presented)
        if device is not None and (device.kind == DESKTOP or path in DEVICE_PATHS):
            scope.setdefault("state", {})["device_id"] = device.id
            return await self.app(scope, receive, send)

        if scope["type"] == "websocket":
            await send({"type": "websocket.close", "code": 4401})
            return
        body = b'{"detail":"Missing or invalid API token"}'
        await send(
            {
                "type": "http.response.start",
                "status": 401,
                "headers": [
                    (b"content-type", b"application/json"),
                    (b"content-length", str(len(body)).encode()),
                    (b"www-authenticate", b"Bearer"),
                ],
            }
        )
        await send({"type": "http.response.body", "body": body})


LOCKED_OPEN = ("/api/encryption", "/api/system")


class LockedMiddleware:
    """While the meetings are encrypted and the key is missing, the API answers 423 except
    for what unlocking needs (the UI then asks for the recovery code)."""

    def __init__(self, app: ASGIApp, ctx: AppContext):
        self.app = app
        self.ctx = ctx

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        path = scope.get("path", "")
        if (
            scope["type"] != "http"
            or not self.ctx.locked
            or not path.startswith("/api/")
            or path.startswith(LOCKED_OPEN)
            or scope.get("method") == "OPTIONS"
        ):
            return await self.app(scope, receive, send)
        body = b'{"detail":"Meetings are encrypted and locked: enter the recovery code"}'
        await send(
            {
                "type": "http.response.start",
                "status": 423,
                "headers": [
                    (b"content-type", b"application/json"),
                    (b"access-control-allow-origin", b"*"),
                ],
            }
        )
        await send({"type": "http.response.body", "body": body})
