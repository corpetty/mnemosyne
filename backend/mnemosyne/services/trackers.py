"""Linear and Jira issues from meeting action items (GitHub is in github_service.py).

Each tracker has `validate()` (what is missing from settings), `check()` (can we reach it
and create issues) and `create_issues(session, indices)`, which records each issue's URL
on the action item, like GitHubService."""

from __future__ import annotations

import httpx

from ..models.base import ApiModel
from ..models.session import ActionItem, Session
from .github_service import CreatedIssue, GitHubService, IssueResult


class TrackerCheck(ApiModel):
    ok: bool
    message: str


def _title(item: ActionItem) -> str:
    return item.text if len(item.text) <= 120 else item.text[:117] + "..."


def _error(r: httpx.Response) -> str:
    try:
        body = r.json()
    except ValueError:
        return f"{r.status_code} {r.text[:200]}"
    if isinstance(body, dict):
        for key in ("errorMessages", "errors", "message", "error"):
            if body.get(key):
                return f"{r.status_code} {body[key]}"
    return f"{r.status_code} {str(body)[:200]}"


async def _create_all(session: Session, indices: list[int], create_one) -> IssueResult:
    items = session.summary_data.action_items if session.summary_data else []
    created, skipped, errors = [], [], []
    for i in dict.fromkeys(indices):
        if not (0 <= i < len(items)) or items[i].issue_url:
            skipped.append(i)
            continue
        try:
            url = await create_one(items[i])
        except Exception as e:
            errors.append(f"{items[i].text[:60]}: {e}")
            continue
        items[i] = items[i].model_copy(update={"issue_url": url})
        created.append(CreatedIssue(index=i, url=url))
    return IssueResult(created=created, skipped=skipped, errors=errors)


class LinearTracker:
    name = "linear"
    API = "https://api.linear.app/graphql"

    def __init__(self, api_key: str, team: str, transport=None):
        self.api_key = api_key.strip()
        self.team = team.strip()
        self._transport = transport
        self._team_id: str | None = None

    def validate(self) -> str | None:
        if not self.api_key:
            return "Set a Linear API key in Settings"
        if not self.team:
            return "Set the Linear team key (e.g. ENG) in Settings"
        return None

    async def _gql(self, query: str, variables: dict | None = None) -> dict:
        async with httpx.AsyncClient(timeout=20, transport=self._transport) as c:
            r = await c.post(
                self.API,
                json={"query": query, "variables": variables or {}},
                headers={"Authorization": self.api_key},
            )
        if r.status_code >= 400:
            raise RuntimeError(_error(r))
        body = r.json()
        if body.get("errors"):
            raise RuntimeError(body["errors"][0].get("message", "Linear error"))
        return body["data"]

    async def _resolve_team(self) -> str:
        if self._team_id is None:
            data = await self._gql("query { teams { nodes { id key name } } }")
            for t in data["teams"]["nodes"]:
                if self.team.casefold() in (t["key"].casefold(), t["name"].casefold()):
                    self._team_id = t["id"]
                    break
            else:
                keys = ", ".join(t["key"] for t in data["teams"]["nodes"])
                raise RuntimeError(f"No Linear team {self.team!r} (teams: {keys})")
        return self._team_id

    async def check(self) -> TrackerCheck:
        if problem := self.validate():
            return TrackerCheck(ok=False, message=problem)
        try:
            await self._resolve_team()
        except Exception as e:
            return TrackerCheck(ok=False, message=str(e))
        return TrackerCheck(ok=True, message=f"Ready: issues go to team {self.team}")

    async def create_issues(self, session: Session, indices: list[int]) -> IssueResult:
        team_id = await self._resolve_team()

        async def one(item: ActionItem) -> str:
            data = await self._gql(
                "mutation($input: IssueCreateInput!) { issueCreate(input: $input) "
                "{ success issue { url identifier } } }",
                {
                    "input": {
                        "teamId": team_id,
                        "title": _title(item),
                        "description": GitHubService.issue_body(session, item),
                    }
                },
            )
            result = data["issueCreate"]
            if not result.get("success"):
                raise RuntimeError("Linear refused the issue")
            return result["issue"]["url"]

        return await _create_all(session, indices, one)


def _adf(text: str) -> dict:
    """Plain text as an Atlassian Document Format paragraph list."""
    return {
        "type": "doc",
        "version": 1,
        "content": [
            {"type": "paragraph", "content": [{"type": "text", "text": line}]}
            if line
            else {"type": "paragraph", "content": []}
            for line in text.replace("**", "").split("\n")
        ],
    }


class JiraTracker:
    name = "jira"

    def __init__(self, url, email, token, project, issue_type="Task", transport=None):
        self.url = url.strip().rstrip("/")
        self.email = email.strip()
        self.token = token.strip()
        self.project = project.strip()
        self.issue_type = issue_type.strip() or "Task"
        self._transport = transport

    def validate(self) -> str | None:
        if not self.url.startswith("https://"):
            return "Set the Jira site URL (https://…atlassian.net) in Settings"
        if not (self.email and self.token):
            return "Set the Jira email and API token in Settings"
        if not self.project:
            return "Set the Jira project key in Settings"
        return None

    def _client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            base_url=self.url,
            auth=(self.email, self.token),
            headers={"Accept": "application/json"},
            timeout=20,
            transport=self._transport,
        )

    async def check(self) -> TrackerCheck:
        if problem := self.validate():
            return TrackerCheck(ok=False, message=problem)
        async with self._client() as c:
            r = await c.get(f"/rest/api/3/project/{self.project}")
        if r.status_code == 401:
            return TrackerCheck(ok=False, message="Email or API token rejected")
        if r.status_code == 404:
            return TrackerCheck(ok=False, message=f"No project {self.project} (or no access)")
        if r.status_code >= 400:
            return TrackerCheck(ok=False, message=_error(r))
        name = r.json().get("name", self.project)
        return TrackerCheck(ok=True, message=f"Ready: issues go to {name} ({self.project})")

    async def create_issues(self, session: Session, indices: list[int]) -> IssueResult:
        async with self._client() as c:

            async def one(item: ActionItem) -> str:
                r = await c.post(
                    "/rest/api/3/issue",
                    json={
                        "fields": {
                            "project": {"key": self.project},
                            "summary": _title(item),
                            "issuetype": {"name": self.issue_type},
                            "description": _adf(GitHubService.issue_body(session, item)),
                        }
                    },
                )
                if r.status_code >= 400:
                    raise RuntimeError(_error(r))
                return f"{self.url}/browse/{r.json()['key']}"

            return await _create_all(session, indices, one)
