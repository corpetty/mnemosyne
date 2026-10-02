"""Share this computer with a team (services/team_host.py): its status, and turning it on and off.
Changing it is for the desktop app itself (an admin on 127.0.0.1, not over the network)."""

import socket

from fastapi import APIRouter, Depends, HTTPException, Request

from ... import access
from ...models.base import ApiModel
from ...services import team_host
from ..context import AppContext, get_ctx

router = APIRouter(prefix="/api/team", tags=["team"])


class TeamStatus(ApiModel):
    enabled: bool  # share_on_network
    running: bool  # the network listener is up
    port: int
    addresses: list[str]  # what to open from another computer, the likeliest first
    owner: str  # the person at this computer, the first admin
    firewall_hint: str  # a command that lets others reach the port, "" when no firewall tool
    error: str
    can_change: bool  # this request comes from the desktop app


class TeamChange(ApiModel):
    enabled: bool
    name: str = ""  # the owner's name the first time (default: the system account's)


def _local(request: Request, ctx: AppContext) -> bool:
    server = request.scope.get("server") or ("", 0)
    client = request.scope.get("client") or ("", 0)
    return server[1] != ctx.settings.team_port and client[0] in ("127.0.0.1", "::1")


def _status(request: Request, ctx: AppContext) -> TeamStatus:
    st = ctx.settings
    port = st.team_port
    hosts = [*team_host.lan_addresses(), f"{socket.gethostname()}.local"]
    owner = ctx.users.get(st.team_owner_id) if st.team_owner_id else None
    return TeamStatus(
        enabled=st.share_on_network,
        running=ctx.team_host.running,
        port=port,
        addresses=[f"https://{h}:{port}" for h in hosts],
        owner=owner.name if owner else team_host.owner_name(),
        firewall_hint=team_host.firewall_hint(port),
        error=ctx.team_host.error,
        can_change=_local(request, ctx) and access.is_admin(),
    )


@router.get("", response_model=TeamStatus)
async def status(request: Request, ctx: AppContext = Depends(get_ctx)):
    return _status(request, ctx)


@router.put("", response_model=TeamStatus)
async def change(body: TeamChange, request: Request, ctx: AppContext = Depends(get_ctx)):
    if not (_local(request, ctx) and access.is_admin()):
        raise access.Forbidden("Sharing is turned on and off in the desktop app on this computer")
    try:
        if body.enabled:
            await team_host.turn_on(ctx, request.app, body.name)
        else:
            await team_host.turn_off(ctx)
    except RuntimeError as e:
        raise HTTPException(status_code=409, detail=str(e)) from e
    return _status(request, ctx)
