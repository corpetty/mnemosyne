"""Supervision (services/supervision.py): the queue of meetings with flagged lines, a meeting's
flags and reviews, and marking a meeting reviewed. For reviewers and admins."""

from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException

from ... import access
from ...jobs import Job
from ...models.base import ApiModel
from ...services import history, supervision
from ..context import AppContext, get_ctx

router = APIRouter(prefix="/api", tags=["supervision"])


class Flag(ApiModel):
    idx: int  # the line in the transcript as it is now
    start: float
    speaker: str
    phrase: str
    text: str
    found_at: datetime


class Review(ApiModel):
    at: datetime
    by: str
    note: str


class MeetingSupervision(ApiModel):
    flags: list[Flag]
    reviews: list[Review]
    reviewed: bool  # a review is newer than every flag


class QueueItem(ApiModel):
    session_id: str
    name: str
    created_at: datetime
    owner: str  # the owner's name on a team server, "" otherwise
    flags: int
    phrases: list[str]
    reviewed: bool
    reviewed_at: datetime | None
    reviewed_by: str
    note: str


class ReviewRequest(ApiModel):
    note: str = ""


def _reviewer() -> None:
    p = access.principal()
    if p is not None and p.role not in (access.REVIEWER, access.ADMIN):
        raise access.Forbidden("Only a reviewer or an admin can do this")


def _reviewed(last_found: str | None, last_review: str | None) -> bool:
    return last_review is not None and (last_found is None or last_review >= last_found)


def _meeting(ctx: AppContext, session_id: str) -> MeetingSupervision:
    flags = ctx.repo.flags(session_id)
    reviews = ctx.repo.reviews(session_id)
    return MeetingSupervision(
        flags=[Flag(**f) for f in flags],
        reviews=[Review(at=r["at"], by=r["by"], note=r["note"]) for r in reviews],
        reviewed=_reviewed(
            max((f["found_at"] for f in flags), default=None),
            reviews[-1]["at"] if reviews else None,
        ),
    )


@router.get("/supervision", response_model=list[QueueItem])
async def queue(ctx: AppContext = Depends(get_ctx)):
    """Flagged meetings, the ones still to review first, newest first within each."""
    _reviewer()
    names = {u.id: u.name for u in ctx.users.list()}
    items = [
        QueueItem(
            session_id=r["id"],
            name=r["name"],
            created_at=r["created_at"],
            owner=names.get(r["owner_id"], ""),
            flags=r["flags"],
            phrases=sorted((r["phrases"] or "").split(","), key=str.casefold),
            reviewed=_reviewed(r["last_found_at"], r["reviewed_at"]),
            reviewed_at=r["reviewed_at"],
            reviewed_by=r["reviewed_by"] or "",
            note=r["note"] or "",
        )
        for r in ctx.repo.supervision_queue()
    ]
    return sorted(items, key=lambda i: i.reviewed)  # stable: keeps newest first


@router.get("/sessions/{session_id}/supervision", response_model=MeetingSupervision)
async def meeting(session_id: str, ctx: AppContext = Depends(get_ctx)):
    _reviewer()
    if not ctx.repo.exists(session_id):
        raise HTTPException(status_code=404, detail="Session not found")
    return _meeting(ctx, session_id)


@router.post("/sessions/{session_id}/supervision/review", response_model=MeetingSupervision)
async def review(session_id: str, request: ReviewRequest, ctx: AppContext = Depends(get_ctx)):
    """Mark the meeting's flags reviewed, with a note; kept in its history."""
    _reviewer()
    if not ctx.repo.exists(session_id):
        raise HTTPException(status_code=404, detail="Session not found")
    note = request.note.strip()[:2000]
    ctx.repo.add_review(session_id, note)
    who = access.principal()
    history.log(
        ctx,
        session_id,
        "supervision_reviewed",
        by=who.name if who else "",
        note=note,
        flags=len(ctx.repo.flags(session_id)),
    )
    return _meeting(ctx, session_id)


@router.post("/supervision/scan", response_model=Job)
async def scan(ctx: AppContext = Depends(get_ctx)):
    """Every meeting against the current phrases (a `supervision_scan` job). Settings changes
    start one on their own; this is for meetings from before supervision was on."""
    access.require_admin()
    if not supervision.enabled(ctx.settings):
        raise HTTPException(status_code=400, detail="Supervision is off")
    return ctx.submit_supervision_scan()
