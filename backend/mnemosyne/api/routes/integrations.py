"""Integrations: issues from action items (GitHub, Linear, Jira), posting follow-ups
(Slack, Matrix) and meetings in a CRM (HubSpot)."""

from typing import Literal

from fastapi import APIRouter, Depends, HTTPException

from ...models.base import ApiModel
from ...services import hubspot
from ...services.chat_post import MatrixPoster, SlackPoster
from ...services.github_service import GitHubService, IssueResult, RepoCheck
from ...services.trackers import JiraTracker, LinearTracker, TrackerCheck
from ..context import AppContext, get_ctx

router = APIRouter(prefix="/api", tags=["integrations"])

Tracker = Literal["github", "linear", "jira"]
Destination = Literal["slack", "matrix"]
Crm = Literal["hubspot"]


def _github(ctx: AppContext) -> GitHubService:
    st = ctx.settings
    return GitHubService(st.github_repo, st.github_token, st.github_labels, ctx.http_transport)


def _tracker(ctx: AppContext, name: str):
    st = ctx.settings
    if name == "github":
        return _github(ctx)
    if name == "linear":
        return LinearTracker(st.linear_api_key, st.linear_team, ctx.http_transport)
    return JiraTracker(
        st.jira_url,
        st.jira_email,
        st.jira_api_token,
        st.jira_project,
        st.jira_issue_type,
        ctx.http_transport,
    )


def _poster(ctx: AppContext, name: str):
    st = ctx.settings
    if name == "slack":
        return SlackPoster(st.slack_webhook_url, ctx.http_transport)
    return MatrixPoster(
        st.matrix_homeserver, st.matrix_access_token, st.matrix_room_id, ctx.http_transport
    )


@router.get("/integrations/github/check", response_model=RepoCheck)
async def check_github(ctx: AppContext = Depends(get_ctx)):
    try:
        return await _github(ctx).check()
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"GitHub: {e}") from e


@router.get("/integrations/{name}/check", response_model=TrackerCheck)
async def check_integration(
    name: Literal["linear", "jira", "slack", "matrix", "hubspot"],
    ctx: AppContext = Depends(get_ctx),
):
    if name == "hubspot":
        target = hubspot.client_for(ctx)
    else:
        target = _tracker(ctx, name) if name in ("linear", "jira") else _poster(ctx, name)
    try:
        return await target.check()
    except Exception as e:
        return TrackerCheck(ok=False, message=f"{name}: {e}")


class Configured(ApiModel):
    trackers: list[Tracker]
    destinations: list[Destination]
    crm: list[Crm]


@router.get("/integrations", response_model=Configured)
async def configured(ctx: AppContext = Depends(get_ctx)):
    """Which integrations have their settings filled in (not whether they work)."""
    return Configured(
        trackers=[t for t in ("github", "linear", "jira") if _tracker(ctx, t).validate() is None],
        destinations=[d for d in ("slack", "matrix") if _poster(ctx, d).validate() is None],
        crm=["hubspot"] if hubspot.client_for(ctx).validate() is None else [],
    )


class CreateIssuesRequest(ApiModel):
    indices: list[int]


@router.post("/sessions/{session_id}/action-items/{tracker}", response_model=IssueResult)
async def create_issues(
    session_id: str,
    tracker: Tracker,
    request: CreateIssuesRequest,
    ctx: AppContext = Depends(get_ctx),
):
    """Create one issue per selected action item in GitHub, Linear or Jira; remembers each
    issue's URL on the item."""
    session = ctx.sessions.get_session(session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="Session not found")
    if not session.summary_data or not session.summary_data.action_items:
        raise HTTPException(status_code=400, detail="This session has no action items")
    target = _tracker(ctx, tracker)
    if problem := target.validate():
        raise HTTPException(status_code=400, detail=problem)
    try:
        result = await target.create_issues(session, request.indices)
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"{tracker}: {e}") from e
    if result.created:
        ctx.repo.update_fields(session_id, summary_data=session.summary_data)
        ctx.bus.publish(
            {"type": "session", "session_id": session_id, "status": session.status.value}
        )
    return result


class SendRequest(ApiModel):
    destination: Destination
    text: str = ""  # default: the saved follow-up draft


class SendResult(ApiModel):
    ok: bool
    message: str


@router.post("/sessions/{session_id}/followup/send", response_model=SendResult)
async def send_followup(session_id: str, request: SendRequest, ctx: AppContext = Depends(get_ctx)):
    session = ctx.sessions.get_session(session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="Session not found")
    text = request.text.strip() or (
        session.summary_data.followup.strip() if session.summary_data else ""
    )
    if not text:
        raise HTTPException(status_code=400, detail="Draft a follow-up first")
    poster = _poster(ctx, request.destination)
    if problem := poster.validate():
        raise HTTPException(status_code=400, detail=problem)
    try:
        await poster.post(text)
    except Exception as e:
        raise HTTPException(status_code=502, detail=str(e)) from e
    return SendResult(ok=True, message=f"Posted to {request.destination.capitalize()}")


def _crm_session(ctx: AppContext, session_id: str):
    session = ctx.sessions.get_session(session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="Session not found")
    return session


@router.get("/sessions/{session_id}/crm/hubspot", response_model=hubspot.HubSpotState)
async def hubspot_state(session_id: str, ctx: AppContext = Depends(get_ctx)):
    """What this meeting became in HubSpot so far (no network)."""
    if not ctx.repo.exists(session_id):
        raise HTTPException(status_code=404, detail="Session not found")
    return hubspot.load_state(ctx, session_id)


@router.get("/sessions/{session_id}/crm/hubspot/matches", response_model=hubspot.HubSpotMatches)
async def hubspot_matches(session_id: str, ctx: AppContext = Depends(get_ctx)):
    """HubSpot contacts this meeting may belong to: the confirmed ones, then attendees found by
    email, then attendees and speakers found by name."""
    session = _crm_session(ctx, session_id)
    if problem := hubspot.client_for(ctx).validate():
        raise HTTPException(status_code=400, detail=problem)
    try:
        return await hubspot.find_matches(ctx, session)
    except Exception as e:
        raise HTTPException(status_code=502, detail=str(e)) from e


class HubSpotPushRequest(ApiModel):
    contact_ids: list[str]


@router.post("/sessions/{session_id}/crm/hubspot", response_model=hubspot.HubSpotPushResult)
async def hubspot_push(
    session_id: str, request: HubSpotPushRequest, ctx: AppContext = Depends(get_ctx)
):
    """Send the meeting to HubSpot (a meeting, a note and a task per action item, associated
    with these contacts and their companies), or update what an earlier push created. The
    contacts are remembered for the next push and for auto push."""
    session = _crm_session(ctx, session_id)
    if problem := hubspot.client_for(ctx).validate():
        raise HTTPException(status_code=400, detail=problem)
    if not session.summary:
        raise HTTPException(status_code=400, detail="Summarize the meeting first")
    if not any(i.strip() for i in request.contact_ids):
        raise HTTPException(status_code=400, detail="Choose at least one HubSpot contact")
    try:
        return await hubspot.push_session(ctx, session, request.contact_ids)
    except Exception as e:
        raise HTTPException(status_code=502, detail=str(e)) from e
