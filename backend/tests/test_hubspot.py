"""HubSpot: matching, pushing and updating a meeting, against a fake HubSpot (no network)."""

import json
from datetime import date, datetime

import httpx

from mnemosyne.models.session import ActionItem, Session, SummaryData
from mnemosyne.models.transcript import TranscriptSegment
from mnemosyne.services import hubspot
from tests.conftest import run_summarize

TOKEN = "pat-na1-test"


class FakeHubSpot:
    """Just enough of the CRM API: contacts, companies, owners, search, objects, associations."""

    def __init__(self):
        self.contacts = {
            "101": {"firstname": "Jane", "lastname": "Client", "email": "jane@client.com",
                    "company": "", "associatedcompanyid": "900"},
            "102": {"firstname": "Bob", "lastname": "Other", "email": "bob@x.com",
                    "company": "Bob's own text", "associatedcompanyid": None},
            "103": {"firstname": "Tom", "lastname": "Smith", "email": "tom@smith.org",
                    "company": "", "associatedcompanyid": "900"},
            "104": {"firstname": "Tom", "lastname": "Jones", "email": "tj@x.com",
                    "company": "", "associatedcompanyid": None},
        }  # fmt: skip
        self.companies = {"900": {"name": "Client Household"}}
        self.owners = [{"id": "77", "email": "advisor@firm.com", "firstName": "Ann",
                        "lastName": "Advisor", "userId": 5}]  # fmt: skip
        self.objects: dict[str, dict[str, dict]] = {"meetings": {}, "notes": {}, "tasks": {}}
        self.associations: set[tuple[str, str, str, str, int | None]] = set()
        self.requests: list[httpx.Request] = []
        self.fail: int | None = None  # answer every request with this status
        self._next = 5000

    def writes(self, method: str | None = None) -> list[httpx.Request]:
        return [
            r
            for r in self.requests
            if r.method in ("POST", "PATCH", "PUT")
            and "search" not in r.url.path
            and "batch/read" not in r.url.path
            and (method is None or r.method == method)
        ]

    def __call__(self, req: httpx.Request) -> httpx.Response:
        self.requests.append(req)
        assert req.url.host == "api.hubapi.com"
        if req.headers.get("Authorization") != f"Bearer {TOKEN}":
            return httpx.Response(
                401, json={"status": "error", "message": "Authentication credentials not found."}
            )
        if self.fail:
            return httpx.Response(self.fail, json={"status": "error", "message": "boom"})
        path = req.url.path
        body = json.loads(req.content) if req.content else {}
        if req.method == "GET" and path == "/crm/v3/objects/contacts":
            return httpx.Response(200, json={"results": []})
        if req.method == "GET" and path == "/crm/v3/owners":
            want = req.url.params.get("email", "").lower()
            return httpx.Response(
                200, json={"results": [o for o in self.owners if o["email"] == want]}
            )
        if path == "/crm/v3/objects/contacts/search":
            assert len(body["filterGroups"]) <= 5
            hits = [
                {"id": cid, "properties": dict(p, hs_object_id=cid)}
                for cid, p in self.contacts.items()
                if any(all(self._match(p, f) for f in g["filters"]) for g in body["filterGroups"])
            ]
            return httpx.Response(200, json={"total": len(hits), "results": hits[: body["limit"]]})
        if path == "/crm/v3/objects/contacts/batch/read":
            ids = [i["id"] for i in body["inputs"]]
            return httpx.Response(
                200,
                json={
                    "status": "COMPLETE",
                    "results": [
                        {"id": i, "properties": self.contacts[i]} for i in ids if i in self.contacts
                    ],
                },
            )
        if path == "/crm/v3/objects/companies/batch/read":
            return httpx.Response(
                200,
                json={
                    "results": [
                        {"id": i["id"], "properties": self.companies[i["id"]]}
                        for i in body["inputs"]
                    ]
                },
            )
        parts = path.strip("/").split("/")
        if req.method == "POST" and parts[:3] == ["crm", "v3", "objects"] and len(parts) == 4:
            kind = parts[3]
            self._next += 1
            oid = str(self._next)
            self.objects[kind][oid] = body["properties"]
            for a in body.get("associations", []):
                (t,) = a["types"]
                assert t["associationCategory"] == "HUBSPOT_DEFINED"
                self.associations.add((kind, oid, "?", a["to"]["id"], t["associationTypeId"]))
            return httpx.Response(201, json={"id": oid, "properties": body["properties"]})
        if req.method == "PATCH" and parts[:3] == ["crm", "v3", "objects"]:
            kind, oid = parts[3], parts[4]
            if oid not in self.objects[kind]:
                return httpx.Response(404, json={"status": "error", "message": "Not found"})
            self.objects[kind][oid].update(body["properties"])
            return httpx.Response(200, json={"id": oid})
        if req.method == "PUT" and parts[:3] == ["crm", "v4", "objects"]:
            _, _, _, from_type, oid, _, _, to_type, to_id = parts
            names = {v: k for k, v in hubspot.OBJECT_TYPE_IDS.items()}
            self.associations.add((names[from_type], oid, names[to_type], to_id, None))
            return httpx.Response(200, json={})
        raise AssertionError(f"unexpected {req.method} {path}")

    @staticmethod
    def _match(props: dict, f: dict) -> bool:
        value = str(props.get(f["propertyName"]) or "").lower()
        if f["operator"] == "IN":
            return value in f["values"]
        assert f["operator"] == "EQ"
        return value == f["value"].lower()

    def assoc(self, kind: str, oid: str) -> set[tuple[str, int | None]]:
        return {(to, t) for k, o, _, to, t in self.associations if (k, o) == (kind, oid)}


def _setup(ctx, owner="advisor@firm.com"):
    fake = FakeHubSpot()
    ctx.http_transport = httpx.MockTransport(fake)
    ctx.settings.hubspot_token = TOKEN
    ctx.settings.hubspot_owner_email = owner
    return fake


def _session(ctx) -> Session:
    s = Session(
        name="Annual review",
        created_at=datetime(2026, 9, 21, 10),
        attendees=["Jane Client", "bob@x.com", "Ann Advisor"],
        transcript=[
            TranscriptSegment(text="Hello", speaker="Tom Smith", start=0, end=5),
            TranscriptSegment(text="Hi", speaker="Speaker 1", start=5, end=10),
            TranscriptSegment(text="So", speaker="Me", start=10, end=1800),
        ],
        summary="## Overview\n- Reviewed the **portfolio**\n- Daughter <starts> college",
        summary_data=SummaryData(
            decisions=["Keep the allocation"],
            open_questions=["Revisit the 529?"],
            action_items=[
                ActionItem(text="Send the 529 comparison", owner="Ann", due=date(2026, 10, 1)),
                ActionItem(text="Book the next review", done=True),
            ],
        ),
    )
    ctx.repo.save(s)
    ctx.repo.set_attendee_emails(s.id, {"Jane Client": "JANE@client.com"})
    return s


def test_check(client, ctx):
    assert client.get("/api/integrations").json()["crm"] == []
    r = client.get("/api/integrations/hubspot/check").json()
    assert r == {"ok": False, "message": "Set a HubSpot private app access token in Settings"}
    fake = _setup(ctx)
    assert client.get("/api/integrations").json()["crm"] == ["hubspot"]
    r = client.get("/api/integrations/hubspot/check").json()
    assert r == {"ok": True, "message": "Ready: tasks are assigned to Ann Advisor"}
    ctx.settings.hubspot_owner_email = "nobody@firm.com"
    r = client.get("/api/integrations/hubspot/check").json()
    assert r["ok"] is False and "No HubSpot user with the email nobody@firm.com" in r["message"]
    ctx.settings.hubspot_token = "wrong"
    r = client.get("/api/integrations/hubspot/check").json()
    assert r["ok"] is False and "rejected the access token" in r["message"]
    ctx.settings.hubspot_token = TOKEN
    fake.fail = 403
    r = client.get("/api/integrations/hubspot/check").json()
    assert r["ok"] is False and r["message"].startswith("The HubSpot token is missing a scope")
    # the token is a secret: never echoed back
    ctx.settings.hubspot_owner_email = ""
    fake.fail = None
    values = client.get("/api/settings").json()
    assert values["secrets_set"]["hubspot_token"] is True
    assert values["values"].get("hubspot_token") in (None, "")


def test_matches_by_email_then_name(client, ctx):
    s = _session(ctx)
    assert client.get(f"/api/sessions/{s.id}/crm/hubspot/matches").status_code == 400
    _setup(ctx)
    r = client.get(f"/api/sessions/{s.id}/crm/hubspot/matches").json()
    got = [(c["id"], c["matched_by"], c["matched_on"], c["company"]) for c in r["candidates"]]
    assert got == [
        ("101", "email", "jane@client.com", "Client Household"),  # the calendar's address
        ("102", "email", "bob@x.com", "Bob's own text"),  # an attendee written as an email
        ("103", "name", "Tom Smith", "Client Household"),  # a speaker; Tom Jones is not him
    ]
    # generic speakers ("Speaker 1", "Me") are never searched; Jane was found by email
    assert r["searched"] == ["jane@client.com", "bob@x.com", "Ann Advisor", "Tom Smith"]
    assert r["state"]["pushed_at"] is None


def test_push_creates_then_updates(client, ctx):
    fake = _setup(ctx)
    s = _session(ctx)
    r = client.post(f"/api/sessions/{s.id}/crm/hubspot", json={"contact_ids": ["101", "103"]})
    assert r.status_code == 200, r.text
    res = r.json()
    assert res["created"] is True
    assert res["message"] == "Sent to HubSpot for Jane Client, Tom Smith with 2 tasks"
    st = res["state"]
    assert [c["id"] for c in st["contacts"]] == ["101", "103"]
    assert len(fake.objects["meetings"]) == 1 and len(fake.objects["notes"]) == 1
    assert len(fake.objects["tasks"]) == 2

    meeting = fake.objects["meetings"][st["meeting_id"]]
    assert meeting["hs_meeting_title"] == "Annual review"
    assert meeting["hubspot_owner_id"] == "77"
    assert meeting["hs_meeting_body"] == (
        "<p><strong>Overview</strong></p><ul><li>Reviewed the <strong>portfolio</strong></li>"
        "<li>Daughter &lt;starts&gt; college</li></ul>"
    )
    start = datetime.fromisoformat(meeting["hs_meeting_start_time"])
    end = datetime.fromisoformat(meeting["hs_meeting_end_time"])
    assert (end - start).total_seconds() == 1800
    assert meeting["hs_timestamp"] == meeting["hs_meeting_start_time"]
    # one company for both contacts (the household), each association with its default type
    assert fake.assoc("meetings", st["meeting_id"]) == {("101", 200), ("103", 200), ("900", 188)}
    note = fake.objects["notes"][st["note_id"]]
    assert "Keep the allocation" in note["hs_note_body"]
    assert "Revisit the 529?" in note["hs_note_body"]
    assert fake.assoc("notes", st["note_id"]) == {("101", 202), ("103", 202), ("900", 190)}
    t1, t2 = (fake.objects["tasks"][i] for i in st["task_ids"])
    assert t1["hs_task_subject"] == "Send the 529 comparison"
    assert t1["hs_task_status"] == "NOT_STARTED" and t2["hs_task_status"] == "COMPLETED"
    assert t1["hubspot_owner_id"] == "77"
    assert t1["hs_timestamp"].startswith("2026-10-01")
    assert "Annual review" in t1["hs_task_body"]
    assert fake.assoc("tasks", st["task_ids"][0]) == {("101", 204), ("103", 204), ("900", 192)}
    events = [e for e in ctx.repo.events(s.id) if e["kind"] == "hubspot_pushed"]
    assert len(events) == 1 and events[0]["detail"]["created"] is True
    assert client.get(f"/api/sessions/{s.id}/crm/hubspot").json()["meeting_id"] == st["meeting_id"]

    # Again, with Bob added and a new action item: same records, patched; Bob associated; the
    # new item becomes a new task with every association.
    data = s.summary_data
    data.action_items.append(ActionItem(text="Call the CPA"))
    ctx.repo.update_fields(s.id, summary_data=data)
    fake.requests.clear()
    res = client.post(
        f"/api/sessions/{s.id}/crm/hubspot", json={"contact_ids": ["101", "103", "102"]}
    ).json()
    assert res["created"] is False and res["message"].startswith("Updated in HubSpot")
    assert len(fake.objects["meetings"]) == 1 and len(fake.objects["notes"]) == 1
    assert len(fake.objects["tasks"]) == 3
    creates = [r.url.path for r in fake.writes("POST")]
    assert creates == ["/crm/v3/objects/tasks"]
    assert len(fake.writes("PATCH")) == 4  # meeting, note, two tasks
    puts = {r.url.path for r in fake.writes("PUT")}
    assert all(p.endswith("/associations/default/0-1/102") for p in puts)
    assert len(puts) == 4  # only Bob, on the 4 records that existed
    assert ("102", None) in fake.assoc("meetings", st["meeting_id"])
    new_task = res["state"]["task_ids"][2]
    assert fake.assoc("tasks", new_task) == {
        ("101", 204),
        ("102", 204),
        ("103", 204),
        ("900", 192),
    }
    assert res["state"]["task_ids"][:2] == st["task_ids"]

    # A meeting deleted in HubSpot is made again instead of failing.
    fake.objects["meetings"].clear()
    res = client.post(f"/api/sessions/{s.id}/crm/hubspot", json={"contact_ids": ["101"]}).json()
    assert res["state"]["meeting_id"] != st["meeting_id"]
    assert len(fake.objects["meetings"]) == 1


def test_push_errors_are_readable(client, ctx):
    s = _session(ctx)
    url = f"/api/sessions/{s.id}/crm/hubspot"
    r = client.post(url, json={"contact_ids": ["101"]})
    assert r.status_code == 400 and "access token" in r.json()["detail"]
    fake = _setup(ctx)
    assert client.post(url, json={"contact_ids": []}).status_code == 400
    assert (
        client.post("/api/sessions/nope/crm/hubspot", json={"contact_ids": ["1"]}).status_code
        == 404
    )
    r = client.post(url, json={"contact_ids": ["999"]})
    assert r.status_code == 502 and r.json()["detail"] == "HubSpot has no contact 999"
    ctx.settings.hubspot_token = "expired"
    r = client.post(url, json={"contact_ids": ["101"]})
    assert r.status_code == 502 and "rejected the access token" in r.json()["detail"]
    ctx.settings.hubspot_token = TOKEN
    fake.fail = 429
    r = client.post(url, json={"contact_ids": ["101"]})
    assert r.status_code == 502 and "rate limit" in r.json()["detail"]
    fake.fail = None
    fake.owners = []
    r = client.post(url, json={"contact_ids": ["101"]})
    assert "No HubSpot user with the email advisor@firm.com" in r.json()["detail"]
    kinds = [e["kind"] for e in ctx.repo.events(s.id)]
    assert kinds.count("hubspot_failed") == 4
    # nothing was written
    assert fake.writes() == []
    empty = ctx.repo.save(Session(name="Empty"))
    r = client.post(f"/api/sessions/{empty.id}/crm/hubspot", json={"contact_ids": ["101"]})
    assert r.status_code == 400 and r.json()["detail"] == "Summarize the meeting first"


def test_auto_push_after_summary(client, ctx, fake_provider, transcribed_session):
    sid = transcribed_session["id"]
    fake = _setup(ctx)
    ctx.settings.hubspot_auto_push = True
    # no confirmed contacts yet: nothing is sent
    job = run_summarize(client, sid, {"provider": "fake"})
    assert job["status"] == "completed" and job["result"]["hubspot"] is None
    assert fake.requests == []

    r = client.post(f"/api/sessions/{sid}/crm/hubspot", json={"contact_ids": ["101"]})
    assert r.status_code == 200
    meeting_id = r.json()["state"]["meeting_id"]
    fake.requests.clear()
    job = run_summarize(client, sid, {"provider": "fake"})
    assert job["result"]["hubspot"].startswith("Updated in HubSpot for Jane Client")
    assert [r.url.path for r in fake.writes()] == [f"/crm/v3/objects/meetings/{meeting_id}"]
    events = [e for e in ctx.repo.events(sid) if e["kind"] == "hubspot_pushed"]
    assert events[-1]["detail"]["auto"] is True

    # HubSpot failing never fails the summary; the failure is in the history
    fake.fail = 500
    job = run_summarize(client, sid, {"provider": "fake"})
    assert job["status"] == "completed"
    assert job["result"]["hubspot"].startswith("HubSpot: HubSpot 500")
    assert ctx.repo.events(sid)[-1]["kind"] == "hubspot_failed"

    # off: nothing is sent even with confirmed contacts
    ctx.settings.hubspot_auto_push = False
    fake.requests.clear()
    job = run_summarize(client, sid, {"provider": "fake"})
    assert job["result"]["hubspot"] is None and fake.requests == []


def test_markdown_html():
    assert hubspot.markdown_html("Para *one*\n\n1. a\n2. b\ntext") == (
        "<p>Para <em>one</em></p><ul><li>a</li><li>b</li></ul><p>text</p>"
    )


def test_identifiers_are_masked_before_they_reach_hubspot(client, ctx):
    fake = _setup(ctx)
    s = _session(ctx)
    s.summary = "## Overview\n- Her social is 123-45-6789, account 4401 2233 8"
    s.summary_data.decisions = ["Move account 44012233 to the new custodian"]
    s.summary_data.action_items[0].text = "Call about card 4242 4242 4242 4242"
    ctx.repo.save(s)
    r = client.post(f"/api/sessions/{s.id}/crm/hubspot", json={"contact_ids": ["101"]})
    assert r.status_code == 200, r.text
    sent = " ".join(
        json.dumps(json.loads(req.content), ensure_ascii=False) for req in fake.writes()
    )
    for secret in ("123-45-6789", "44012233", "4242 4242 4242 4242"):
        assert secret not in sent
    assert "[SSN]" in sent and "[card ••4242]" in sent
    ctx.settings.redact_exports = False  # the firm's choice
    client.post(f"/api/sessions/{s.id}/crm/hubspot", json={"contact_ids": ["101"]})
    assert "123-45-6789" in " ".join(req.content.decode() for req in fake.writes())
