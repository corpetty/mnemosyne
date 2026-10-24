"""Demo-only routes for the browser tests: seed or remove the shared-with-me fixture (Ada
Lovelace and a meeting she shared, services/share_demo.py). 404 unless demo mode is on, 409 on a
team server."""

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from ...demo import enabled
from ..context import AppContext, get_ctx

router = APIRouter(prefix="/api/demo", tags=["demo"])


class DemoShare(BaseModel):
    session_id: str
    person: str


def _check(ctx: AppContext) -> None:
    if not enabled():
        raise HTTPException(status_code=404, detail="Not found")
    if ctx.settings.team_mode:
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
