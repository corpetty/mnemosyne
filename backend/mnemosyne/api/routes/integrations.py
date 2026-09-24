"""GitHub issues from action items."""

from fastapi import APIRouter, Depends, HTTPException

from ...models.base import ApiModel
from ...services.github_service import GitHubService, IssueResult, RepoCheck
from ..context import AppContext, get_ctx

router = APIRouter(prefix="/api", tags=["integrations"])


def _github(ctx: AppContext) -> GitHubService:
    st = ctx.settings
    return GitHubService(st.github_repo, st.github_token, st.github_labels, ctx.github_transport)


@router.get("/integrations/github/check", response_model=RepoCheck)
async def check_github(ctx: AppContext = Depends(get_ctx)):
    try:
        return await _github(ctx).check()
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"GitHub: {e}") from e


class CreateIssuesRequest(ApiModel):
    indices: list[int]


@router.post("/sessions/{session_id}/action-items/github", response_model=IssueResult)
async def create_github_issues(
    session_id: str, request: CreateIssuesRequest, ctx: AppContext = Depends(get_ctx)
):
    """Create one issue per selected action item; remembers each issue's URL."""
    session = ctx.sessions.get_session(session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="Session not found")
    if not session.summary_data or not session.summary_data.action_items:
        raise HTTPException(status_code=400, detail="This session has no action items")
    gh = _github(ctx)
    if problem := gh.validate():
        raise HTTPException(status_code=400, detail=problem)
    result = await gh.create_issues(session, request.indices)
    if result.created:
        ctx.repo.update_fields(session_id, summary_data=session.summary_data)
        ctx.bus.publish(
            {"type": "session", "session_id": session_id, "status": session.status.value}
        )
    return result
