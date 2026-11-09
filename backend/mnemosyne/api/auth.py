"""Bearer-token auth for server mode, and who is signed in on a team server.

Off unless `api_token` is set or `team_mode` is on. Then every /api path and /ws needs a token in
`Authorization: Bearer <token>` or `?token=` (for <audio> elements and the
WebSocket, which cannot set headers). /health, /docs, /openapi.json, the phone page, the web
app's files and redeeming a pairing code stay open, and so does the desktop app's attach
(LOCAL_PATHS) when it comes from this machine: the shell does not know the token, and a
relaunched app that cannot attach loses the recording its crashed predecessor left running
(api/app_watch.py). A paired phone's own token
(services/pairing.py) opens only DEVICE_PATHS, what the phone page needs; a paired computer's
opens everything. Another person's computer (a CONTACT device, services/contacts.py) opens only
/api/share/ over HTTP, as that contact (a member principal), and only while the contact exists
and is not removed; never on a team server. A person's token (services/users.py) opens the
API as that person:
the request runs with them in `access.current`, which decides what they see (access.py).

Requests that came through the link sidecar's tunnel (other computers, other people's too)
reach this process from 127.0.0.2 (TUNNEL_CLIENTS; link/src/lib.rs TUNNEL_SOURCE). They are never
"this machine": a token is always required for them, even with no api_token and team mode off,
and neither LOCAL_PATHS nor the tokenless desktop owner apply. Open paths stay open (redeeming a
pairing code). If the sidecar could not bind 127.0.0.2 it connects from 127.0.0.1 and this
distinction is lost.

The CHANNEL a request came in on is recorded in `scope["state"]["channel"]` (one of CHANNELS),
with `on_team_port` and `loopback_main` beside it. On a main computer that also hosts a team
(config.hosts_team) the owner's own requests and the team's share one backend, and
`require_owner_channel` is how a personal route tells them apart
(docs/plans/2026-11-09-team-hosting.md 2.2). Open paths and requests refused with 401 get none.
"""

from __future__ import annotations

import hmac
from urllib.parse import parse_qs

from fastapi import HTTPException, Request
from starlette.types import ASGIApp, Receive, Scope, Send

from .. import access
from ..config import hosts_team, personal_allowed
from ..services.pairing import CONTACT, DESKTOP
from .context import AppContext, get_ctx

OPEN_PATHS = {
    "/health",
    "/docs",
    "/openapi.json",
    "/redoc",
    "/docs/oauth2-redirect",
    "/m",
    "/api/pairing/redeem",
    "/api/users/redeem",
}
DEVICE_PATHS = {"/api/audio/import", "/api/pairing/me"}
SHARE_PREFIX = "/api/share/"
# What the desktop shell asks its own backend for. It never has the API token, so these are open
# to it (`from_desktop_app`), never to the network or the tunnel. Each is read-only or concerns
# only this machine, and none carries meeting data:
# - attach: a relaunched app takes over its crashed predecessor's backend (api/app_watch.py);
# - /api/system (GET only: the path has no other method): whether to install the GPU extra
#   (`setup_complete`, `gpu_support`; src-tauri lib.rs `gpu_install_wanted`);
# - /api/system/main: the main computer this one uses (services/main_computer.py), set when the
#   shell's remote access connects and cleared when it disconnects. The route itself also refuses
#   anything not from the desktop app, whatever token it carries.
LOCAL_PATHS = {"/api/system/attach", "/api/system", "/api/system/main"}
TUNNEL_CLIENTS = {"127.0.0.2"}
LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "[::1]"}
PROXY_HEADERS = ("forwarded", "x-forwarded-for", "x-forwarded-host", "x-real-ip")

# How a request was let in (`scope["state"]["channel"]`):
DESKTOP_WINDOW = (
    "desktop"  # tokenless, from the desktop app on 127.0.0.1 (LOCAL_PATHS, `_desktop_owner`)
)
API_TOKEN = "api_token"  # the configured API token
DEVICE = "device"  # a DESKTOP device token: a paired laptop or sync peer
USER = "user"  # a users.json token (state.user_id)
CONTACT_CHANNEL = "contact"  # another person's computer (state.device_id)
PHONE = "phone"  # a paired phone, DEVICE_PATHS only
LOCAL = "local"  # no sign-in needed at all: a plain main computer's own window
CHANNELS = (DESKTOP_WINDOW, API_TOKEN, DEVICE, USER, CONTACT_CHANNEL, PHONE, LOCAL)
# The owner of a computer that hosts a team (require_owner_channel); USER only with more checks.
OWNER_CHANNELS = {DESKTOP_WINDOW, API_TOKEN, DEVICE, LOCAL}


def is_open(path: str) -> bool:
    """Paths anyone may load: the list above, and everything outside the API and WebSockets
    (/ws, /ws/notes/...), which is the web app's own files (api/web.py; the code is not a
    secret, the data is)."""
    return path in OPEN_PATHS or not (
        path.startswith("/api/") or path == "/ws" or path.startswith("/ws/")
    )


class TokenAuthMiddleware:
    def __init__(self, app: ASGIApp, ctx: AppContext):
        self.app = app
        self.ctx = ctx

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            return await self._serve(scope, receive, send)
        # The MCP server says who it is (mcp_server.py): its requests are held to the owner's
        # view of a computer with a team owner. A privacy default for an assistant that reads
        # for the owner, not a security boundary: any local process may leave the header out.
        # Whoever else signs in keeps their own scope (`only_for`).
        from ..services import host_scope

        headers = dict(scope.get("headers", []))
        client = headers.get(host_scope.CLIENT_HEADER.encode(), b"").decode().strip().lower()
        view = host_scope.view(self.ctx) if client == host_scope.CLIENT_MCP else None
        with access.limited_to(view, only_for=host_scope.owner(self.ctx)):
            return await self._serve(scope, receive, send)

    async def _serve(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] not in ("http", "websocket"):
            return await self.app(scope, receive, send)
        token = self.ctx.settings.api_token
        path = scope.get("path", "")
        tunnel = (scope.get("client") or ("", 0))[0] in TUNNEL_CLIENTS
        required = bool(token) or self.ctx.settings.team_mode or tunnel
        headers = {k.decode().lower(): v.decode() for k, v in scope.get("headers", [])}
        state = scope.setdefault("state", {})
        server = scope.get("server") or ("", 0)
        state["on_team_port"] = server[1] == self.ctx.settings.team_port
        state["loopback_main"] = self._from_desktop_app(scope, headers)
        if not required:
            state["channel"] = LOCAL
        if not required or is_open(path) or scope.get("method") == "OPTIONS":
            return await self.app(scope, receive, send)

        presented = ""
        if path in LOCAL_PATHS and state["loopback_main"]:
            state["channel"] = DESKTOP_WINDOW
            return await self.app(scope, receive, send)
        auth = headers.get("authorization", "")
        if auth.lower().startswith("bearer "):
            presented = auth[7:].strip()
        if not presented:
            qs = parse_qs(scope.get("query_string", b"").decode())
            presented = (qs.get("token") or [""])[0]

        if token and hmac.compare_digest(presented.encode(), token.encode()):
            state["channel"] = API_TOKEN
            return await self.app(scope, receive, send)
        user = self.ctx.users.verify(presented)
        if user is not None:
            state["user_id"] = user.id
            state["channel"] = USER
            reset = access.current.set(access.Principal(user.id, user.name, user.role))
            try:
                return await self.app(scope, receive, send)
            finally:
                access.current.reset(reset)
        device = self.ctx.pairing.verify(presented)
        if device is not None and device.kind == CONTACT:
            principal = self._contact_principal(scope, device, path)
            if principal is not None:
                state["device_id"] = device.id
                state["channel"] = CONTACT_CHANNEL
                reset = access.current.set(principal)
                try:
                    return await self.app(scope, receive, send)
                finally:
                    access.current.reset(reset)
        elif device is not None and (device.kind == DESKTOP or path in DEVICE_PATHS):
            state["device_id"] = device.id
            state["channel"] = DEVICE if device.kind == DESKTOP else PHONE
            return await self.app(scope, receive, send)
        owner = self._desktop_owner(scope, presented, headers)
        if owner is not None:
            state["channel"] = DESKTOP_WINDOW
            reset = access.current.set(owner)
            try:
                return await self.app(scope, receive, send)
            finally:
                access.current.reset(reset)

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

    def _contact_principal(self, scope: Scope, device, path: str) -> access.Principal | None:
        """Who a CONTACT token's request runs as, or None to refuse. Resolved by the device's
        person, not its endpoint: the token is shared by all of that person's machines."""
        if (
            scope["type"] != "http"
            or not path.startswith(SHARE_PREFIX)
            or not personal_allowed(self.ctx.settings)
            or not device.person
        ):
            return None
        row = self.ctx.repo.contact(device.person)
        if row is None or row.get("removed_at"):
            return None
        from ..services import contacts

        return contacts.principal_of(row)

    def _desktop_owner(
        self, scope: Scope, presented: str, headers: dict[str, str]
    ) -> access.Principal | None:
        """A team shared from this desktop (services/team_host.py): the desktop app's own
        requests, on 127.0.0.1 and not on the network listener's port, run as its owner.
        Not when a proxy on this machine passed them on (`tailscale serve`, Caddy): those come
        from 127.0.0.1 too, but name another host or say whom they forward."""
        st = self.ctx.settings
        if presented or not (st.team_mode and st.team_owner_id):
            return None
        if not self._from_desktop_app(scope, headers):
            return None
        user = self.ctx.users.get(st.team_owner_id)
        if user is None or user.disabled:
            return None
        return access.Principal(user.id, user.name, user.role)

    def _from_desktop_app(self, scope: Scope, headers: dict[str, str]) -> bool:
        return from_desktop_app(self.ctx, scope, headers)


def from_desktop_app(ctx: AppContext, scope: Scope, headers: dict[str, str]) -> bool:
    """On 127.0.0.1, not on the network listener's port, not through the link sidecar's tunnel
    and not passed on by a proxy."""
    server, client = scope.get("server") or ("", 0), scope.get("client") or ("", 0)
    if client[0] in TUNNEL_CLIENTS:
        return False
    if server[1] == ctx.settings.team_port or client[0] not in ("127.0.0.1", "::1"):
        return False
    host = headers.get("host", "")
    host = host[: host.find("]") + 1] if host.startswith("[") else host.rsplit(":", 1)[0]
    return host in LOOPBACK_HOSTS and not any(h in headers for h in PROXY_HEADERS)


def request_from_desktop_app(ctx: AppContext, request) -> bool:
    """`from_desktop_app` for a route (a Starlette Request), whatever token it carried."""
    headers = {k.decode().lower(): v.decode() for k, v in request.scope.get("headers", [])}
    return from_desktop_app(ctx, request.scope, headers)


OWNER_ONLY = "Only the owner of this computer can do this"


def is_owner_channel(ctx: AppContext, state) -> bool:
    """Whether a request came in the way the owner of this computer does. On a computer that
    hosts a team (config.hosts_team), only: the desktop window, the API token, a DESKTOP device
    (a paired laptop or sync peer: only the owner can pair one), and the owner's own user token
    from this machine's main port. Members, reviewers, contacts, phones, and the owner's user
    token on the team listener or through a proxy are not. Anywhere else every caller that
    reached the route is the owner already (a plain main computer), or the existing refusals
    stand (a dedicated team server): True. `state` is `request.state` or the scope's state."""
    if not hosts_team(ctx.settings):
        return True
    get = state.get if isinstance(state, dict) else lambda k, d=None: getattr(state, k, d)
    channel = get("channel")
    if channel in OWNER_CHANNELS:
        return True
    return (
        channel == USER
        and get("user_id") == ctx.settings.team_owner_id
        and bool(get("loopback_main"))
        and not get("on_team_port")
    )


def require_owner_channel(request: Request) -> None:
    """FastAPI dependency for what only the owner of a computer that hosts a team may do (403
    otherwise); it changes nothing on any other computer. Like `access.require_admin`, a router
    or route lists it: `dependencies=[Depends(require_owner_channel)]`."""
    if not is_owner_channel(get_ctx(request), request.state):
        raise HTTPException(status_code=403, detail=OWNER_ONLY)


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
