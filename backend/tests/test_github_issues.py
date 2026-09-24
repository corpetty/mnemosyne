"""Action items -> GitHub issues, against a mocked GitHub API."""

import json

import httpx
import pytest

from mnemosyne.models.session import ActionItem, SummaryData


class FakeGitHub:
    def __init__(self, labels_exist=True, fail_titles=()):
        self.created = []
        self.labels_exist = labels_exist
        self.fail_titles = set(fail_titles)

    def handler(self, request: httpx.Request) -> httpx.Response:
        assert request.headers["Authorization"] == "Bearer ghp_test"
        assert request.headers["X-GitHub-Api-Version"] == "2022-11-28"
        if request.method == "GET" and request.url.path == "/repos/corpetty/notes":
            return httpx.Response(
                200,
                json={
                    "full_name": "corpetty/notes",
                    "has_issues": True,
                    "permissions": {"push": True},
                },
            )
        if request.method == "GET":
            return httpx.Response(404, json={"message": "Not Found"})
        body = json.loads(request.content)
        if "labels" in body and not self.labels_exist:
            return httpx.Response(422, json={"message": "Validation Failed"})
        if body["title"] in self.fail_titles:
            return httpx.Response(403, json={"message": "Resource not accessible"})
        n = len(self.created) + 1
        self.created.append(body)
        return httpx.Response(
            201, json={"html_url": f"https://github.com/corpetty/notes/issues/{n}"}
        )


@pytest.fixture
def github(ctx):
    gh = FakeGitHub()
    ctx.http_transport = httpx.MockTransport(gh.handler)
    ctx.settings.github_repo = "corpetty/notes"
    ctx.settings.github_token = "ghp_test"
    ctx.settings.github_labels = "meeting-action, mnemosyne"
    return gh


@pytest.fixture
def session_with_items(ctx):
    s = ctx.sessions.create_session("Release sync")
    ctx.sessions.set_summary(
        s.id,
        "Summary",
        SummaryData(
            decisions=["Ship in October"],
            action_items=[
                ActionItem(text="Update docs", owner="Alice"),
                ActionItem(text="Tag release"),
            ],
        ),
    )
    return s


def test_check(client, github):
    body = client.get("/api/integrations/github/check").json()
    assert body == {
        "ok": True,
        "repo": "corpetty/notes",
        "can_create_issues": True,
        "message": "Ready: issues can be created",
    }


def test_check_bad_repo(client, ctx, github):
    ctx.settings.github_repo = "corpetty/missing"
    assert client.get("/api/integrations/github/check").json()["ok"] is False
    ctx.settings.github_repo = "not a repo"
    assert client.get("/api/integrations/github/check").json()["message"] == "Use owner/name"


def test_create_issues_and_no_duplicates(client, github, session_with_items):
    sid = session_with_items.id
    r = client.post(
        f"/api/sessions/{sid}/action-items/github", json={"indices": [0, 1, 1, 7]}
    ).json()
    assert [c["index"] for c in r["created"]] == [0, 1]
    assert r["skipped"] == [7] and r["errors"] == []
    first = github.created[0]
    assert first["title"] == "Update docs" and first["labels"] == ["meeting-action", "mnemosyne"]
    assert (
        "**Release sync**" in first["body"]
        and "Owner (as named in the meeting): Alice" in first["body"]
    )
    assert "- Ship in October" in first["body"]
    items = client.get(f"/api/sessions/{sid}").json()["summary_data"]["action_items"]
    assert items[0]["issue_url"].endswith("/issues/1")
    # second call creates nothing
    again = client.post(f"/api/sessions/{sid}/action-items/github", json={"indices": [0, 1]}).json()
    assert again["created"] == [] and again["skipped"] == [0, 1] and len(github.created) == 2


def test_missing_labels_retry_and_partial_errors(client, ctx, session_with_items):
    gh = FakeGitHub(labels_exist=False, fail_titles={"Tag release"})
    ctx.http_transport = httpx.MockTransport(gh.handler)
    ctx.settings.github_repo, ctx.settings.github_token = "corpetty/notes", "ghp_test"
    r = client.post(
        f"/api/sessions/{session_with_items.id}/action-items/github", json={"indices": [0, 1]}
    ).json()
    assert [c["index"] for c in r["created"]] == [0]
    assert "labels" not in gh.created[0]
    assert r["errors"] and "403" in r["errors"][0]


def test_validation(client, ctx, session_with_items):
    url = f"/api/sessions/{session_with_items.id}/action-items/github"
    assert client.post(url, json={"indices": [0]}).status_code == 400  # not configured
    ctx.settings.github_repo = "corpetty/notes"
    assert "token" in client.post(url, json={"indices": [0]}).json()["detail"]
    empty = client.post("/api/sessions", json={}).json()["id"]
    assert (
        client.post(f"/api/sessions/{empty}/action-items/github", json={"indices": [0]}).status_code
        == 400
    )
    assert (
        client.post("/api/sessions/nope/action-items/github", json={"indices": [0]}).status_code
        == 404
    )


def test_token_is_secret(client, ctx):
    client.put("/api/settings", json={"github_token": "ghp_x"})
    body = client.get("/api/settings").json()
    assert body["values"]["github_token"] == "" and body["secrets_set"]["github_token"]


def test_export_links_issues(tmp_path, ctx, github, session_with_items, client):
    from mnemosyne.export.obsidian import ObsidianExporter

    client.post(f"/api/sessions/{session_with_items.id}/action-items/github", json={"indices": [0]})
    md = ObsidianExporter(str(tmp_path)).render(ctx.sessions.get_session(session_with_items.id))
    assert "- [ ] Update docs ([[Alice]]) [issue](https://github.com/corpetty/notes/issues/1)" in md
