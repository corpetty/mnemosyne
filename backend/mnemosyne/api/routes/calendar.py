"""Calendar status, upcoming meetings, and the meeting happening now."""

import asyncio

from fastapi import APIRouter, Depends, HTTPException

from ...models.base import ApiModel
from ...models.session import Session
from ...services.calendar_service import CalendarEvent
from ...services.desktop_calendar import DesktopCalendar, list_calendars
from ..context import AppContext, get_ctx

router = APIRouter(prefix="/api", tags=["calendar"])


class CalendarResponse(ApiModel):
    configured: bool
    error: str | None
    current: CalendarEvent | None
    upcoming: list[CalendarEvent]


@router.get("/calendar", response_model=CalendarResponse)
async def calendar(hours: float = 12, refresh: bool = False, ctx: AppContext = Depends(get_ctx)):
    cal = ctx.calendar
    if not cal.configured:
        return CalendarResponse(configured=False, error=None, current=None, upcoming=[])
    upcoming = await cal.upcoming(hours=hours) if not refresh else []
    if refresh:
        from datetime import datetime, timedelta

        now = datetime.now().astimezone()
        await cal.events(now, now + timedelta(minutes=1), force=True)
        upcoming = await cal.upcoming(hours=hours)
    return CalendarResponse(
        configured=True,
        error=cal.last_error,
        current=await cal.current(),
        upcoming=upcoming,
    )


@router.get("/calendar/desktop", response_model=list[DesktopCalendar])
async def desktop_calendars():
    """The calendars GNOME Online Accounts and evolution-data-server know about (empty when
    the desktop has none)."""
    try:
        return await asyncio.to_thread(list_calendars)
    except Exception:
        return []


class AttendeesRequest(ApiModel):
    attendees: list[str]


@router.put("/sessions/{session_id}/attendees", response_model=Session)
async def set_attendees(
    session_id: str, request: AttendeesRequest, ctx: AppContext = Depends(get_ctx)
):
    names = list(dict.fromkeys(a.strip() for a in request.attendees if a.strip()))
    session = ctx.repo.update_fields(session_id, attendees=names)
    if session is None:
        raise HTTPException(status_code=404, detail="Session not found")
    return session
