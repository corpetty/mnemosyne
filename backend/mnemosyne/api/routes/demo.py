"""Demo-only routes for the browser tests: seed or remove the shared-with-me fixture (Ada
Lovelace and a meeting she shared, services/share_demo.py), make the live copilot "run" on a
meeting without audio, and set up a team host and a team member without the link sidecar
(`/team-host`, `/join-team`). 404 unless demo mode is on, 409 on a team server."""

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from ...config import personal_allowed
from ...demo import enabled
from ...models.session import CopilotItem, CopilotNotes, Session, TranscriptSegment
from ..context import AppContext, get_ctx

router = APIRouter(prefix="/api/demo", tags=["demo"])


class DemoShare(BaseModel):
    session_id: str
    person: str


def _check(ctx: AppContext) -> None:
    if not enabled():
        raise HTTPException(status_code=404, detail="Not found")
    if not personal_allowed(ctx.settings):
        raise HTTPException(status_code=409, detail="Not available on a team server")


@router.post("/share")
async def seed_share(ctx: AppContext = Depends(get_ctx)) -> DemoShare:
    from ...services import share_demo

    _check(ctx)
    share_demo.seed_demo(ctx)
    return DemoShare(session_id=share_demo.MEETING_ID, person=share_demo.ada_person())


@router.delete("/share", status_code=204)
async def remove_share(ctx: AppContext = Depends(get_ctx)) -> None:
    from ...services import share_demo

    _check(ctx)
    await share_demo.remove_demo(ctx)


# The canned notes of the demo copilot: one of each kind, so the meeting's notes get four lines
# (tests-e2e/summary-in-notes.spec.ts depends on these exact strings).
DEMO_COPILOT = {
    "summary": ["The team went over the Waku migration"],
    "decisions": ["Hold the beta until the docs are ready"],
    "action_items": [{"text": "Send the beta invites", "owner": "Mira"}],
    "open_questions": ["Who tests the Android build?"],
}


class DemoCopilot(BaseModel):
    added: int  # lines the copilot put into the meeting's notes this time


@router.post("/copilot/{session_id}", include_in_schema=False)
async def run_copilot(session_id: str, ctx: AppContext = Depends(get_ctx)) -> DemoCopilot:
    """As if the live copilot had just run on the meeting: the canned notes go through
    `copilot_notes.add_new` and the runner's store step (services/copilot.py). What was added
    before is remembered (`CopilotNotes.added`), so a second call adds nothing."""
    from ...services.copilot_notes import add_new

    _check(ctx)
    session = ctx.sessions.get_session(session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="Session not found")
    previous = ctx.copilot_notes.get(session_id) or session.copilot_notes
    fresh = CopilotNotes(
        session_id=session_id,
        summary=list(DEMO_COPILOT["summary"]),
        decisions=list(DEMO_COPILOT["decisions"]),
        action_items=[CopilotItem(**a) for a in DEMO_COPILOT["action_items"]],
        open_questions=list(DEMO_COPILOT["open_questions"]),
    )
    if previous is not None:
        fresh.added = list(previous.added)
        fresh.lines = previous.lines
    written = await add_new(ctx, session_id, fresh)
    ctx.copilot_notes[session_id] = fresh
    ctx.repo.update_fields(session_id, copilot_notes=fresh)
    ctx.bus.publish(
        {
            "type": "copilot_notes",
            "session_id": session_id,
            "notes": fresh.model_dump(mode="json"),
        }
    )
    return DemoCopilot(added=len(written))


# ---- teams without iroh (tests-e2e/team-members.spec.ts) -----------------------------------------
# No link sidecar in the browser tests, and nothing leaves this machine. A HOST demo backend hosts a
# team from here (the owner, one member, their meetings) and opens its tunnel listener
# (services/tunnel_listener.py): that port is what the sidecar's `T` streams are handed to, so a
# request to it is a TUNNELLED one, the only way a MEMBER token is accepted (api/auth.py
# `_member_principal`). A MEMBER demo backend is pointed straight at that port instead of at a dial
# port of its sidecar (`TeamMember.listen`, which tests/test_team_member.py sets the same way).

DEMO_OWNER = "Corey"
DEMO_MEMBER = "Sam"
DEMO_MEMBER_ENDPOINT = "a1" * 32  # an iroh endpoint id the host never dials


class DemoTeamHostRequest(BaseModel):
    host_name: str = "Desk"  # what the team is called: "Team on <host_name>" (the member's card)
    member: str = DEMO_MEMBER


class DemoTeamHost(BaseModel):
    host_name: str
    url: str  # "http://127.0.0.1:<tunnel port>": hand it to the member's `join-team`
    token: str  # the member app's MEMBER token
    user: dict  # {id, name, role} of the member
    owner_id: str
    owner_meeting: str  # the owner's personal meeting: the member must never see it
    member_meeting: str  # the member's meeting in the team library


def _meeting(ctx: AppContext, session_id: str, name: str, owner_id: str, line: str) -> str:
    """The meeting with this id, made once (a second `team-host` finds it)."""
    if ctx.repo.get(session_id) is None:
        transcript = [TranscriptSegment(text=line, speaker="Speaker 1", start=0.0, end=4.0)]
        ctx.repo.save(Session(id=session_id, name=name, owner_id=owner_id, transcript=transcript))
    return session_id


@router.post("/team-host", include_in_schema=False)
async def team_host(body: DemoTeamHostRequest, ctx: AppContext = Depends(get_ctx)) -> DemoTeamHost:
    """Host a team as of now: `team_mode` on with the owner, one member with a MEMBER device, a
    meeting each, the tunnel listener up. Safe to call again: the member's device is replaced (a
    new token) and nothing is made twice. Set `api_token` first: a team host asks for one."""
    _check(ctx)
    if not ctx.settings.api_token:
        raise HTTPException(status_code=409, detail="Set api_token first: a team host asks for one")
    users = {u.name: u for u in ctx.users.list()}
    owner = users.get(DEMO_OWNER) or ctx.users.add(DEMO_OWNER, "", "admin")
    member = users.get(body.member) or ctx.users.add(body.member, "", "member")
    ctx.settings.team_mode = True
    ctx.settings.team_owner_id = owner.id
    ctx.follow_hosting()
    _, token = ctx.pairing.add_member(f"{member.name}'s app", DEMO_MEMBER_ENDPOINT, member.id)
    try:
        port = await ctx.tunnel.ensure(ctx.app)
    except RuntimeError as e:
        raise HTTPException(status_code=409, detail=str(e)) from e
    return DemoTeamHost(
        host_name=body.host_name,
        url=f"http://127.0.0.1:{port}",
        token=token,
        user={"id": member.id, "name": member.name, "role": member.role},
        owner_id=owner.id,
        owner_meeting=_meeting(
            ctx, "demo-owner-private", "Private planning", owner.id, "Only the owner reads this."
        ),
        member_meeting=_meeting(
            ctx, "demo-team-standup", "Team standup", member.id, "The team standup is at ten."
        ),
    )


@router.delete("/team-host", status_code=204, include_in_schema=False)
async def stop_team_host(ctx: AppContext = Depends(get_ctx)) -> None:
    """Back to a plain computer: the members' devices are revoked, hosting and the listener off
    (meetings and users stay: a second `team-host` finds them)."""
    _check(ctx)
    for u in ctx.users.list():
        ctx.pairing.revoke_members(u.id)
    ctx.settings.team_mode = False
    ctx.settings.team_owner_id = ""
    ctx.follow_hosting()
    await ctx.tunnel.stop()


class DemoJoinTeam(BaseModel):
    url: str  # the host's tunnel listener, from `team-host`
    token: str
    name: str = "Desk"  # the host's name
    user: dict  # {id, name, role} on the host


@router.post("/join-team", include_in_schema=False)
async def join_team(body: DemoJoinTeam, ctx: AppContext = Depends(get_ctx)) -> dict:
    """As if the app invite had been redeemed: the membership is written (services/team_member.py
    `TeamStore`) and pointed straight at `url`, with no sidecar to pair or dial through."""
    from datetime import UTC, datetime

    from ...services.team_member import Membership

    _check(ctx)
    tm = ctx.team_member
    if tm is None:
        raise HTTPException(status_code=409, detail="No team membership on this computer")
    tm.store.clear()
    reason = tm.refusal()
    if reason:
        raise HTTPException(status_code=409, detail=reason)
    listen = body.url.removeprefix("http://").rstrip("/")
    tm.store.save(
        Membership(
            host_name=body.name[:80] or "Team",
            host_ticket="demo",
            host_endpoint=DEMO_MEMBER_ENDPOINT,
            token=body.token,
            user={k: str(body.user.get(k) or "") for k in ("id", "name", "role")},
            joined_at=datetime.now(UTC).isoformat(),
        )
    )
    tm.listen = listen
    tm._probe = None
    return {"joined": True, "listen": listen}
