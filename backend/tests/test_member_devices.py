"""A team member's app on a computer that hosts a team (docs/plans/2026-11-10-team-members-in-
the-app.md 1.1, 1.6): a MEMBER device, redeemed with a user's invite through the sidecar,
runs as that person on the tunnel and is never the owner.

Covers M1 (tunnel only), M2 (never an owner channel), M3 (reads what the person reads), M5
(revocation), M6 (redeeming an invite), M7 (the event stream). M4 (hand-offs and streams) is the
last section.

The machine and its callers come from test_hosting_access.py (`make_machine`, the owner-only
route list); the meetings from test_hosting_visibility.py (`host`). A tunnelled request is one
served by the tunnel listener, which sets `scope["tunnel"]` (`Tunnelled`), or one from 127.0.0.2
(an older sidecar): both are tried.
"""

# ruff: noqa: F811 - fixtures imported from the hosting tests are used as arguments
from __future__ import annotations

import json

import httpx
import pytest
from fastapi.testclient import TestClient

from mnemosyne import access
from mnemosyne.api import auth
from mnemosyne.api.websocket import TEAM_EVENTS, visibility
from mnemosyne.services.pairing import MEMBER, PairingService
from mnemosyne.services.tunnel_listener import Tunnelled
from tests.test_hosting_access import (  # noqa: F401 - make_machine is a fixture
    CALLERS,
    LAN,
    MAIN,
    OWNER_ONLY,
    PERSONAL_ROUTES,
    TEAM_PORT,
    Machine,
    _owner_refusal,
    _send,
    make_machine,
)
from tests.test_hosting_visibility import (  # noqa: F401 - host is a fixture
    LISTINGS,
    SEES,
    SESSIONLESS,
    _found,
    host,
    marker,
)

EP_SAM, EP_RAE, EP_OWNER = "a1" * 32, "a2" * 32, "a3" * 32
INVALID = "This pairing code is invalid or has expired: show a new code in Settings"


def app_device(m: Machine, who: str, endpoint: str) -> str:
    """A MEMBER device for the person called `who`; its token."""
    user = next(u for u in m.ctx.users.list() if u.name == who)
    _, token = m.ctx.pairing.add_member(f"{who}'s Mac", endpoint, user.id)
    return token


def via(m: Machine, how: str, token: str | None = None) -> tuple[TestClient, dict]:
    """A client for the way into the machine: the tunnel listener's mark, an older sidecar's
    127.0.0.2, the main port from this machine, the team listener from the LAN or from here."""
    app, base, addr = {
        "tunnel": (Tunnelled(m.app), MAIN, "127.0.0.1"),
        "legacy": (m.app, MAIN, "127.0.0.2"),
        "main": (m.app, MAIN, "127.0.0.1"),
        "team": (m.app, f"http://{LAN}:{TEAM_PORT}", LAN),
        "team_loopback": (m.app, f"http://127.0.0.1:{TEAM_PORT}", "127.0.0.1"),
        "lan": (m.app, MAIN, LAN),
    }[how]
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    return TestClient(app, base_url=base, client=(addr, 50000)), headers


@pytest.fixture
def apps(host, monkeypatch):  # noqa: F811
    """The host with Sam's (a member), Rae's (a reviewer) and the owner's apps paired."""
    tokens = {
        "member_app": app_device(host, "Sam", EP_SAM),
        "reviewer_app": app_device(host, "Rae", EP_RAE),
        "owner_app": app_device(host, "Corey", EP_OWNER),
    }
    host.tokens.update(tokens)
    for name in tokens:  # the sweep helpers (`_send`, `Machine.probe`) look callers up here
        monkeypatch.setitem(CALLERS, name, (MAIN, "127.0.0.2", name, {}))
    return host


# ---- M1: only through the tunnel ---------------------------------------------------------------


@pytest.mark.parametrize("how", ["tunnel", "legacy"])
def test_a_member_device_is_let_in_through_the_tunnel(apps, how):
    client, headers = via(apps, how, apps.tokens["member_app"])
    r = client.get("/api/_t/channel", headers=headers)
    assert r.status_code == 200, r.text
    sam = apps.users["Sam"]
    assert r.json()["channel"] == auth.MEMBER_DEVICE == "member"
    assert r.json()["user_id"] == sam
    device = apps.ctx.pairing.members_for(sam)[0]
    assert r.json()["device_id"] == device.id
    who = client.get("/api/users/me", headers=headers).json()
    assert (who["id"], who["role"], who["owner"]) == (sam, "member", False)


@pytest.mark.parametrize("how", ["main", "team", "team_loopback", "lan"])
@pytest.mark.parametrize("who", ["member_app", "reviewer_app", "owner_app"])
def test_a_member_device_is_refused_everywhere_else(apps, how, who):
    client, headers = via(apps, how, apps.tokens[who])
    for path in ("/api/_t/channel", "/api/sessions", "/api/users/me"):
        assert client.get(path, headers=headers).status_code == 401, (how, path)
    with pytest.raises(Exception):  # noqa: B017 - the socket is closed 4401 on accept
        with client.websocket_connect("/ws", headers=headers):
            pass


def test_a_member_device_is_never_downgraded_to_a_phone_or_a_computer(apps):
    """Off the tunnel the token is not a phone's on a DEVICE_PATH (`/api/pairing/me`), nor a
    computer's anywhere."""
    client, headers = via(apps, "main", apps.tokens["member_app"])
    assert client.get("/api/pairing/me", headers=headers).status_code == 401
    assert client.delete("/api/pairing/me", headers=headers).status_code == 401
    assert apps.ctx.pairing.members_for(apps.users["Sam"])  # still there


def test_a_member_device_needs_the_computer_to_host_a_team(apps):
    """With hosting off every caller reads as the owner (`is_owner_channel`): an app member
    must not, so the token is refused until hosting is on again."""
    client, headers = via(apps, "tunnel", apps.tokens["member_app"])
    assert client.get("/api/sessions", headers=headers).status_code == 200
    apps.ctx.settings.team_mode = False
    assert client.get("/api/sessions", headers=headers).status_code == 401
    apps.ctx.settings.team_mode = True
    assert client.get("/api/sessions", headers=headers).status_code == 200


def test_the_other_kinds_of_token_do_not_become_members(apps):
    """A member's token is not a user's: it is not found among users.json tokens, and a user's
    browser token is still a user's (channel `user`)."""
    assert apps.ctx.users.verify(apps.tokens["member_app"]) is None
    assert apps.probe("member").channel == auth.USER


# ---- M2: never an owner channel ----------------------------------------------------------------


def test_the_member_channel_is_not_an_owner_channel():
    assert auth.MEMBER_DEVICE not in auth.OWNER_CHANNELS
    assert auth.MEMBER_DEVICE in auth.CHANNELS


@pytest.mark.parametrize("who", ["member_app", "reviewer_app", "owner_app"])
def test_the_member_channel_is_never_the_owner_even_for_the_owners_own_user(apps, who):
    p = apps.probe(who)
    assert p.channel == "member", p
    assert p.owner_status == 403
    # the state that made the owner's USER token the owner is all there, and still not enough
    owner = apps.ctx.settings.team_owner_id
    state = {"channel": auth.MEMBER_DEVICE, "user_id": owner, "loopback_main": True}
    assert not auth.is_owner_channel(apps.ctx, state)
    assert not auth.is_owner_channel(apps.ctx, {**state, "on_team_port": False})


# The one personal route a member's app may use: it hands over its own recording (M4, below).
MEMBER_ROUTES = {("POST", "/api/connections/handoff")}
OWNER_ROUTES = [r for r in PERSONAL_ROUTES if r not in MEMBER_ROUTES]


@pytest.mark.parametrize("who", ["member_app", "reviewer_app", "owner_app"])
def test_every_personal_route_is_refused_to_a_member_device(apps, who):
    wrong = []
    for method, path in OWNER_ROUTES:
        r = _send(apps, who, method, path)
        if not _owner_refusal(r):
            wrong.append((method, path, r.status_code))
    assert not wrong


def test_a_member_device_is_refused_in_the_owners_words_whatever_the_route(apps):
    details = {
        (r.status_code, r.json()["detail"])
        for r in (_send(apps, "member_app", m, p) for m, p in OWNER_ROUTES)
    }
    assert details == {(403, OWNER_ONLY)}


@pytest.mark.parametrize("who", ["member_app", "reviewer_app"])
def test_the_admin_routes_are_refused_to_a_member_or_reviewer_device(apps, who):
    before = len(apps.ctx.users.list())
    for method, path, body in (
        ("POST", "/api/users", {"name": "Pat", "role": "member"}),
        ("PATCH", f"/api/users/{apps.users['Sam']}", {"role": "reviewer"}),
        ("POST", f"/api/users/{apps.users['Sam']}/invite", None),
        ("POST", f"/api/users/{apps.users['Sam']}/signout", None),
    ):
        client, headers, _ = apps.client(who)
        r = client.request(method, path, headers=headers, json=body)
        assert r.status_code == 403, (method, path, r.status_code)
    assert len(apps.ctx.users.list()) == before
    assert apps.ctx.users.get(apps.users["Sam"]).role == "member"


def test_a_member_device_cannot_remove_devices_or_others_apps(apps):
    client, headers, _ = apps.client("member_app")
    rae = apps.ctx.pairing.members_for(apps.users["Rae"])[0]
    r = client.delete(f"/api/pairing/devices/{rae.id}", headers=headers)
    assert _owner_refusal(r)
    assert apps.ctx.pairing.get(rae.id) is not None
    # it may leave the team itself, and only itself
    own = apps.ctx.pairing.members_for(apps.users["Sam"])[0]
    assert client.get("/api/pairing/me", headers=headers).json()["id"] == own.id
    assert client.delete("/api/pairing/me", headers=headers).status_code == 200
    assert apps.ctx.pairing.get(own.id) is None
    assert apps.ctx.pairing.get(rae.id) is not None
    assert client.get("/api/sessions", headers=headers).status_code == 401  # at once


def test_leaving_revokes_only_the_calling_device_of_a_person(apps):
    """Two apps of one person: one leaves, the other stays."""
    sam = apps.users["Sam"]
    second = app_device(apps, "Sam", "b4" * 32)
    assert len(apps.ctx.pairing.members_for(sam)) == 2
    client, headers = via(apps, "tunnel", second)
    assert client.delete("/api/pairing/me", headers=headers).status_code == 200
    left = apps.ctx.pairing.members_for(sam)
    assert [d.endpoint_id for d in left] == [EP_SAM]
    client, headers = via(apps, "tunnel", apps.tokens["member_app"])
    assert client.get("/api/sessions", headers=headers).status_code == 200


def test_only_a_member_device_can_use_the_leave_route(apps):
    """A phone's or a computer's token (which reach /api/pairing/me) leave nothing behind."""
    client, headers, _ = apps.client("device")
    assert client.delete("/api/pairing/me", headers=headers).status_code == 404
    assert any(d.kind == "desktop" for d in apps.ctx.pairing.devices())


# ---- M3: it reads what its person reads --------------------------------------------------------

PERSON = {"member_app": "member", "reviewer_app": "reviewer"}


@pytest.mark.parametrize("who", ["member_app", "reviewer_app"])
@pytest.mark.parametrize(("path", "params"), LISTINGS, ids=[p for p, _ in LISTINGS])
def test_a_member_device_lists_what_the_same_person_in_a_browser_does(apps, who, path, params):
    mine = apps.client(who)[0].get(path, headers=apps.client(who)[1], params=params)
    browser = apps.client(PERSON[who])[0].get(
        path, headers=apps.client(PERSON[who])[1], params=params
    )
    assert mine.status_code == browser.status_code == 200, mine.text
    assert _found(mine.text) == _found(browser.text)
    if path == "/api/sessions":
        assert _found(mine.text) == SEES[PERSON[who]]


@pytest.mark.parametrize("who", ["member_app", "reviewer_app"])
def test_a_meeting_one_may_not_read_is_not_there_for_a_member_device(apps, who):
    sees = SEES[PERSON[who]]
    client, headers, _ = apps.client(who)
    for key, sid in apps.ids.items():
        for path in (
            "/api/sessions/{id}",
            "/api/sessions/{id}/comments",
            "/api/sessions/{id}/stats",
        ):
            r = client.get(path.format(id=sid), headers=headers)
            if key in sees:
                assert r.status_code == 200, (key, path, r.text)
            else:
                assert r.status_code == 404, (key, path, r.status_code)
    # the three named in the plan: another member's, the owner's own, and a contact's
    for key in ("teamgrant" if who == "reviewer_app" else "", "own", "contact"):
        if key and key not in sees:
            sid = apps.ids[key]
            assert client.get(f"/api/sessions/{sid}", headers=headers).status_code == 404


def test_a_member_device_reads_only_its_own_meeting_and_its_grants(apps):
    client, headers, _ = apps.client("member_app")
    r = client.get("/api/sessions", headers=headers)
    assert _found(r.text) == {"team", "teamgrant", "everyone"}
    for key in ("own", "stray", "granted", "contact", "contactstar"):
        assert marker(key) not in r.text
        assert client.get(f"/api/sessions/{apps.ids[key]}", headers=headers).status_code == 404


def test_a_reviewers_app_reads_the_team_never_the_owners_unshared_meetings(apps):
    client, headers, _ = apps.client("reviewer_app")
    r = client.get("/api/sessions", headers=headers)
    found = _found(r.text)
    assert {"team", "teamgrant"} <= found
    assert not found & {"own", "stray", "contact", "contactstar"}
    rae = apps.ctx.users.get(apps.users["Rae"])
    reset = access.current.set(access.Principal(rae.id, rae.name, rae.role))
    try:
        assert apps.ctx.repo.read_scope().kind == access.TEAM_WIDE
    finally:
        access.current.reset(reset)


def test_a_member_device_only_reads_what_it_may_even_when_writing(apps):
    client, headers, _ = apps.client("member_app")
    for key in ("own", "granted", "contact"):
        r = client.patch(f"/api/sessions/{apps.ids[key]}", headers=headers, json={"name": "x"})
        assert r.status_code == 404, key
    r = client.patch(f"/api/sessions/{apps.ids['team']}", headers=headers, json={"name": "Mine"})
    assert r.status_code == 200


# ---- M5: revocation ----------------------------------------------------------------------------


def test_disabling_a_user_revokes_its_devices_and_the_token_dies_at_once(apps):
    sam = apps.users["Sam"]
    token = apps.tokens["member_app"]
    other = apps.tokens["reviewer_app"]
    client, headers = via(apps, "tunnel", token)
    assert client.get("/api/sessions", headers=headers).status_code == 200
    owner, oh, _ = apps.client("owner_main")
    r = owner.patch(f"/api/users/{sam}", headers=oh, json={"disabled": True})
    assert r.status_code == 200, r.text
    assert apps.ctx.pairing.members_for(sam) == []
    assert apps.ctx.pairing.verify(token) is None
    assert client.get("/api/sessions", headers=headers).status_code == 401
    # nobody else loses theirs
    rae_client, rae_headers = via(apps, "tunnel", other)
    assert rae_client.get("/api/sessions", headers=rae_headers).status_code == 200
    # the sidecar's file no longer lists the endpoint
    listed = json.loads(apps.ctx.pairing.path.read_text())["devices"]
    assert EP_SAM not in {d.get("endpoint_id") for d in listed}


def test_a_disabled_user_is_out_before_the_device_is_revoked(apps):
    """Every request looks the user up: disabled in users.json (the CLI, say) without the route
    touching the device file still ends the session."""
    sam = apps.users["Sam"]
    client, headers = via(apps, "tunnel", apps.tokens["member_app"])
    apps.ctx.users.update(sam, disabled=True)
    assert apps.ctx.pairing.members_for(sam)  # the device is still listed
    assert client.get("/api/sessions", headers=headers).status_code == 401
    with pytest.raises(Exception):  # noqa: B017
        with client.websocket_connect("/ws", headers=headers):
            pass
    apps.ctx.users.update(sam, disabled=False)  # and back, if the person is enabled again
    assert client.get("/api/sessions", headers=headers).status_code == 200


def test_a_removed_user_is_out(apps):
    client, headers = via(apps, "tunnel", apps.tokens["member_app"])
    apps.ctx.users._users.pop(apps.users["Sam"])
    assert client.get("/api/sessions", headers=headers).status_code == 401


def test_signing_a_person_out_everywhere_takes_their_apps_too(apps):
    sam = apps.users["Sam"]
    second = app_device(apps, "Sam", "b5" * 32)
    owner, oh, _ = apps.client("owner_main")
    r = owner.post(f"/api/users/{sam}/signout", headers=oh)
    assert r.status_code == 200
    assert apps.ctx.pairing.members_for(sam) == []
    for token in (apps.tokens["member_app"], second):
        assert apps.ctx.pairing.verify(token) is None
        client, headers = via(apps, "tunnel", token)
        assert client.get("/api/sessions", headers=headers).status_code == 401
    assert apps.ctx.pairing.members_for(apps.users["Rae"])  # not Rae's


def test_the_apps_are_listed_with_the_person(apps):
    owner, oh, _ = apps.client("owner_main")
    people = {u["name"]: u for u in owner.get("/api/users", headers=oh).json()}
    assert [d["device"] for d in people["Sam"]["devices"] if d["device"].endswith("(app)")] == [
        "Sam's Mac (app)"
    ]
    # a member sees no one's devices
    member, mh, _ = apps.client("member")
    assert all(u["devices"] == [] for u in member.get("/api/users", headers=mh).json())


def test_the_apps_are_people_not_devices(apps):
    from mnemosyne.api.routes.connections import connected_devices

    assert apps.ctx.pairing.members_for(apps.users["Sam"])
    names = {d.name for d in connected_devices(apps.ctx)}
    assert not {n for n in names if "Mac" in n}
    client, headers, _ = apps.client("api_token")
    listed = client.get("/api/connections/devices", headers=headers).json()
    assert not [d for d in listed if "Mac" in d["name"]]
    assert {"computer", "phone"} <= {d["kind"] for d in listed}


# ---- M6: redeeming an invite -------------------------------------------------------------------


def _redeem(m: Machine, how: str, **body):
    client, _ = via(m, how)
    return client.post("/api/pairing/redeem", json=body)


def _invite(m: Machine, who: str = "Sam") -> str:
    user = next(u for u in m.ctx.users.list() if u.name == who)
    return m.ctx.users.invite(user.id)[0]


def test_an_invite_is_redeemed_by_an_app_through_the_tunnel_and_used_up(host):
    code = _invite(host)
    browsers = len(host.ctx.users.get(host.users["Sam"]).tokens)  # the fixture signed one in
    r = _redeem(host, "tunnel", code=code, name="Sam's MacBook", endpoint_id=EP_SAM)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["device"]["kind"] == MEMBER and body["device"]["name"] == "Sam's MacBook"
    assert body["sync"] is None and body["token"]
    team = body["card"]["team"]
    assert team["user"] == {"id": host.users["Sam"], "name": "Sam", "role": "member"}
    assert team["consent_required"] is False and team["name"]
    device = host.ctx.pairing.members_for(host.users["Sam"])[0]
    assert device.endpoint_id == EP_SAM and device.user_id == host.users["Sam"]
    # the browser link is used up, and so is a second redeem; no browser token was made
    assert len(host.ctx.users.get(host.users["Sam"]).tokens) == browsers
    r = via(host, "main")[0].post("/api/users/redeem", json={"code": code})
    assert r.status_code == 400
    assert _redeem(host, "tunnel", code=code, name="x", endpoint_id="c1" * 32).status_code == 403
    # and the token works, as Sam, through the tunnel
    client, headers = via(host, "tunnel", body["token"])
    assert client.get("/api/users/me", headers=headers).json()["id"] == host.users["Sam"]


def test_the_browser_link_used_first_uses_the_code_up_for_the_app(host):
    code = _invite(host)
    r = via(host, "team")[0].post("/api/users/redeem", json={"code": code})
    assert r.status_code == 200
    r = _redeem(host, "tunnel", code=code, name="x", endpoint_id=EP_SAM)
    assert r.status_code == 403 and r.json()["detail"] == INVALID
    assert host.ctx.pairing.members_for(host.users["Sam"]) == []


def test_an_app_invite_needs_an_endpoint_and_is_not_used_up_without_one(host):
    code = _invite(host)
    r = _redeem(host, "tunnel", code=code, name="x")
    assert r.status_code == 400
    assert "through remote access" in r.json()["detail"]
    assert host.ctx.users.invited(code) is not None
    assert host.ctx.pairing.members_for(host.users["Sam"]) == []
    assert _redeem(host, "tunnel", code=code, name="x", endpoint_id=EP_SAM).status_code == 200


@pytest.mark.parametrize("kind", ["plain", "dedicated", "off"])
def test_only_a_computer_that_hosts_a_team_takes_app_members(make_machine, kind):  # noqa: F811
    m = make_machine(kind)
    code = _invite(m)
    r = _redeem(m, "tunnel", code=code, name="x", endpoint_id=EP_SAM)
    assert r.status_code == 409
    assert m.ctx.users.invited(code) is not None  # not used up
    assert not [d for d in m.ctx.pairing.devices() if d.kind == MEMBER]


def test_a_disabled_person_cannot_join(host):
    code = _invite(host)
    host.ctx.users.update(host.users["Sam"], disabled=True)
    r = _redeem(host, "tunnel", code=code, name="x", endpoint_id=EP_SAM)
    assert r.status_code == 403 and r.json()["detail"] == INVALID
    assert host.ctx.pairing.members_for(host.users["Sam"]) == []


def test_an_admin_who_is_not_the_owner_cannot_join_the_way_the_browser_cannot(host):
    code = _invite(host, "Alex")
    r = _redeem(host, "tunnel", code=code, name="x", endpoint_id=EP_SAM)
    assert r.status_code == 403 and r.json()["detail"] == INVALID
    # the owner may (another computer of theirs)
    owner_code = _invite(host, "Corey")
    r = _redeem(host, "tunnel", code=owner_code, name="Corey's Mac", endpoint_id=EP_OWNER)
    assert r.status_code == 200, r.text


def test_no_oracle_an_unknown_or_unusable_code_says_what_an_invalid_pairing_code_says(host):
    def answer(**body):
        r = _redeem(host, "tunnel", **body)
        return r.status_code, r.json()["detail"]

    unknown = answer(code="no-such-code", name="x", endpoint_id=EP_SAM)
    assert unknown == (403, INVALID)
    used = _invite(host)
    assert _redeem(host, "tunnel", code=used, name="x", endpoint_id=EP_SAM).status_code == 200
    assert answer(code=used, name="x", endpoint_id="c1" * 32) == unknown
    disabled = _invite(host, "Rae")
    host.ctx.users.update(host.users["Rae"], disabled=True)
    assert answer(code=disabled, name="x", endpoint_id=EP_RAE) == unknown
    nonowner_admin = _invite(host, "Alex")
    assert answer(code=nonowner_admin, name="x", endpoint_id=EP_RAE) == unknown
    # the phone-page route says the same for a code that does not exist
    assert (
        via(host, "main")[0]
        .post("/api/pairing/redeem", json={"code": "no-such-code", "name": "x"})
        .json()["detail"]
        == unknown[1]
    )


def test_one_person_per_machine_and_one_device_per_person_per_machine(host):
    sam, rae = host.users["Sam"], host.users["Rae"]
    first = _redeem(host, "tunnel", code=_invite(host), name="Old", endpoint_id=EP_SAM).json()
    # Sam joins again from the same machine: the earlier device and its token are replaced
    again = _redeem(host, "tunnel", code=_invite(host), name="New", endpoint_id=EP_SAM).json()
    devices = host.ctx.pairing.members_for(sam)
    assert [d.name for d in devices] == ["New"]
    assert host.ctx.pairing.verify(first["token"]) is None
    assert host.ctx.pairing.verify(again["token"]).user_id == sam
    # Rae on the same machine is refused, and her invite is not used up
    code = _invite(host, "Rae")
    r = _redeem(host, "tunnel", code=code, name="x", endpoint_id=EP_SAM)
    assert r.status_code == 409
    assert host.ctx.users.invited(code) is not None
    assert host.ctx.pairing.members_for(rae) == []
    assert [d.user_id for d in host.ctx.pairing.members_at(EP_SAM)] == [sam]
    # on a machine of her own she joins
    assert _redeem(host, "tunnel", code=code, name="x", endpoint_id=EP_RAE).status_code == 200


def test_several_machines_of_one_person_each_have_a_device(host):
    sam = host.users["Sam"]
    for ep in (EP_SAM, "b6" * 32):
        r = _redeem(host, "tunnel", code=_invite(host), name=f"m-{ep[:2]}", endpoint_id=ep)
        assert r.status_code == 200
    assert len(host.ctx.pairing.members_for(sam)) == 2


def test_the_consent_rule_travels_in_the_card(host):
    host.ctx.settings.require_consent = True
    r = _redeem(host, "tunnel", code=_invite(host), name="x", endpoint_id=EP_SAM)
    assert r.json()["card"]["team"]["consent_required"] is True


def test_the_other_codes_redeem_as_before(host):
    code, _ = host.ctx.pairing.new_code()
    r = via(host, "lan")[0].post("/api/pairing/redeem", json={"code": code, "name": "Phone 2"})
    assert r.status_code == 200 and r.json()["device"]["kind"] == "phone"


WANT_TEAM = {"want": "team"}  # what a member's app sends (services/team_member.py)


def test_an_app_that_wants_a_team_redeems_an_invite_with_its_card(host):
    r = _redeem(
        host, "tunnel", code=_invite(host), name="Sam's Mac", endpoint_id=EP_SAM, card=WANT_TEAM
    )
    assert r.status_code == 200, r.text
    assert r.json()["device"]["kind"] == MEMBER
    assert r.json()["card"]["team"]["user"]["id"] == host.users["Sam"]


@pytest.mark.parametrize("kind", ["desktop", "sync", "contact", "phone"])
def test_an_app_that_wants_a_team_never_uses_up_another_code(host, kind):
    """A pairing code pasted as a team invite: refused as invalid before anything is made, and
    still good for the pairing it was made for."""
    devices = {d.id for d in host.ctx.pairing.devices()}
    code, _ = host.ctx.pairing.new_code(kind)
    r = _redeem(host, "tunnel", code=code, name="Sam's Mac", endpoint_id=EP_SAM, card=WANT_TEAM)
    assert r.status_code == 403 and r.json()["detail"] == INVALID
    assert {d.id for d in host.ctx.pairing.devices()} == devices  # no device made
    assert host.ctx.pairing.code_kind(code) == kind  # not used up
    # and it still pairs what it was made for
    if kind == "desktop":
        r = _redeem(host, "tunnel", code=code, name="Laptop", endpoint_id="d9" * 32)
        assert r.status_code == 200 and r.json()["device"]["kind"] == "desktop"
    elif kind == "phone":
        r = _redeem(host, "lan", code=code, name="Phone 2")
        assert r.status_code == 200 and r.json()["device"]["kind"] == "phone"


def test_an_app_that_wants_a_team_is_refused_for_an_unknown_or_used_invite(host):
    code = _invite(host)
    assert via(host, "team")[0].post("/api/users/redeem", json={"code": code}).status_code == 200
    for c in (code, "no-such-code"):
        r = _redeem(host, "tunnel", code=c, name="x", endpoint_id=EP_SAM, card=WANT_TEAM)
        assert r.status_code == 403 and r.json()["detail"] == INVALID
    assert host.ctx.pairing.members_for(host.users["Sam"]) == []


# ---- the device file ---------------------------------------------------------------------------


def test_an_old_device_file_still_loads(tmp_path):
    path = tmp_path / "paired_devices.json"
    path.write_text(
        json.dumps(
            {
                "devices": [
                    {
                        "id": "d1",
                        "name": "Laptop",
                        "token_hash": "abc",
                        "created_at": "2026-10-01T00:00:00+00:00",
                        "last_seen_at": None,
                        "kind": "desktop",
                        "endpoint_id": "ee" * 32,
                        "person": None,
                    },
                    {
                        "id": "d2",
                        "name": "Phone",
                        "token_hash": "def",
                        "created_at": "2026-10-02T00:00:00+00:00",
                    },
                ]
            }
        )
    )
    service = PairingService(path)
    assert [d.user_id for d in service.devices()] == [None, None]
    assert service.members_for("u1") == [] and service.revoke_members("u1") == 0
    device, token = service.add_member("Mac", "ff" * 32, "u1")
    again = PairingService(path)  # and the new field round-trips
    assert again.get(device.id).user_id == "u1" and again.verify(token).kind == MEMBER
    assert len(again.devices()) == 3


# ---- M7: the event stream ----------------------------------------------------------------------


def _hears(m: Machine, who: str, event: dict) -> bool:
    user = m.ctx.users.get(m.users[who])
    state = {"channel": auth.MEMBER_DEVICE, "user_id": user.id, "loopback_main": False}
    reset = access.current.set(access.Principal(user.id, user.name, user.role))
    try:
        return visibility(m.ctx, state)(event)
    finally:
        access.current.reset(reset)


@pytest.mark.parametrize("who", ["Sam", "Rae", "Corey"])
@pytest.mark.parametrize("kind", sorted(SESSIONLESS))
def test_session_less_events_reach_a_member_device_only_when_the_team_may_hear_them(
    apps, who, kind
):
    assert _hears(apps, who, {"type": kind}) == SESSIONLESS[kind]
    assert SESSIONLESS[kind] == (kind in TEAM_EVENTS)


def test_an_unknown_session_less_event_stays_with_the_owner_for_a_member_device(apps):
    for who in ("Sam", "Rae", "Corey"):
        assert not _hears(apps, who, {"type": "something_new"}), who
        assert not _hears(apps, who, {"type": "levels", "session_id": ""}), who


def test_meeting_events_follow_the_read_rule_for_a_member_device(apps):
    seen = {
        key
        for key, sid in apps.ids.items()
        if _hears(apps, "Sam", {"type": "live_segment", "session_id": sid})
    }
    assert seen == SEES["member"]


def test_over_a_real_socket_a_member_device_hears_only_what_it_may(apps):
    events = [
        {"type": "sync", "peer": "x"},
        {"type": "meeting_app"},
        {"type": "live_segment", "session_id": apps.ids["own"]},
        {"type": "live_segment", "session_id": apps.ids["team"]},
        {"type": "shares"},
    ]
    client, headers = via(apps, "tunnel", apps.tokens["member_app"])
    with client.websocket_connect("/ws", headers=headers) as ws:
        assert ws.receive_json()["type"] == "hello"
        for e in events:
            ws.portal.call(apps.ctx.bus.publish, e)
        ws.send_json({"type": "ping"})
        got = []
        while (msg := ws.receive_json())["type"] != "pong":
            got.append((msg["type"], msg.get("session_id")))
    assert got == [("live_segment", apps.ids["team"]), ("shares", None)]


# ---- M4: hand-offs and streams from a member's app (task T3) -----------------------------------


def _calm(m: Machine) -> None:
    st = m.ctx.settings
    st.live_transcription = False  # no live job behind a streamed recording
    st.copilot = False
    st.auto_transcribe = False


def avia(m: Machine, how: str, token: str):
    """`via` for async tests (jobs need the test's own loop): (client, headers)."""

    app, base, addr = {
        "tunnel": (Tunnelled(m.app), MAIN, "127.0.0.1"),
        "main": (m.app, MAIN, "127.0.0.1"),
        "team": (m.app, f"http://{LAN}:{TEAM_PORT}", LAN),
        "lan": (m.app, MAIN, LAN),
    }[how]
    transport = httpx.ASGITransport(app=app, client=(addr, 50000))
    return httpx.AsyncClient(transport=transport, base_url=base), {
        "Authorization": f"Bearer {token}"
    }


async def _jobs_done(m: Machine, sid: str) -> None:
    jobs = [j for j in m.ctx.jobs.list(session_id=sid) if j.kind == "finish"]
    assert jobs, "no finish job"
    for j in jobs:
        done = await m.ctx.jobs.wait(j.id)
        assert done.status == "completed", done.error


async def _hand(client, headers, meta_: dict):
    from tests.test_handoff import handoff_form

    form = handoff_form(meta_)
    return await client.post(
        "/api/connections/handoff", data=form["data"], files=form["files"], headers=headers
    )


async def _stream(client, headers, **body) -> httpx.Response:  # noqa: F821
    body = {"sources": ["mic", "system"], "sample_rate": 16000} | body
    return await client.post("/api/audio/start-browser", json=body, headers=headers)


async def _stream_and_stop(m: Machine, client, headers) -> str:
    r = await _stream(client, headers)
    assert r.status_code == 200, r.text
    sid = r.json()["session_id"]
    r = await client.post(f"/api/audio/stop/{sid}", json={"transcribe": False}, headers=headers)
    assert r.status_code == 200, r.text
    await _jobs_done(m, sid)
    return sid


def _owner_of(m: Machine, sid: str) -> str:
    repo = m.ctx.repo
    with repo._lock:
        return repo._conn.execute("SELECT owner_id FROM sessions WHERE id=?", (sid,)).fetchone()[0]


@pytest.fixture
def ann_app(apps):
    """A second member's app (Ann), beside Sam's."""
    apps.tokens["ann_app"] = app_device(apps, "Ann", "a4" * 32)
    return apps.tokens["ann_app"]


@pytest.mark.anyio
async def test_a_members_handoff_makes_a_meeting_the_member_owns(apps, ann_app, fake_pipewire):
    from tests.test_handoff import meta

    _calm(apps)
    sam = apps.users["Sam"]
    client, headers = avia(apps, "tunnel", apps.tokens["member_app"])
    r = await _hand(client, headers, meta("m-1"))
    assert r.status_code == 200, r.text
    sid = r.json()["session_id"]
    await _jobs_done(apps, sid)
    assert _owner_of(apps, sid) == sam
    assert (await client.get(f"/api/sessions/{sid}", headers=headers)).status_code == 200
    assert sid in {s["id"] for s in (await client.get("/api/sessions", headers=headers)).json()}
    # Another member does not see it.
    other, oh = avia(apps, "tunnel", ann_app)
    assert (await other.get(f"/api/sessions/{sid}", headers=oh)).status_code == 404
    assert sid not in {s["id"] for s in (await other.get("/api/sessions", headers=oh)).json()}
    # The history names the person and the device.
    handed = next(e for e in apps.ctx.repo.events(sid) if e["kind"] == "handed_over")
    device = apps.ctx.pairing.members_for(sam)[0]
    assert handed["detail"]["device_id"] == device.id
    assert handed["detail"]["by"] == f"Sam ({device.name})"
    # A retry is the same meeting (the key is the device's).
    again = await _hand(client, headers, meta("m-1"))
    assert again.status_code == 200 and again.json()["session_id"] == sid


@pytest.mark.anyio
async def test_a_members_handoff_is_refused_off_the_tunnel_and_for_browser_users(apps):
    from tests.test_handoff import meta

    _calm(apps)
    for how in ("main", "team", "lan"):
        c, h = avia(apps, how, apps.tokens["member_app"])
        assert (await _hand(c, h, meta("x"))).status_code == 401, how
    # A signed-in browser user (a users.json token) is still not a computer.
    c, h = avia(apps, "tunnel", apps.tokens["member"])
    assert (await _hand(c, h, meta("x"))).status_code == 403


@pytest.mark.anyio
async def test_a_disabled_member_cannot_hand_over(apps):
    from tests.test_handoff import meta

    c, h = avia(apps, "tunnel", apps.tokens["member_app"])
    apps.ctx.users.update(apps.users["Sam"], disabled=True)
    assert (await _hand(c, h, meta("d"))).status_code == 401


@pytest.mark.anyio
async def test_a_member_replaces_only_a_meeting_its_own_device_streamed(
    apps, ann_app, fake_pipewire
):
    from tests.test_handoff import meta

    _calm(apps)
    sam = apps.users["Sam"]
    sc, sh = avia(apps, "tunnel", apps.tokens["member_app"])
    sid = await _stream_and_stop(apps, sc, sh)
    assert _owner_of(apps, sid) == sam
    started = next(e for e in apps.ctx.repo.events(sid) if e["kind"] == "recording_started")
    assert started["detail"]["by_device"] == apps.ctx.pairing.members_for(sam)[0].id

    # Another member's device, the owner's laptop, and the owner's own app: refused.
    ac, ah = avia(apps, "tunnel", ann_app)
    assert (await _hand(ac, ah, meta("r0", session_id=sid))).status_code in (404, 409)
    lc, lh = avia(apps, "tunnel", apps.tokens["device"])
    assert (await _hand(lc, lh, meta("r0b", session_id=sid))).status_code == 409
    oc, oh = avia(apps, "tunnel", apps.tokens["owner_app"])
    assert (await _hand(oc, oh, meta("r0c", session_id=sid))).status_code in (404, 409)

    r = await _hand(sc, sh, meta("r1", session_id=sid))
    assert r.status_code == 200, r.text
    assert r.json() == {"session_id": sid, "replaced": True}
    await _jobs_done(apps, sid)
    assert _owner_of(apps, sid) == sam

    # A meeting the owner's laptop streamed is not Sam's to replace either.
    laptop_sid = await _stream_and_stop(apps, lc, lh)
    r = await _hand(sc, sh, meta("r2", session_id=laptop_sid))
    assert r.status_code in (404, 409), r.text


@pytest.mark.anyio
async def test_the_owners_laptop_handoff_is_unchanged(apps, fake_pipewire):
    from tests.test_handoff import meta

    _calm(apps)
    lc, lh = avia(apps, "tunnel", apps.tokens["device"])
    r = await _hand(lc, lh, meta("l-1"))
    assert r.status_code == 200, r.text
    sid = r.json()["session_id"]
    await _jobs_done(apps, sid)
    handed = next(e for e in apps.ctx.repo.events(sid) if e["kind"] == "handed_over")
    assert handed["detail"]["by"] == "Laptop"
    assert _owner_of(apps, sid) != apps.users["Sam"]


@pytest.mark.anyio
async def test_a_member_streams_a_meeting_it_owns_and_consent_applies(apps, fake_pipewire):
    _calm(apps)
    sam = apps.users["Sam"]
    sc, sh = avia(apps, "tunnel", apps.tokens["member_app"])
    apps.ctx.settings.require_consent = True
    assert (await _stream(sc, sh)).status_code == 400
    r = await _stream(sc, sh, consent="all_parties")
    assert r.status_code == 200, r.text
    sid = r.json()["session_id"]
    assert _owner_of(apps, sid) == sam
    events = apps.ctx.repo.events(sid)
    started = next(e for e in events if e["kind"] == "recording_started")
    assert started["detail"]["by_device"] == apps.ctx.pairing.members_for(sam)[0].id
    consent = next(e for e in events if e["kind"] == "consent")
    assert consent["detail"]["by"] == "Sam"
    assert (
        await sc.post(f"/api/audio/stop/{sid}", json={"transcribe": False}, headers=sh)
    ).status_code == 200
    await _jobs_done(apps, sid)
