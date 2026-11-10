"""Recording on a computer that uses a main computer (services/satellite.py; docs/plans/
2026-11-09-connections-design.md, task 7).

The window on such a computer shows the main computer's library, but it records through THIS
computer's backend (http://127.0.0.1:8008, with this computer's own token like any /api route):
capture is here, the meeting is the main computer's. Nothing here ever enters this computer's
own library. Only for this computer: never through the link sidecar's tunnel or the team
listener, whatever token comes with it (another computer must not open this one's microphone).
"""

from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Request

from ...models.base import ApiModel
from ...services import satellite
from ..auth import require_owner_channel
from ..context import AppContext, get_ctx
from .audio import Consent

router = APIRouter(
    prefix="/api/satellite", tags=["satellite"], dependencies=[Depends(require_owner_channel)]
)


def _this_computer(request: Request, ctx: AppContext = Depends(get_ctx)) -> None:
    from ..auth import is_tunnelled

    server = request.scope.get("server") or ("", 0)
    if is_tunnelled(request.scope) or server[1] == ctx.settings.team_port:
        raise HTTPException(status_code=403, detail="Only on this computer")


HERE = [Depends(_this_computer)]


class SatelliteMain(ApiModel):
    name: str


Destination = Literal["main", "team"]


class SatelliteSource(ApiModel):
    source: Literal["mic", "system"]
    label: str
    captured: int  # PCM bytes recorded here
    sent: int  # PCM bytes sent to the main computer
    connected: bool  # its stream to the main computer is open
    gap: bool  # the stream dropped (the complete audio is handed over at stop)


class SatelliteRecording(ApiModel):
    local_id: str
    session_id: str | None  # the main computer's (or team's) meeting when streaming, else None
    destination: Destination  # where this recording goes
    streaming: bool  # False: recorded here only, handed over later
    title: str
    started_at: str  # UTC ISO
    sources: list[SatelliteSource]


class OutboxEntry(ApiModel):
    id: str
    title: str
    started_at: str  # UTC ISO
    parts: int
    # recording: being recorded; waiting: for the main computer; sending: being handed over;
    # failed: refused by the main computer (last_error); kept here either way.
    state: Literal["recording", "waiting", "sending", "failed"]
    last_error: str
    replaces: str | None  # the main computer's meeting whose streamed audio it completes
    destination: Destination  # the main computer or the team it is handed to


class SatelliteStatus(ApiModel):
    main: SatelliteMain | None  # the main computer this one uses (None: it is one)
    reachable: bool  # the main computer answers
    team: SatelliteMain | None  # the team this computer is in (None: not in one, or signed out)
    team_reachable: bool  # the team's host answers
    recording: SatelliteRecording | None
    outbox: list[OutboxEntry]


class SatelliteStartRequest(ApiModel):
    mic_device: int | None = None  # PipeWire device ids, as /api/devices lists them
    system_device: int | None = None
    title: str = ""
    consent: Consent | None = None
    destination: Destination = "main"  # the main computer, or the team this computer is in


class SatelliteStartResponse(ApiModel):
    session_id: str | None  # the main computer's (or team's) meeting, when streaming
    local_id: str
    streaming: bool


class SatelliteStopRequest(ApiModel):
    notes: str | None = None  # notes typed during an outbox recording (markdown)


class SatelliteStopResponse(ApiModel):
    local_id: str
    session_id: str | None
    # streamed: the main computer has all of it; handing_over: the stream had gaps, the complete
    # audio is being sent; waiting: recorded here only, it goes when the main computer is there.
    outcome: Literal["streamed", "handing_over", "waiting"]


@router.get("", response_model=SatelliteStatus, dependencies=HERE)
async def status(ctx: AppContext = Depends(get_ctx)):
    sat = satellite.service(ctx)
    await sat.ready()
    main = ctx.main_computer
    member = ctx.team_member.membership if ctx.team_member is not None else None
    team = member if member is not None and not member.signed_out_at else None
    return SatelliteStatus(
        main=SatelliteMain(name=main.name) if main else None,
        reachable=await sat.reachable(),
        team=SatelliteMain(name=team.host_name) if team else None,
        team_reachable=team is not None and await sat.reachable("team"),
        recording=sat.recording(),
        outbox=sat.items(),
    )


@router.post("/start", response_model=SatelliteStartResponse, dependencies=HERE)
async def start(body: SatelliteStartRequest, ctx: AppContext = Depends(get_ctx)):
    """Record with this computer's devices: streamed to the destination (the main computer, or
    the team's host), which records the meeting, or (not reachable) kept here until it can be
    handed over. A destination this computer does not have (no main computer, not in a team,
    signed out): 409."""
    active = await satellite.service(ctx).start(
        body.mic_device, body.system_device, body.title, body.consent, body.destination
    )
    return SatelliteStartResponse(
        session_id=active.item.session_id,
        local_id=active.item.id,
        streaming=bool(active.streams),
    )


@router.post("/stop", response_model=SatelliteStopResponse, dependencies=HERE)
async def stop(body: SatelliteStopRequest | None = None, ctx: AppContext = Depends(get_ctx)):
    return await satellite.service(ctx).stop(notes=body.notes if body else None)


@router.post("/outbox/{item_id}/new-meeting", response_model=OutboxEntry, dependencies=HERE)
async def as_new_meeting(item_id: str, ctx: AppContext = Depends(get_ctx)):
    """A waiting or refused recording goes to the main computer as a NEW meeting, not as the
    complete audio of the meeting it streamed into (refused once that one was transcribed)."""
    sat = satellite.service(ctx)
    await sat.ready()
    return sat.entry(await sat.as_new_meeting(item_id))


class OutboxDeleted(ApiModel):
    id: str


@router.delete("/outbox/{item_id}", response_model=OutboxDeleted, dependencies=HERE)
async def discard(item_id: str, ctx: AppContext = Depends(get_ctx)):
    """Delete a waiting or refused recording's audio from this computer, for good. Never while
    it is being recorded or handed over (409)."""
    sat = satellite.service(ctx)
    await sat.ready()
    sat.discard(item_id)
    return OutboxDeleted(id=item_id)
