"""Optional bearer-token auth for server mode.

Off unless `api_token` is set. Then every /api path and /ws needs the token in
`Authorization: Bearer <token>` or `?token=` (for <audio> elements and the
WebSocket, which cannot set headers). /health, /docs and /openapi.json stay open.
"""

from __future__ import annotations

import hmac
from urllib.parse import parse_qs

from starlette.types import ASGIApp, Receive, Scope, Send

from .context import AppContext

OPEN_PATHS = {"/health", "/docs", "/openapi.json", "/redoc", "/docs/oauth2-redirect"}


class TokenAuthMiddleware:
    def __init__(self, app: ASGIApp, ctx: AppContext):
        self.app = app
        self.ctx = ctx

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] not in ("http", "websocket"):
            return await self.app(scope, receive, send)
        token = self.ctx.settings.api_token
        path = scope.get("path", "")
        if not token or path in OPEN_PATHS or scope.get("method") == "OPTIONS":
            return await self.app(scope, receive, send)

        presented = ""
        headers = {k.decode().lower(): v.decode() for k, v in scope.get("headers", [])}
        auth = headers.get("authorization", "")
        if auth.lower().startswith("bearer "):
            presented = auth[7:].strip()
        if not presented:
            qs = parse_qs(scope.get("query_string", b"").decode())
            presented = (qs.get("token") or [""])[0]

        if hmac.compare_digest(presented, token):
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
