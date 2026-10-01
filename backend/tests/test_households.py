"""Households (services/households.py): grouping, meetings and facts, the brief, HubSpot sync."""

import json
from datetime import datetime, timedelta

import httpx
from fastapi.testclient import TestClient

from mnemosyne.api.app import create_app
from mnemosyne.models.session import ActionItem, ClientFact, SummaryData

TOKEN = "pat-na1-test"


def _meeting(ctx, name, facts=(), participants=(), attendees=(), days_ago=0, tasks=()):
    session = ctx.sessions.create_session(name)
    ctx.repo.update_fields(session.id, participants=list(participants), attendees=list(attendees))
    with ctx.repo._lock, ctx.repo._conn:
        ctx.repo._conn.execute(
            "UPDATE sessions SET created_at=? WHERE id=?",
            ((datetime.now() - timedelta(days=days_ago)).isoformat(), session.id),
        )
    data = SummaryData(
        style="advisory",
        client_facts=[ClientFact(kind=k, text=t) for k, t in facts],
        action_items=[ActionItem(text=t) for t in tasks],
    )
    ctx.sessions.set_summary(session.id, "Summary", data)
    return session.id


def _lopez(client):
    r = client.post(
        "/api/households",
        json={
            "name": "Lopez household",
            "members": [{"name": "Maria  Lopez"}, {"name": "David Lopez", "email": "D@x.com"}],
        },
    )
    assert r.status_code == 200
    return r.json()


def test_a_households_meetings_and_facts(client, ctx):
    h = _lopez(client)
    assert [m["name"] for m in h["members"]] == ["Maria Lopez", "David Lopez"]
    old = _meeting(ctx, "Annual review", [("goal", "Retire at 65")], ["Maria Lopez"], days_ago=365)
    new = _meeting(
        ctx,
        "Annual review",
        [("goal", "Retire at 62"), ("life_event", "Emma starts college in 2027")],
        attendees=["d@x.com"],
        days_ago=2,
        tasks=["Send the beneficiary form"],
    )
    titled = _meeting(ctx, "Call with Maria Lopez", days_ago=30)
    _meeting(ctx, "Someone else", [("goal", "Buy a boat")], ["Maria Smith"], days_ago=1)

    [summary] = client.get("/api/households").json()
    assert summary["meetings"] == 3 and summary["facts"] == 3
    detail = client.get(f"/api/households/{h['id']}").json()
    assert [m["id"] for m in detail["meetings"]] == [new, titled, old]
    assert [f["text"] for f in detail["facts"]] == [
        "Retire at 62",
        "Emma starts college in 2027",
        "Retire at 65",
    ]
    assert [t["text"] for t in detail["open_tasks"]] == ["Send the beneficiary form"]


def test_the_brief_shows_the_last_reviews_facts(client, ctx):
    _lopez(client)
    _meeting(ctx, "Annual review", [("goal", "Retire at 65")], ["Maria Lopez"], days_ago=365)
    _meeting(ctx, "Annual review", [("goal", "Retire at 62")], ["Maria Lopez"], days_ago=180)
    _meeting(ctx, "Quick call", [], ["Maria Lopez"], days_ago=10)  # no facts: skipped
    brief = client.get("/api/brief", params={"title": "Review with Maria Lopez"}).json()
    household = brief["household"]
    assert household["name"] == "Lopez household"
    assert [f["text"] for f in household["facts"]] == ["Retire at 62"]
    assert household["earlier_facts"] == 1
    by_email = client.get("/api/brief", params={"attendees": ["d@x.com"]}).json()
    assert by_email["household"]["name"] == "Lopez household"
    assert client.get("/api/brief", params={"title": "Maria"}).json()["household"] is None


def test_a_person_is_in_one_household(client, ctx):
    first = _lopez(client)
    client.post("/api/households", json={"name": "Other", "members": [{"name": "maria lopez"}]})
    left = client.get(f"/api/households/{first['id']}").json()
    assert [m["name"] for m in left["members"]] == ["David Lopez"]
    r = client.put(f"/api/households/{first['id']}", json={"name": "Lopez", "members": []})
    assert r.json()["name"] == "Lopez" and r.json()["members"] == []
    assert client.delete(f"/api/households/{first['id']}").status_code == 200
    assert client.get(f"/api/households/{first['id']}").status_code == 404


def test_advisors_see_facts_only_from_their_own_meetings(settings, keystore):
    settings.team_mode = True
    app = create_app(settings, keystore=keystore)
    ctx = app.state.ctx
    tokens = {}
    for name in ("Ann", "Bob"):
        user = ctx.users.add(name, "", "advisor")
        code, _ = ctx.users.invite(user.id)
        tokens[name] = {"Authorization": f"Bearer {ctx.users.redeem(code, 'x')[1]}"}
    with TestClient(app) as client:
        client.headers.update(tokens["Ann"])
        h = _lopez(client)
        sid = client.post("/api/sessions", json={"name": "Review with Maria Lopez"}).json()["id"]
        data = SummaryData(style="advisory", client_facts=[ClientFact(kind="goal", text="Ann's")])
        ctx.repo.update_fields(sid, summary_data=data)
        assert len(client.get(f"/api/households/{h['id']}").json()["facts"]) == 1
        client.headers.update(tokens["Bob"])
        detail = client.get(f"/api/households/{h['id']}").json()
        assert detail["members"] and detail["facts"] == [] and detail["meetings"] == []


def test_hubspot_companies_become_households(client, ctx):
    contacts = [
        {"id": "1", "firstname": "Maria", "lastname": "Lopez", "email": "m@x.com", "co": "900"},
        {"id": "2", "firstname": "David", "lastname": "Lopez", "email": "", "co": "900"},
        {"id": "3", "firstname": "Solo", "lastname": "Person", "email": "", "co": None},
        {"id": "4", "firstname": "Tom", "lastname": "Smith", "email": "t@s.org", "co": "901"},
    ]
    requests: list[httpx.Request] = []

    def fake(req: httpx.Request) -> httpx.Response:
        requests.append(req)
        assert req.headers["Authorization"] == f"Bearer {TOKEN}"
        if req.method == "GET" and req.url.path == "/crm/v3/objects/contacts":
            start = int(req.url.params.get("after", "0"))
            page = contacts[start : start + 2]  # two per page: paging is followed
            results = [
                {
                    "id": c["id"],
                    "properties": {
                        "firstname": c["firstname"],
                        "lastname": c["lastname"],
                        "email": c["email"],
                        "associatedcompanyid": c["co"],
                    },
                }
                for c in page
            ]
            more = start + 2 < len(contacts)
            return httpx.Response(
                200,
                json={
                    "results": results,
                    **({"paging": {"next": {"after": str(start + 2)}}} if more else {}),
                },
            )
        if req.url.path == "/crm/v3/objects/companies/batch/read":
            names = {"900": "Lopez Family", "901": "Smith Trust"}
            ids = [i["id"] for i in json.loads(req.content)["inputs"]]
            return httpx.Response(
                200, json={"results": [{"id": i, "properties": {"name": names[i]}} for i in ids]}
            )
        raise AssertionError(f"unexpected {req.method} {req.url.path}")

    ctx.http_transport = httpx.MockTransport(fake)
    ctx.settings.hubspot_token = TOKEN
    manual = client.post(
        "/api/households", json={"name": "Mine", "members": [{"name": "Neighbor Joe"}]}
    ).json()
    r = client.post("/api/households/sync-hubspot").json()
    assert r == {"created": 2, "updated": 0, "households": 2}
    # Again, with a member added by hand to a synced one: updated, the hand-added member stays.
    lopez = next(h for h in client.get("/api/households").json() if h["name"] == "Lopez Family")
    members = [*lopez["members"], {"name": "Emma Lopez"}]
    client.put(f"/api/households/{lopez['id']}", json={"name": "Lopez Family", "members": members})
    assert client.post("/api/households/sync-hubspot").json()["updated"] == 2
    found = {h["name"]: h for h in client.get("/api/households").json()}
    assert [(m["name"], m["source"]) for m in found["Lopez Family"]["members"]] == [
        ("Maria Lopez", "hubspot"),
        ("David Lopez", "hubspot"),
        ("Emma Lopez", "manual"),
    ]
    assert found["Lopez Family"]["hubspot_company_id"] == "900"
    assert found["Mine"]["id"] == manual["id"]
    assert all(r.method == "GET" or "batch/read" in r.url.path for r in requests)  # reads only
