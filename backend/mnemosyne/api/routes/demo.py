"""Demo-only routes for the browser tests: seed or remove the shared-with-me fixture (Ada
Lovelace and a meeting she shared, services/share_demo.py) and make the live copilot "run" on a
meeting without audio. 404 unless demo mode is on, 409 on a team server."""

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from ...config import personal_allowed
from ...demo import enabled
from ...models.session import CopilotItem, CopilotNotes
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
