"""A computer that hosts a team stamps its owner on the meetings unrestricted callers make
(docs/plans/2026-11-09-team-hosting.md, 2.5): the API token, a paired laptop's hand-off or
stream, automation. A plain computer leaves them unowned; a contact's meeting is never owned;
turning hosting on adopts what has no owner and is refused while another admin or a main
computer is in the way."""

import socket
from datetime import datetime

import httpx
import pytest
from fastapi.testclient import TestClient

from mnemosyne import access
from mnemosyne.api.app import create_app
from mnemosyne.models.session import Session, SummaryData
from mnemosyne.models.shared_meeting import SharedMeeting, SharedOwner, ShareParts
from mnemosyne.storage.crypto import MemoryKeyStore
from tests.test_handoff import finish, handoff_form, meta

pytestmark = pytest.mark.anyio

LOCAL = "http://127.0.0.1:8008"
LOOPBACK = ("127.0.0.1", 50000)
TUNNEL = ("127.0.0.2", 50000)
HOST = "u-host"
ADA = "a" * 64


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def client(app, token: str | None = None, peer=LOOPBACK):
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app, client=peer), base_url=LOCAL, headers=headers
    )


def owner_of(ctx, sid: str) -> str:
    with access.internally():
        return ctx.repo.get(sid).owner_id


def shared_meeting(sid: str = "ada000000001") -> SharedMeeting:
    now = datetime(2026, 10, 20, 14, 0)
    return SharedMeeting(
        id=sid,
        name="Ada's roadmap",
        created_at=now,
        updated_at=now,
        owner=SharedOwner(person=ADA, name="Ada"),
        parts=ShareParts(transcript=False, notes=False),
        summary="x",
        summary_data=SummaryData(),
        attendees=["Ada"],
    )


@pytest.fixture
def hosting(settings):
    """A main computer that hosts a team: the team_mode / team_owner_id pair turn_on saves."""
    settings.team_mode = True
    settings.team_owner_id = HOST
    settings.api_token = "admin-main"
    settings.live_transcription = False
    settings.copilot = False
    settings.auto_transcribe = False
    return settings


@pytest.fixture
def desktop(settings, keystore):
    """The desktop app's backend on a plain computer: requests from 127.0.0.1, as the app makes
    them (tests/test_team_host.py)."""
    settings.team_port = _free_port()
    settings.api_token = ""
    app = create_app(settings, keystore=keystore)
    with TestClient(app, base_url=LOCAL, client=LOOPBACK) as c:
        yield c, app.state.ctx


# ---- the default owner ---------------------------------------------------------------------


def test_a_plain_computer_leaves_unrestricted_meetings_unowned(ctx):
    assert ctx.repo.host_owner == ""
    assert ctx.sessions.create_session().owner_id == ""


def test_unrestricted_callers_on_a_host_own_what_they_make(hosting, keystore):
    host_ctx = create_app(hosting, keystore=keystore).state.ctx
    assert host_ctx.repo.host_owner == HOST
    assert host_ctx.sessions.create_session().owner_id == HOST
    # A meeting that names its owner keeps it, and a member's request is the member's.
    assert host_ctx.repo.save(Session(name="Sam's", owner_id="u-sam")).owner_id == "u-sam"
    reset = access.current.set(access.Principal("u-sam", "Sam", access.MEMBER))
    try:
        assert host_ctx.repo.save(Session(name="Sam again")).owner_id == "u-sam"
    finally:
        access.current.reset(reset)
    # Once made, a meeting's owner does not move.
    mine = host_ctx.sessions.create_session()
    host_ctx.repo.host_owner = "someone-else"
    assert host_ctx.repo.save(mine).owner_id == HOST


async def test_the_api_token_makes_meetings_for_the_owner(hosting):
    app = create_app(hosting, keystore=MemoryKeyStore())
    async with client(app, "admin-main") as c:
        r = await c.post("/api/sessions", json={"name": "From a script"})
    assert r.status_code == 200, r.text
    assert r.json()["owner_id"] == HOST


async def test_a_streamed_meeting_is_the_owners(hosting, keystore, fake_pipewire):
    host = create_app(hosting, keystore=keystore)
    hctx = host.state.ctx
    _, token = hctx.pairing.add_desktop("Corey's laptop", "e" * 64)
    async with client(host, token, peer=TUNNEL) as laptop:
        r = await laptop.post(
            "/api/audio/start-browser", json={"sources": ["mic", "system"], "sample_rate": 16000}
        )
        assert r.status_code == 200, r.text
        sid = r.json()["session_id"]
        r = await laptop.post(f"/api/audio/stop/{sid}", json={"transcribe": False})
        assert r.status_code == 200, r.text
    await finish(hctx, sid)
    assert owner_of(hctx, sid) == HOST


async def test_a_handoff_is_the_owners(hosting, keystore, fake_pipewire):
    host = create_app(hosting, keystore=keystore)
    hctx = host.state.ctx
    _, token = hctx.pairing.add_desktop("Corey's laptop", "e" * 64)
    async with client(host, token, peer=TUNNEL) as laptop:
        r = await laptop.post("/api/connections/handoff", **handoff_form(meta()))
    assert r.status_code == 200, r.text
    sid = r.json()["session_id"]
    await finish(hctx, sid)
    assert owner_of(hctx, sid) == HOST


# ---- a contact's meeting -------------------------------------------------------------------


def test_a_contacts_meeting_is_never_owned(ctx):
    ctx.repo.host_owner = HOST
    sm = shared_meeting()
    assert ctx.repo.apply_shared(sm, ADA) == "new"
    assert owner_of(ctx, sm.id) == ""
    # A later write of the meeting through save() keeps it unowned.
    with access.internally():
        copy = ctx.repo.get(sm.id)
        copy.notes = "mine"
        ctx.repo.save(copy)
    assert owner_of(ctx, sm.id) == ""
    assert ctx.repo.apply_shared(sm.model_copy(update={"summary": "y"}), ADA) == "updated"
    assert owner_of(ctx, sm.id) == ""


def test_adopting_skips_a_contacts_meeting(ctx):
    mine = ctx.sessions.create_session().id
    sm = shared_meeting()
    ctx.repo.apply_shared(sm, ADA)
    assert ctx.repo.adopt_unowned(HOST) == 1
    assert owner_of(ctx, mine) == HOST and owner_of(ctx, sm.id) == ""
    assert ctx.repo.adopt_unowned(HOST) == 0


# ---- startup -------------------------------------------------------------------------------


def test_starting_as_a_host_adopts_meetings_that_have_no_owner(settings, keystore):
    plain = create_app(settings, keystore=keystore).state.ctx
    stray = plain.sessions.create_session("From an older peer").id
    plain.repo.apply_shared(shared_meeting(), ADA)
    taken = plain.sessions.create_session("Sam's").id
    plain.repo._conn.execute("UPDATE sessions SET owner_id='u-sam' WHERE id=?", (taken,))
    plain.repo._conn.commit()

    settings.team_mode, settings.team_owner_id = True, HOST
    host = create_app(settings, keystore=keystore).state.ctx
    assert owner_of(host, stray) == HOST
    assert owner_of(host, "ada000000001") == ""
    assert owner_of(host, taken) == "u-sam"

    # Turned off (the owner is kept, team mode is not): nothing is stamped or adopted.
    settings.team_mode = False
    off = create_app(settings, keystore=keystore).state.ctx
    assert off.repo.host_owner == ""
    assert off.sessions.create_session().owner_id == ""


# ---- turning hosting on and off ------------------------------------------------------------


def test_hosting_follows_the_settings_being_turned_on_and_off(desktop):
    c, ctx = desktop
    before = ctx.sessions.create_session("Before").id
    assert c.put("/api/team", json={"enabled": True, "name": "Corey"}).status_code == 200
    host = ctx.settings.team_owner_id
    assert ctx.repo.host_owner == host
    assert owner_of(ctx, before) == host
    assert ctx.sessions.create_session().owner_id == host

    assert c.put("/api/team", json={"enabled": False}).status_code == 200
    assert ctx.repo.host_owner == ""
    stray = ctx.sessions.create_session("While off").id
    assert owner_of(ctx, stray) == ""

    r = c.put("/api/team", json={"enabled": True})
    assert r.status_code == 200, r.text
    assert ctx.settings.team_owner_id == host  # the same person
    assert ctx.repo.host_owner == host
    assert owner_of(ctx, stray) == host  # adopted again
    assert ctx.sessions.create_session().owner_id == host


def test_hosting_is_refused_while_another_admin_exists(desktop):
    c, ctx = desktop
    ann = ctx.users.add("Ann", "", access.ADMIN)
    ctx.users.add("Bo", "", access.ADMIN)
    ctx.users.add("Rae", "", access.REVIEWER)
    gone = ctx.users.add("Gone", "", access.ADMIN)
    ctx.users.update(gone.id, disabled=True)  # a disabled admin is no one to worry about
    keep = ctx.sessions.create_session().id
    r = c.put("/api/team", json={"enabled": True, "name": "Corey"})
    assert r.status_code == 409
    detail = r.json()["detail"]
    assert detail.startswith("Make the other admins reviewers first: Ann, Bo")
    assert "mnemosyne-backend users set-role" in detail  # Ann and Bo are the only admins
    assert ctx.settings.team_mode is False and ctx.settings.team_owner_id == ""
    assert ctx.repo.host_owner == "" and owner_of(ctx, keep) == ""
    assert [u.name for u in ctx.users.list() if u.role == access.ADMIN] == ["Ann", "Bo", "Gone"]

    # Made reviewers first (nothing demotes them for the owner), it goes through. The last
    # admin cannot be demoted through update(), so the file is edited here.
    bo = next(u for u in ctx.users.list() if u.name == "Bo")
    ctx.users.update(bo.id, role=access.REVIEWER)
    ctx.users.get(ann.id).role = access.REVIEWER
    ctx.users._save()
    assert c.put("/api/team", json={"enabled": True, "name": "Corey"}).status_code == 200


def test_the_owner_being_the_only_admin_is_fine_again_after_off_and_on(desktop):
    c, ctx = desktop
    assert c.put("/api/team", json={"enabled": True, "name": "Corey"}).status_code == 200
    assert c.put("/api/team", json={"enabled": False}).status_code == 200
    # The owner is still the one admin: no refusal on the way back.
    assert c.put("/api/team", json={"enabled": True}).status_code == 200


def test_hosting_is_refused_while_this_computer_uses_a_main_computer(desktop):
    c, ctx = desktop
    ctx.main_computer_store().set("http://127.0.0.1:8009", "tok", "Desktop")
    keep = ctx.sessions.create_session().id
    r = c.put("/api/team", json={"enabled": True, "name": "Corey"})
    assert r.status_code == 409
    assert r.json()["detail"] == "This computer uses another main computer; it cannot host a team"
    assert ctx.settings.team_mode is False and ctx.users.list() == []
    assert owner_of(ctx, keep) == ""
    ctx.main_computer_store().clear()
    assert c.put("/api/team", json={"enabled": True, "name": "Corey"}).status_code == 200
