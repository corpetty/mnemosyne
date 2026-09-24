"""Linear and Jira issues, Slack and Matrix posts, against mocked APIs."""

import base64
import json
from datetime import datetime

import httpx

from mnemosyne.models.session import ActionItem, Session, SummaryData


def _session(ctx, followup="Recap: ship in October."):
    s = Session(
        name="Release sync",
        created_at=datetime(2026, 9, 21, 10),
        summary="s",
        summary_data=SummaryData(
            decisions=["Ship in October"],
            action_items=[
                ActionItem(text="Update docs", owner="Alice"),
                ActionItem(text="Tag rc1"),
            ],
            followup=followup,
        ),
    )
    return ctx.repo.save(s)


class Recorder:
    def __init__(self, handler):
        self.handler = handler
        self.requests = []

    def __call__(self, request):
        self.requests.append(request)
        return self.handler(request)


def test_linear(client, ctx):
    def handle(req):
        assert req.url == "https://api.linear.app/graphql"
        assert req.headers["Authorization"] == "lin_api_x"
        body = json.loads(req.content)
        if "teams" in body["query"]:
            return httpx.Response(
                200,
                json={"data": {"teams": {"nodes": [{"id": "t1", "key": "ENG", "name": "Eng"}]}}},
            )
        inp = body["variables"]["input"]
        assert inp["teamId"] == "t1" and "Release sync" in inp["description"]
        n = len([r for r in rec.requests if b"issueCreate" in r.content])
        return httpx.Response(
            200,
            json={
                "data": {
                    "issueCreate": {
                        "success": True,
                        "issue": {
                            "url": f"https://linear.app/x/issue/ENG-{n}",
                            "identifier": f"ENG-{n}",
                        },
                    }
                }
            },
        )

    rec = Recorder(handle)
    ctx.http_transport = httpx.MockTransport(rec)
    s = _session(ctx)
    assert (
        client.post(f"/api/sessions/{s.id}/action-items/linear", json={"indices": [0]}).status_code
        == 400
    )
    ctx.settings.linear_api_key, ctx.settings.linear_team = "lin_api_x", "eng"
    assert client.get("/api/integrations/linear/check").json() == {
        "ok": True,
        "message": "Ready: issues go to team eng",
    }
    r = client.post(
        f"/api/sessions/{s.id}/action-items/linear", json={"indices": [0, 1, 0, 7]}
    ).json()
    assert [c["url"] for c in r["created"]] == [
        "https://linear.app/x/issue/ENG-1",
        "https://linear.app/x/issue/ENG-2",
    ]
    assert r["skipped"] == [7]
    items = ctx.repo.get(s.id).summary_data.action_items
    assert items[0].issue_url.endswith("ENG-1")
    ctx.settings.linear_team = "OPS"
    check = client.get("/api/integrations/linear/check").json()
    assert check["ok"] is False and "teams: ENG" in check["message"]


def test_jira(client, ctx):
    def handle(req):
        auth = base64.b64decode(req.headers["Authorization"].split()[1]).decode()
        assert auth == "me@x.io:tok"
        if req.method == "GET":
            if req.url.path == "/rest/api/3/project/OPS":
                return httpx.Response(200, json={"name": "Operations"})
            return httpx.Response(404, json={"errorMessages": ["No project"]})
        fields = json.loads(req.content)["fields"]
        assert fields["project"] == {"key": "OPS"} and fields["issuetype"] == {"name": "Task"}
        assert fields["description"]["type"] == "doc"
        if fields["summary"] == "Tag rc1":
            return httpx.Response(400, json={"errors": {"summary": "bad"}})
        return httpx.Response(201, json={"key": "OPS-7"})

    ctx.http_transport = httpx.MockTransport(handle)
    st = ctx.settings
    st.jira_url, st.jira_email, st.jira_api_token, st.jira_project = (
        "https://x.atlassian.net/",
        "me@x.io",
        "tok",
        "OPS",
    )
    s = _session(ctx)
    assert client.get("/api/integrations/jira/check").json()["message"] == (
        "Ready: issues go to Operations (OPS)"
    )
    r = client.post(f"/api/sessions/{s.id}/action-items/jira", json={"indices": [0, 1]}).json()
    assert [c["url"] for c in r["created"]] == ["https://x.atlassian.net/browse/OPS-7"]
    assert len(r["errors"]) == 1 and r["errors"][0].startswith("Tag rc1: 400")
    st.jira_project = "NOPE"
    assert client.get("/api/integrations/jira/check").json()["ok"] is False


def test_slack_and_matrix_posts(client, ctx):
    posted = []

    def handle(req):
        if req.url.host == "hooks.slack.com":
            posted.append(("slack", json.loads(req.content)["text"]))
            return httpx.Response(200, text="ok")
        assert req.headers["Authorization"] == "Bearer syt_x"
        if req.url.path.endswith("/whoami"):
            return httpx.Response(200, json={"user_id": "@corey:matrix.org"})
        if req.url.path.endswith("/joined_rooms"):
            return httpx.Response(200, json={"joined_rooms": ["!room:matrix.org"]})
        assert "/rooms/%21room%3Amatrix.org/send/m.room.message/" in req.url.raw_path.decode()
        posted.append(("matrix", json.loads(req.content)["body"]))
        return httpx.Response(200, json={"event_id": "$e"})

    ctx.http_transport = httpx.MockTransport(handle)
    s = _session(ctx)
    st = ctx.settings
    assert client.get("/api/integrations").json() == {"trackers": [], "destinations": []}
    r = client.post(f"/api/sessions/{s.id}/followup/send", json={"destination": "slack"})
    assert r.status_code == 400
    st.slack_webhook_url = "https://hooks.slack.com/services/T/B/x"
    st.matrix_homeserver, st.matrix_access_token, st.matrix_room_id = (
        "https://matrix.org",
        "syt_x",
        "!room:matrix.org",
    )
    assert client.get("/api/integrations").json()["destinations"] == ["slack", "matrix"]
    assert client.get("/api/integrations/matrix/check").json()["message"] == (
        "Ready: posting as @corey:matrix.org"
    )
    assert client.post(f"/api/sessions/{s.id}/followup/send", json={"destination": "slack"}).json()[
        "ok"
    ]
    r = client.post(
        f"/api/sessions/{s.id}/followup/send", json={"destination": "matrix", "text": "edited"}
    )
    assert r.json() == {"ok": True, "message": "Posted to Matrix"}
    assert posted == [("slack", "Recap: ship in October."), ("matrix", "edited")]
    empty = _session(ctx, followup="")
    r = client.post(f"/api/sessions/{empty.id}/followup/send", json={"destination": "slack"})
    assert r.status_code == 400
