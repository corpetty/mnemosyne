"""Create GitHub issues from meeting action items."""

from __future__ import annotations

import os
import re

import httpx

from ..models.base import ApiModel
from ..models.session import ActionItem, Session

# Overridable for testing against a local stand-in.
API = os.environ.get("MNEMOSYNE_GITHUB_API", "https://api.github.com")
REPO_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")


class RepoCheck(ApiModel):
    ok: bool
    repo: str
    can_create_issues: bool | None
    message: str


class CreatedIssue(ApiModel):
    index: int
    url: str


class IssueResult(ApiModel):
    created: list[CreatedIssue]
    skipped: list[int]  # already had an issue, or out of range
    errors: list[str]


class GitHubService:
    def __init__(self, repo: str, token: str, labels: str = "", transport=None):
        self.repo = repo.strip()
        self.token = token.strip()
        self.labels = [x.strip() for x in labels.split(",") if x.strip()]
        self._transport = transport

    def _client(self) -> httpx.AsyncClient:
        headers = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        return httpx.AsyncClient(
            base_url=API, headers=headers, timeout=20, transport=self._transport
        )

    def validate(self) -> str | None:
        if not REPO_RE.match(self.repo):
            return "Set the repository as owner/name in Settings"
        if not self.token:
            return "Set a GitHub token in Settings"
        return None

    async def check(self) -> RepoCheck:
        def fail(msg: str, can: bool | None = None) -> RepoCheck:
            return RepoCheck(ok=False, repo=self.repo, can_create_issues=can, message=msg)

        if not REPO_RE.match(self.repo):
            return fail("Use owner/name")
        async with self._client() as c:
            r = await c.get(f"/repos/{self.repo}")
        if r.status_code == 404:
            return fail("Repository not found (or the token cannot see it)")
        if r.status_code == 401:
            return fail("Token rejected")
        r.raise_for_status()
        data = r.json()
        if not data.get("has_issues", True):
            return fail("Issues are disabled on this repository", can=False)
        perms = data.get("permissions")
        can = any(perms.get(k) for k in ("push", "triage", "maintain", "admin")) if perms else None
        if can:
            msg = "Ready: issues can be created"
        elif can is False:
            msg = "Repository found, but this token may not be allowed to create issues"
        else:
            msg = "Repository found (add a token to create issues)"
        return RepoCheck(
            ok=True, repo=data.get("full_name", self.repo), can_create_issues=can, message=msg
        )

    @staticmethod
    def issue_body(session: Session, item: ActionItem) -> str:
        lines = [
            f"Action item from the meeting **{session.name}** "
            f"({session.created_at.strftime('%Y-%m-%d %H:%M')}).",
            "",
            f"> {item.text}",
        ]
        if item.owner:
            lines += ["", f"Owner (as named in the meeting): {item.owner}"]
        d = session.summary_data
        if d and d.decisions:
            lines += ["", "Decisions in that meeting:"] + [f"- {x}" for x in d.decisions[:5]]
        lines += ["", "_Created by Mnemosyne._"]
        return "\n".join(lines)

    async def create_issues(self, session: Session, indices: list[int]) -> IssueResult:
        """Create issues for the selected items; updates `session.summary_data` in place."""
        items = session.summary_data.action_items if session.summary_data else []
        created, skipped, errors = [], [], []
        async with self._client() as c:
            for i in dict.fromkeys(indices):
                if not (0 <= i < len(items)) or items[i].issue_url:
                    skipped.append(i)
                    continue
                text = items[i].text
                title = text if len(text) <= 120 else text[:117] + "..."
                payload: dict = {"title": title, "body": self.issue_body(session, items[i])}
                if self.labels:
                    payload["labels"] = self.labels
                r = await c.post(f"/repos/{self.repo}/issues", json=payload)
                if r.status_code == 422 and "labels" in payload:
                    # Repos without these labels may reject them; retry without.
                    payload.pop("labels")
                    r = await c.post(f"/repos/{self.repo}/issues", json=payload)
                if r.status_code >= 400:
                    try:
                        detail = r.json().get("message", r.text)
                    except ValueError:
                        detail = r.text
                    errors.append(f"{text[:60]}: {r.status_code} {detail}")
                    continue
                url = r.json()["html_url"]
                items[i] = items[i].model_copy(update={"issue_url": url})
                created.append(CreatedIssue(index=i, url=url))
        return IssueResult(created=created, skipped=skipped, errors=errors)
