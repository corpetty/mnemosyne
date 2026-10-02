"""Share this computer with a team (services/team_host.py): its status, and turning it on and off.
Changing it is for the desktop app itself (an admin on 127.0.0.1, not over the network)."""

import asyncio
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
    keep_sharing_after_quit: bool  # the backend keeps serving when the app quits
    keep_awake_while_sharing: bool  # a sleep inhibitor all the time it is shared
    keep_awake_available: bool  # systemd-inhibit is there (not in the Flatpak)
    tailscale_name: str  # this machine's tailnet name when it can get a certificate, else ""
    tailscale_cert: bool  # serve that name with its real certificate (team_tailscale_cert)
    tailscale_error: str  # why that certificate could not be had


class TeamChange(ApiModel):
    enabled: bool | None = None  # None: leave sharing as it is, change only the options
    name: str = ""  # the owner's name the first time (default: the system account's)
    keep_sharing_after_quit: bool | None = None
    keep_awake_while_sharing: bool | None = None
    tailscale_cert: bool | None = None


def _local(request: Request, ctx: AppContext) -> bool:
    server = request.scope.get("server") or ("", 0)
    client = request.scope.get("client") or ("", 0)
    return server[1] != ctx.settings.team_port and client[0] in ("127.0.0.1", "::1")


def _status(request: Request, ctx: AppContext, tailscale: str) -> TeamStatus:
    st = ctx.settings
    port = st.team_port
    hosts = [*team_host.lan_addresses(), f"{socket.gethostname()}.local"]
    if ctx.team_host.tailscale:  # no browser warning there: first
        hosts.insert(0, ctx.team_host.tailscale)
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
        keep_sharing_after_quit=st.keep_sharing_after_quit,
        keep_awake_while_sharing=st.keep_awake_while_sharing,
        keep_awake_available=ctx.awake.available,
        tailscale_name=tailscale,
        tailscale_cert=st.team_tailscale_cert,
        tailscale_error=ctx.team_host.tailscale_error,
    )


@router.get("", response_model=TeamStatus)
async def status(request: Request, ctx: AppContext = Depends(get_ctx)):
    return _status(request, ctx, await asyncio.to_thread(team_host.tailscale_name))


@router.put("", response_model=TeamStatus)
async def change(body: TeamChange, request: Request, ctx: AppContext = Depends(get_ctx)):
    if not (_local(request, ctx) and access.is_admin()):
        raise access.Forbidden("Sharing is turned on and off in the desktop app on this computer")
    options = {
        k: v
        for k, v in body.model_dump(
            include={"keep_sharing_after_quit", "keep_awake_while_sharing"}
        ).items()
        if v is not None
    }
    if body.tailscale_cert is not None:
        options["team_tailscale_cert"] = body.tailscale_cert
    try:
        if options:
            await team_host.set_options(ctx, **options)
        if body.enabled is True:
            await team_host.turn_on(ctx, request.app, body.name)
        elif body.enabled is False:
            await team_host.turn_off(ctx)
    except RuntimeError as e:
        raise HTTPException(status_code=409, detail=str(e)) from e
    return _status(request, ctx, await asyncio.to_thread(team_host.tailscale_name))
