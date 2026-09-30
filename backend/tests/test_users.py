"""People on a firm's server: invites and tokens, and who sees and changes which meetings."""

import os

import pytest
from fastapi.testclient import TestClient

from mnemosyne import access
from mnemosyne.api.app import create_app
from mnemosyne.models.session import Session
from mnemosyne.models.transcript import TranscriptSegment
from mnemosyne.services.users import InvalidInvite, UserService

# ---- the user store ------------------------------------------------------------------------


def test_invite_redeem_verify_and_sign_out(tmp_path):
    clock = [1000.0]
    users = UserService(tmp_path / "users.json", clock=lambda: clock[0])
    pat = users.add("Pat", "Pat@Firm.test", "admin")
    assert pat.email == "pat@firm.test"
    code, _ = users.invite(pat.id)
    user, token = users.redeem(code, "Laptop")
    assert user.id == pat.id and users.verify(token).id == pat.id
    with pytest.raises(InvalidInvite):
        users.redeem(code, "again")  # once only
    assert oct(os.stat(tmp_path / "users.json").st_mode & 0o777) == "0o600"
    assert token not in (tmp_path / "users.json").read_text()  # hashes only
    users.forget_token(token)
    assert users.verify(token) is None


def test_invites_expire_and_disabled_people_are_out(tmp_path):
    clock = [1000.0]
    users = UserService(tmp_path / "users.json", clock=lambda: clock[0])
    users.add("Admin", "", "admin")
    sam = users.add("Sam", "sam@firm.test", "advisor")
    old, _ = users.invite(sam.id)
    clock[0] += 8 * 24 * 3600
    with pytest.raises(InvalidInvite):
        users.redeem(old, "x")
    code, _ = users.invite(sam.id)
    _, token = users.redeem(code, "x")
    users.update(sam.id, disabled=True)
    assert users.verify(token) is None
    users.update(sam.id, disabled=False)
    assert users.verify(token) is not None
    users.sign_out(sam.id)
    assert users.verify(token) is None


def test_the_last_admin_cannot_be_removed(tmp_path):
    users = UserService(tmp_path / "users.json")
    pat = users.add("Pat", "", "admin")
    with pytest.raises(ValueError):
        users.update(pat.id, role="advisor")
    with pytest.raises(ValueError):
        users.update(pat.id, disabled=True)
    assert users.get(pat.id).role == "admin"
    with pytest.raises(ValueError):
        users.add("Twin", "", "boss")
    users.add("Sam", "sam@x.test", "advisor")
    with pytest.raises(ValueError):
        users.add("Sam again", "SAM@x.test", "advisor")


def test_changes_from_another_process_are_picked_up(tmp_path):
    server = UserService(tmp_path / "users.json")
    cli = UserService(tmp_path / "users.json")
    added = cli.add("First Admin", "", "admin")
    os.utime(tmp_path / "users.json", (1, 1))  # a different mtime, as another writer leaves
    assert server.get(added.id) is not None


# ---- the API -------------------------------------------------------------------------------


@pytest.fixture
def firm(settings, keystore):
    settings.firm_mode = True
    app = create_app(settings, keystore=keystore)
    ctx = app.state.ctx
    people = {}
    for name, role in [
        ("Admin", "admin"),
        ("Ann", "advisor"),
        ("Bob", "advisor"),
        ("Rev", "reviewer"),
    ]:
        user = ctx.users.add(name, f"{name.lower()}@firm.test", role)
        code, _ = ctx.users.invite(user.id)
        people[name] = (user, ctx.users.redeem(code, "test")[1])
    with TestClient(app) as client:
        yield client, ctx, people


def _h(people, name):
    return {"Authorization": f"Bearer {people[name][1]}"}


def _meeting(ctx, people, owner, name, text="the quarterly rebalance"):
    user = people[owner][0]
    reset = access.current.set(access.Principal(user.id, user.name, user.role))
    try:
        session = Session(
            name=name,
            transcript=[TranscriptSegment(text=text, speaker="Client", start=0, end=2)],
        )
        return ctx.repo.save(session)
    finally:
        access.current.reset(reset)


def test_firm_mode_needs_someone_signed_in(firm):
    client, _, people = firm
    assert client.get("/api/sessions").status_code == 401
    assert client.get("/health").json()["auth_required"] is True
    assert client.get("/api/users/me", headers=_h(people, "Ann")).json()["role"] == "advisor"
    assert client.get("/api/users/me", headers=_h(people, "Ann")).json()["name"] == "Ann"


def test_an_invite_link_signs_a_browser_in(firm):
    client, ctx, people = firm
    code, _ = ctx.users.invite(people["Bob"][0].id)
    r = client.post("/api/users/redeem", json={"code": code, "device": "Chrome"})
    assert r.status_code == 200
    token = r.json()["token"]
    me = client.get("/api/users/me", headers={"Authorization": f"Bearer {token}"}).json()
    assert me["name"] == "Bob"
    assert client.post("/api/users/redeem", json={"code": code}).status_code == 400
    out = client.post("/api/users/me/signout", headers={"Authorization": f"Bearer {token}"})
    assert out.status_code == 200
    assert (
        client.get("/api/users/me", headers={"Authorization": f"Bearer {token}"}).status_code == 401
    )


def test_meetings_belong_to_whoever_creates_them(firm):
    client, _, people = firm
    created = client.post("/api/sessions", json={"name": "Ann's"}, headers=_h(people, "Ann"))
    assert created.json()["owner_id"] == people["Ann"][0].id


def test_advisors_see_only_their_own_meetings(firm):
    client, ctx, people = firm
    a = _meeting(ctx, people, "Ann", "Ann's client", "the Roth conversion for Dana")
    b = _meeting(ctx, people, "Bob", "Bob's client", "Bob talks about the Roth ladder")

    def ids(name):
        return {s["id"] for s in client.get("/api/sessions", headers=_h(people, name)).json()}

    assert ids("Ann") == {a.id} and ids("Bob") == {b.id}
    assert ids("Rev") == ids("Admin") == {a.id, b.id}
    assert client.get(f"/api/sessions/{a.id}", headers=_h(people, "Bob")).status_code == 404
    assert client.get(f"/api/audio/file/{a.id}", headers=_h(people, "Bob")).status_code == 404
    hits = client.get("/api/search", params={"q": "Roth"}, headers=_h(people, "Bob")).json()
    assert [h["session_id"] for h in hits] == [b.id]
    # Changing someone else's meeting: not found, as if it did not exist.
    r = client.patch(f"/api/sessions/{a.id}", json={"name": "x"}, headers=_h(people, "Bob"))
    assert r.status_code == 404
    assert client.delete(f"/api/sessions/{a.id}", headers=_h(people, "Bob")).status_code == 404
    assert ctx.repo.get(a.id).name == "Ann's client"


def test_a_reviewer_reads_everything_and_changes_nothing_of_others(firm):
    client, ctx, people = firm
    a = _meeting(ctx, people, "Ann", "Ann's client")
    assert client.get(f"/api/sessions/{a.id}", headers=_h(people, "Rev")).status_code == 200
    r = client.patch(f"/api/sessions/{a.id}", json={"name": "x"}, headers=_h(people, "Rev"))
    assert r.status_code == 403
    assert client.delete(f"/api/sessions/{a.id}", headers=_h(people, "Rev")).status_code == 403
    assert (
        client.patch(
            f"/api/sessions/{a.id}", json={"name": "renamed"}, headers=_h(people, "Admin")
        ).json()["name"]
        == "renamed"
    )


def test_opening_a_meeting_is_in_its_history(firm):
    client, ctx, people = firm
    a = _meeting(ctx, people, "Ann", "Ann's client")
    for _ in range(3):
        client.get(f"/api/sessions/{a.id}", headers=_h(people, "Rev"))
    viewed = [e for e in ctx.repo.events(a.id) if e["kind"] == "viewed"]
    assert len(viewed) == 1  # once per half hour, not per request
    assert viewed[0]["detail"]["by"] == "Rev" and viewed[0]["detail"]["role"] == "reviewer"


def test_only_admins_change_settings_and_people(firm):
    client, _, people = firm
    for name in ("Ann", "Rev"):
        h = _h(people, name)
        assert client.put("/api/settings", json={"language": "fr"}, headers=h).status_code == 403
        assert client.get("/api/storage", headers=h).status_code == 403
        assert client.post("/api/users", json={"name": "Eve"}, headers=h).status_code == 403
        assert client.post("/api/pairing/codes", json={}, headers=h).status_code == 403
        assert client.get("/api/settings", headers=h).status_code == 200  # reading is fine
    h = _h(people, "Admin")
    assert client.put("/api/settings", json={"language": "fr"}, headers=h).status_code == 200
    added = client.post("/api/users", json={"name": "Eve", "role": "advisor"}, headers=h).json()
    assert added["code"] and added["user"]["name"] == "Eve"
    listed = client.get("/api/users", headers=_h(people, "Ann")).json()
    assert {u["name"] for u in listed} >= {"Ann", "Bob", "Eve"}
    assert all(u["email"] == "" and u["devices"] == [] for u in listed)  # details: admins


def test_jobs_are_visible_to_their_starter_and_the_meetings_owner(firm):
    client, ctx, people = firm
    a = _meeting(ctx, people, "Ann", "Ann's client")
    ann = people["Ann"][0]

    async def noop(job_ctx):
        return {"answer": "private"}

    reset = access.current.set(access.Principal(ann.id, ann.name, ann.role))
    try:
        import asyncio

        async def submit():
            return ctx.jobs.submit("ask", noop), ctx.jobs.submit("summarize", noop, a.id)

        loop = asyncio.new_event_loop()
        ask, summarize = loop.run_until_complete(submit())
        loop.close()
    finally:
        access.current.reset(reset)
    seen = {j["id"] for j in client.get("/api/jobs", headers=_h(people, "Bob")).json()}
    assert ask.id not in seen and summarize.id not in seen
    assert client.get(f"/api/jobs/{ask.id}", headers=_h(people, "Bob")).status_code == 404
    mine = {j["id"] for j in client.get("/api/jobs", headers=_h(people, "Ann")).json()}
    assert {ask.id, summarize.id} <= mine


def test_the_event_stream_keeps_other_advisors_meetings_out(firm):
    client, ctx, people = firm
    a = _meeting(ctx, people, "Ann", "Ann's client")
    b = _meeting(ctx, people, "Bob", "Bob's client")
    token = people["Bob"][1]
    with client.websocket_connect(f"/ws?token={token}") as ws:
        assert ws.receive_json()["type"] == "hello"
        ctx.bus.publish({"type": "live_segment", "session_id": a.id, "segment": {"text": "secret"}})
        ctx.bus.publish({"type": "meeting_app", "status": "started", "app": "zoom"})
        ctx.bus.publish({"type": "live_segment", "session_id": b.id, "segment": {"text": "mine"}})
        assert ws.receive_json()["type"] == "meeting_app"
        got = ws.receive_json()
        assert got["session_id"] == b.id and got["segment"]["text"] == "mine"


def test_saved_questions_and_digests_are_personal(firm):
    from datetime import date

    from mnemosyne.models.ask import Ask
    from mnemosyne.models.digest import Digest

    client, ctx, people = firm
    for name in ("Ann", "Bob"):
        user = people[name][0]
        reset = access.current.set(access.Principal(user.id, user.name, user.role))
        try:
            ctx.repo.save_ask(Ask(question=f"{name}?", answer="a"))
            ctx.repo.save_digest(
                Digest(
                    label="2026-W40",
                    start=date(2026, 9, 28),
                    end=date(2026, 10, 4),
                    markdown=f"{name}'s week",
                )
            )
        finally:
            access.current.reset(reset)
    asks = client.get("/api/asks", headers=_h(people, "Ann")).json()
    assert [a["question"] for a in asks] == ["Ann?"]
    digests = client.get("/api/digests", headers=_h(people, "Bob")).json()
    assert [d["markdown"] for d in digests] == ["Bob's week"]  # same week, both kept
