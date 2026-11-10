"""This computer in a team another computer hosts (services/team_member.py; docs/plans/
2026-11-10-team-members-in-the-app.md 1.2): join, leave, the work mode, and the proxy the
window uses for the team's library.

The window shows the team's library by pointing its one connection at
`http://127.0.0.1:8008/api/joined` with THIS computer's token; everything under it that is the
host's API (`api/...`, `ws`, `ws/...`, `health`) is forwarded to the host through the sidecar's
dial port with the member token instead. The window never holds the member token, and the host
never sees this computer's token or cookies (R1 in the plan): `authorization`, `cookie`, `host`,
proxy headers and the `token` query parameter are dropped on the way. Bodies stream both ways
(no buffering: records exports, audio with ranges, long Ask answers); WebSockets are bridged
frame by frame with their close codes.

Only for this computer: never through the link sidecar's tunnel or the team listener, whatever
token comes with it (another machine must not drive this one's team membership or use its
member token), and on a computer that hosts a team only for its owner.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from typing import Literal
from urllib.parse import parse_qsl, quote, urlencode

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request, WebSocket
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from starlette.background import BackgroundTask

from ...models.base import ApiModel
from ...services.team_member import TeamMember, TeamMemberError
from ..auth import is_owner_channel, is_tunnelled, require_owner_channel
from ..context import AppContext, get_ctx, get_ws_ctx
from .satellite import _this_computer

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/joined", tags=["team membership"])
# On every HTTP route (a WebSocket checks the same itself: these take a Request).
HERE = [Depends(require_owner_channel), Depends(_this_computer)]

# Request headers never passed to the host: this computer's credentials, hop-by-hop headers,
# and what a proxy in front of this computer would have added.
DROP_REQUEST = {
    "host",
    "authorization",
    "cookie",
    "connection",
    "keep-alive",
    "proxy-authorization",
    "proxy-connection",
    "te",
    "trailer",
    "transfer-encoding",
    "upgrade",
    "forwarded",
    "x-real-ip",
}
# Response headers never passed back: hop-by-hop ones, cookies, and CORS (this backend's own
# middleware answers for its own origin).
DROP_RESPONSE = {
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-connection",
    "te",
    "trailer",
    "transfer-encoding",
    "upgrade",
    "set-cookie",
    "www-authenticate",
}
# What stays unencoded in a forwarded path (RFC 3986 pchar and "/"); "?" and "#" never do.
PATH_SAFE = "/:@!$&'()*+,;=-._~"
SIGNED_OUT_CLOSE = 4409  # a bridged socket closed because the host signed this member out
UNREACHABLE_CLOSE = 1011


class JoinedUser(ApiModel):
    id: str
    name: str
    role: str


class JoinedStatus(ApiModel):
    joined: bool
    host_name: str | None  # the team's name (its hosting computer's)
    user: JoinedUser | None  # who this person is on the host
    reachable: bool
    work: Literal["here", "host"]  # where a team recording's work runs by default
    consent_required: bool  # the host's rule for recording (its /health, else as joined)
    signed_out: bool  # the host refused the token: leave and join with a new invite


class JoinResponse(JoinedStatus):
    # The API token made for this computer by joining (remote access needs one), once; None
    # when it had one. The window carries it, or its next request is refused.
    new_api_token: str | None


class JoinRequest(BaseModel):
    invite: str  # the app invite, <host ticket>#<code>
    name: str = ""  # this computer's name on the host (default: its sync name)


class WorkRequest(BaseModel):
    work: Literal["here", "host"]


def _service(ctx: AppContext) -> TeamMember:
    return ctx.team_member


async def _status(tm: TeamMember) -> dict:
    m = tm.membership
    if m is None:
        return {
            "joined": False,
            "host_name": None,
            "user": None,
            "reachable": False,
            "work": "here",
            "consent_required": False,
            "signed_out": False,
        }
    reachable = False if m.signed_out_at else await tm.reachable()
    m = tm.membership or m  # the probe may have refreshed the consent rule
    return {
        "joined": True,
        "host_name": m.host_name,
        "user": JoinedUser(**m.user),
        "reachable": reachable,
        "work": m.work,
        "consent_required": m.consent_required,
        "signed_out": bool(m.signed_out_at),
    }


def _raise(e: TeamMemberError) -> HTTPException:
    return HTTPException(status_code=e.status, detail=e.detail)


@router.get("", response_model=JoinedStatus, dependencies=HERE)
async def status(ctx: AppContext = Depends(get_ctx)):
    return JoinedStatus(**await _status(_service(ctx)))


@router.post("/join", response_model=JoinResponse, dependencies=HERE)
async def join(body: JoinRequest, ctx: AppContext = Depends(get_ctx)):
    """Join the team whose app invite this is (Settings on the hosting computer, People and
    access). Turns on this computer's API token and remote access when they are off."""
    tm = _service(ctx)
    try:
        _, new_token = await tm.join(body.invite, body.name)
    except TeamMemberError as e:
        raise _raise(e) from e
    return JoinResponse(**await _status(tm), new_api_token=new_token)


@router.delete("", response_model=JoinedStatus, dependencies=HERE)
async def leave(ctx: AppContext = Depends(get_ctx)):
    """Leave the team: the host is told when it can be reached (this computer's device there
    is removed), and this computer forgets the membership either way."""
    tm = _service(ctx)
    if not await tm.leave():
        raise HTTPException(status_code=404, detail="This computer is not in a team")
    return JoinedStatus(**await _status(tm))


@router.put("/work", response_model=JoinedStatus, dependencies=HERE)
async def set_work(body: WorkRequest, ctx: AppContext = Depends(get_ctx)):
    tm = _service(ctx)
    try:
        tm.set_work(body.work)
    except TeamMemberError as e:
        raise _raise(e) from e
    return JoinedStatus(**await _status(tm))


# ---- the proxy -------------------------------------------------------------------------------


def _forwardable(path: str) -> bool:
    """Only the host's API, its sockets and /health. Never this proxy again, never the
    satellite's recording stream (satellite.py opens that itself), never a dot segment."""
    if any(seg in (".", "..") for seg in path.split("/")) or "\\" in path:
        return False
    if path in ("ws", "health") or path.startswith("ws/"):
        return True
    if not path.startswith("api/"):
        return False
    return not any(path == p or path.startswith(p + "/") for p in ("api/joined", "api/record"))


def _target(base: str, path: str, query_string: bytes) -> str:
    """The host URL: the validated path re-encoded (so nothing in it can become a query or a
    fragment) and the query without `token` (this computer's, for <audio> and sockets)."""
    query = [
        (k, v)
        for k, v in parse_qsl(query_string.decode("latin-1"), keep_blank_values=True)
        if k != "token"
    ]
    url = f"{base}/{quote(path, safe=PATH_SAFE)}"
    return f"{url}?{urlencode(query)}" if query else url


def _signed_out_detail(name: str) -> str:
    return f"You were signed out of {name}; ask for a new invite"


async def _destination(tm: TeamMember):
    """The host and the member token, or the refusal: 404 not in a team, 409 signed out, 502
    not reachable."""
    m = tm.membership
    if m is None:
        raise HTTPException(status_code=404, detail="This computer is not in a team")
    if m.signed_out_at:
        raise HTTPException(status_code=409, detail=_signed_out_detail(m.host_name))
    dest = await tm.ensure_destination()
    if dest is None:
        raise HTTPException(status_code=502, detail=f"{m.host_name} is not reachable")
    return dest


@router.api_route(
    "/{path:path}",
    methods=["GET", "HEAD", "POST", "PUT", "PATCH", "DELETE"],
    dependencies=HERE,
    include_in_schema=False,
)
async def forward(path: str, request: Request, ctx: AppContext = Depends(get_ctx)):
    if not _forwardable(path):
        raise HTTPException(status_code=404, detail="Not found")
    tm = _service(ctx)
    dest = await _destination(tm)
    headers = [
        (k, v)
        for k, v in request.headers.items()
        if k not in DROP_REQUEST and not k.startswith("x-forwarded-")
    ]
    headers.append(("authorization", f"Bearer {dest.token}"))
    has_body = "content-length" in request.headers or "transfer-encoding" in request.headers
    client = tm.client()
    upstream = client.build_request(
        request.method,
        _target(dest.url, path, request.scope.get("query_string", b"")),
        headers=headers,
        content=request.stream() if has_body else None,
    )
    try:
        resp = await client.send(upstream, stream=True)
    except httpx.HTTPError as e:
        tm.seen(False)
        logger.info("Team proxy: %s %s failed: %s", request.method, path, type(e).__name__)
        raise HTTPException(status_code=502, detail=f"{dest.name} is not reachable") from e
    tm.seen(True)
    if resp.status_code == 401:
        await resp.aclose()
        # Never a 401 to the window: it would take it for its own token being refused.
        if await tm.check_signed_out():
            raise HTTPException(status_code=409, detail=_signed_out_detail(dest.name))
        raise HTTPException(status_code=502, detail=f"{dest.name} refused the request")

    async def body():
        try:
            async for chunk in resp.aiter_raw():
                yield chunk
        except httpx.HTTPError:
            return  # the host went away mid-stream: the caller sees a short body
        finally:
            await resp.aclose()

    out = StreamingResponse(
        body(), status_code=resp.status_code, background=BackgroundTask(resp.aclose)
    )
    out.raw_headers = [
        (k, v)
        for k, v in resp.headers.raw
        if k.lower().decode("latin-1") not in DROP_RESPONSE
        and not k.lower().startswith(b"access-control-")
    ]
    return out


# ---- WebSockets ------------------------------------------------------------------------------


def _ws_refusal(ws: WebSocket, ctx: AppContext) -> bool:
    """The HTTP routes' guards for a socket: through the tunnel, on the team listener or (on a
    computer that hosts a team) not the owner: refused."""
    server = ws.scope.get("server") or ("", 0)
    if is_tunnelled(ws.scope) or server[1] == ctx.settings.team_port:
        return True
    return not is_owner_channel(ctx, ws.scope.get("state") or {})


def _close_code(code: int | None) -> int:
    """A code that may be sent in a close frame (1005/1006/1015 only describe one)."""
    if code is None or code in (1005, 1006, 1015) or not (1000 <= code < 5000):
        return UNREACHABLE_CLOSE
    return code


async def _bridge(ws: WebSocket, path: str) -> None:
    from websockets.asyncio.client import connect
    from websockets.exceptions import ConnectionClosed, InvalidHandshake, InvalidStatus

    ctx = get_ws_ctx(ws)
    if _ws_refusal(ws, ctx):
        await ws.close(code=4403)
        return
    if not _forwardable(path):
        await ws.close(code=4404)
        return
    await ws.accept()
    tm = _service(ctx)
    m = tm.membership
    if m is None:
        await ws.close(code=4404)
        return
    if m.signed_out_at:
        await ws.close(code=SIGNED_OUT_CLOSE, reason="signed out")
        return
    dest = await tm.ensure_destination()
    if dest is None:
        await ws.close(code=UNREACHABLE_CLOSE)
        return
    url = "ws" + _target(dest.url, path, ws.scope.get("query_string", b""))[4:]
    try:
        upstream = await connect(
            url,
            additional_headers={"Authorization": f"Bearer {dest.token}"},
            open_timeout=10,
            ping_interval=20,
            ping_timeout=30,
            close_timeout=5,
            max_size=2**25,
            proxy=None,
        )
    except InvalidStatus as e:
        # The host refuses a socket before accepting it (403 for a refused token too).
        status = e.response.status_code
        if await tm.check_signed_out():
            await ws.close(code=SIGNED_OUT_CLOSE, reason="signed out")
        else:
            await ws.close(code={403: 4403, 404: 4404}.get(status, UNREACHABLE_CLOSE))
        return
    except (OSError, TimeoutError, InvalidHandshake) as e:
        tm.seen(False)
        logger.info("Team proxy: socket %s failed: %s", path, type(e).__name__)
        await ws.close(code=UNREACHABLE_CLOSE)
        return

    async def to_host() -> None:
        """Browser -> host, until the browser goes."""
        while True:
            msg = await ws.receive()
            if msg["type"] == "websocket.disconnect":
                code = _close_code(msg.get("code"))
                await upstream.close(code=1000 if code == UNREACHABLE_CLOSE else code)
                return
            if msg.get("bytes") is not None:
                await upstream.send(msg["bytes"])
            elif msg.get("text") is not None:
                await upstream.send(msg["text"])

    async def to_browser() -> None:
        """Host -> browser, then the host's close code."""
        try:
            async for data in upstream:
                if isinstance(data, bytes):
                    await ws.send_bytes(data)
                else:
                    await ws.send_text(data)
        except ConnectionClosed:
            pass
        code = upstream.close_code
        if code == 4401 and await tm.check_signed_out():
            code = SIGNED_OUT_CLOSE
        with contextlib.suppress(Exception):  # the browser may be gone already
            await ws.close(code=_close_code(code), reason=upstream.close_reason or "")

    tasks = [asyncio.create_task(to_host()), asyncio.create_task(to_browser())]
    try:
        done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        for task in pending:
            task.cancel()
        for task in tasks:
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
    finally:
        for task in tasks:
            task.cancel()
        with contextlib.suppress(Exception):
            await upstream.close()


@router.websocket("/ws")
async def bridge_events(ws: WebSocket):
    await _bridge(ws, "ws")


@router.websocket("/ws/{rest:path}")
async def bridge_socket(ws: WebSocket, rest: str):
    await _bridge(ws, f"ws/{rest}")
