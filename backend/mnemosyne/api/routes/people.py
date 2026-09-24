"""People across meetings."""

from fastapi import APIRouter, Depends, HTTPException

from ...services.people import PersonDetail, PersonSummary, list_people, person_detail
from ..context import AppContext, get_ctx

router = APIRouter(prefix="/api", tags=["people"])


def _generic(ctx: AppContext) -> tuple[str, ...]:
    return (ctx.settings.local_speaker_name, ctx.settings.remote_speaker_name)


@router.get("/people", response_model=list[PersonSummary])
async def get_people(ctx: AppContext = Depends(get_ctx)):
    """Everyone seen as a named speaker, an invitee, an action item owner or a saved voice."""
    return list_people(ctx.repo, _generic(ctx))


@router.get("/people/{name}", response_model=PersonDetail)
async def get_person(name: str, ctx: AppContext = Depends(get_ctx)):
    detail = person_detail(ctx.repo, name, _generic(ctx))
    if detail is None:
        raise HTTPException(status_code=404, detail="Nobody by that name")
    return detail
