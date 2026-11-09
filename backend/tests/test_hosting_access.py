"""A main computer that also hosts a team (docs/plans/2026-11-09-team-hosting.md 2.1, 2.2): the
derived roles (`hosts_team`, `personal_allowed`, `dedicated_team_server`), the channel the auth
middleware records for each kind of caller, and `require_owner_channel`.

The helpers at the top are shared by the later hosting tests (guarded routes, visibility,
sync): `machine(kind)` builds a computer of one kind with its callers, `Machine.probe` asks one
caller what channel it came in on and whether the owner-only route lets it through.

Three kinds of computer: `plain` (a main computer, no team), `hosting` (team_mode and a
team_owner_id: the desktop shared itself), `dedicated` (team_mode, no owner: deploy/team/).
"""

from __future__ import annotations

from dataclasses import dataclass, field

import pytest
from fastapi import Depends, Request
from fastapi.testclient import TestClient

from mnemosyne import access
from mnemosyne.api import auth
from mnemosyne.api.app import create_app
from mnemosyne.api.auth import require_owner_channel
from mnemosyne.config import (
    Settings,
    dedicated_team_server,
    hosts_team,
    personal_allowed,
)

MAIN_PORT = 8008
MAIN = f"http://127.0.0.1:{MAIN_PORT}"
TEAM_PORT = 8443
LAN = "192.168.1.20"
CONTACT_PERSON = "ab" * 32
CONTACT_TOKEN = "contact-token-for-the-hosting-tests"
API_TOKEN = "s3cret"

# ---- the callers ------------------------------------------------------------------
# name: (base_url, client address, which token it presents, extra headers)
# A token is one of the Machine.tokens keys; None = no Authorization header at all.
CALLERS = {
    # the desktop app's window: tokenless, 127.0.0.1, the main port
    "window": (MAIN, "127.0.0.1", None, {}),
    "api_token": (MAIN, "127.0.0.1", "api_token", {}),
    # a paired laptop / sync peer: through the tunnel (127.0.0.2), a DESKTOP device token
    "device": (MAIN, "127.0.0.2", "device", {}),
    "phone": (MAIN, "192.168.1.30", "phone", {}),
    "contact": (MAIN, "127.0.0.2", "contact", {}),
    # a user's token from this machine on the main port (the owner's browser, a CLI)
    "owner_main": (MAIN, "127.0.0.1", "owner", {}),
    # ... on the team listener, from the network and from this machine
    "owner_team": (f"http://{LAN}:{TEAM_PORT}", LAN, "owner", {}),
    "owner_team_loopback": (f"http://127.0.0.1:{TEAM_PORT}", "127.0.0.1", "owner", {}),
    # ... through a proxy on this machine (tailscale serve, Caddy) or the link tunnel
    "owner_proxied": (MAIN, "127.0.0.1", "owner", {"X-Forwarded-For": "100.64.0.7"}),
    "owner_tunnel": (MAIN, "127.0.0.2", "owner", {}),
    "member": (f"http://{LAN}:{TEAM_PORT}", LAN, "member", {}),
    "reviewer": (f"http://{LAN}:{TEAM_PORT}", LAN, "reviewer", {}),
    "other_admin": (f"http://{LAN}:{TEAM_PORT}", LAN, "other_admin", {}),
}


def _add_probe_routes(app) -> None:
    """Tiny routes behind the middleware: what channel the request came in on, and the same
    guarded by `require_owner_channel`. The /api/share/ pair is where a CONTACT token reaches."""

    def channel(request: Request):
        s = request.state
        return {
            "channel": getattr(s, "channel", None),
            "user_id": getattr(s, "user_id", None),
            "device_id": getattr(s, "device_id", None),
            "on_team_port": getattr(s, "on_team_port", None),
            "loopback_main": getattr(s, "loopback_main", None),
        }

    def owner_only(request: Request):
        return {"channel": getattr(request.state, "channel", None)}

    guard = [Depends(require_owner_channel)]
    for prefix in ("/api/_t", "/api/share/_t"):
        app.add_api_route(f"{prefix}/channel", channel)
        app.add_api_route(f"{prefix}/owner", owner_only, dependencies=guard)


@dataclass
class Probe:
    status: int  # of the channel route (401 = not let in)
    channel: str | None
    owner_status: int  # of the guarded route: 200 passes, 403 refused, 401 not let in
    state: dict = field(default_factory=dict)

    @property
    def passes(self) -> bool:
        return self.owner_status == 200


@dataclass
class Machine:
    app: object
    ctx: object
    tokens: dict[str, str]

    def client(self, caller: str) -> tuple[TestClient, dict[str, str], bool]:
        base, addr, token, headers = CALLERS[caller]
        headers = dict(headers)
        if token is not None:
            headers["Authorization"] = f"Bearer {self.tokens[token]}"
        # No `with`: only the machine's own TestClient runs the lifespan.
        return (
            TestClient(self.app, base_url=base, client=(addr, 50000)),
            headers,
            token == "contact",
        )

    def probe(self, caller: str) -> Probe:
        client, headers, share = self.client(caller)
        prefix = "/api/share/_t" if share else "/api/_t"
        r = client.get(f"{prefix}/channel", headers=headers)
        o = client.get(f"{prefix}/owner", headers=headers)
        return Probe(
            r.status_code,
            r.json().get("channel") if r.status_code == 200 else None,
            o.status_code,
            r.json() if r.status_code == 200 else {},
        )


def _add_contact(ctx) -> None:
    import hashlib

    ctx.repo.put_contact(
        {
            "person": CONTACT_PERSON,
            "name": "Ada",
            "card": {"person": CONTACT_PERSON, "name": "Ada", "machines": []},
            "secret": "00" * 32,
            "created_at": "2026-10-23T00:00:00+00:00",
            "updated_at": "2026-10-23T00:00:00+00:00",
            "removed_at": "",
        }
    )
    ctx.pairing.set_contact_devices(
        CONTACT_PERSON,
        ["e1" * 32, "e2" * 32],
        hashlib.sha256(CONTACT_TOKEN.encode()).hexdigest(),
        "Ada",
    )


def _user_token(ctx, name: str, role: str) -> tuple[str, str]:
    user = ctx.users.add(name, "", role)
    code, _ = ctx.users.invite(user.id)
    _, token = ctx.users.redeem(code, "browser")
    return user.id, token


@pytest.fixture
def make_machine(settings, keystore, monkeypatch):
    """`make_machine(kind)` -> Machine of that kind, with an owner, a member, a reviewer, another
    admin, a laptop, a phone and a contact. Kinds: "plain", "plain_open" (no sign-in at all),
    "hosting", "off" (hosting turned off: team_owner_id kept), "dedicated"."""
    # A phone token only opens DEVICE_PATHS: let it reach the probe routes.
    monkeypatch.setattr(
        auth, "DEVICE_PATHS", auth.DEVICE_PATHS | {"/api/_t/channel", "/api/_t/owner"}
    )
    stack: list[TestClient] = []

    def build(kind: str) -> Machine:
        settings.team_port = TEAM_PORT
        app = create_app(settings, keystore=keystore)
        app.state.ctx.live_watcher.interval = 0
        app.state.ctx.live_bridges.enabled = False
        app.state.ctx.record_sealer.interval = 0
        _add_probe_routes(app)
        lifespan = TestClient(app)
        lifespan.__enter__()
        stack.append(lifespan)
        ctx = app.state.ctx
        st = ctx.settings
        owner_id, owner_token = _user_token(ctx, "Corey", "admin")
        _, member_token = _user_token(ctx, "Sam", "member")
        _, reviewer_token = _user_token(ctx, "Rae", "reviewer")
        _, other_admin_token = _user_token(ctx, "Alex", "admin")
        _, device_token = ctx.pairing.add_desktop("Laptop", "cd" * 32)
        _, phone_token = ctx.pairing.redeem(ctx.pairing.new_code()[0], "Phone")
        _add_contact(ctx)
        if kind != "plain_open":
            st.api_token = API_TOKEN
        if kind in ("hosting", "dedicated"):
            st.team_mode = True
        if kind in ("hosting", "off"):
            st.team_owner_id = owner_id
        if kind == "off":
            st.api_token = API_TOKEN  # hosting off, so sign-in is by token again
        return Machine(
            app,
            ctx,
            {
                "api_token": API_TOKEN,
                "owner": owner_token,
                "member": member_token,
                "reviewer": reviewer_token,
                "other_admin": other_admin_token,
                "device": device_token,
                "phone": phone_token,
                "contact": CONTACT_TOKEN,
            },
        )

    yield build
    for c in reversed(stack):
        c.__exit__(None, None, None)


# ---- the derived roles ----------------------------------------------------------------


@pytest.mark.parametrize(
    ("team_mode", "owner", "hosts", "personal", "dedicated"),
    [
        (False, "", False, True, False),  # a plain main computer
        (False, "u1", False, True, False),  # hosting turned off: the owner is kept
        (True, "u1", True, True, False),  # a main computer that hosts a team
        (True, "", False, False, True),  # a dedicated team server
    ],
)
def test_the_roles_are_derived_from_two_settings(team_mode, owner, hosts, personal, dedicated):
    s = Settings(team_mode=team_mode, team_owner_id=owner)
    assert (hosts_team(s), personal_allowed(s), dedicated_team_server(s)) == (
        hosts,
        personal,
        dedicated,
    )
    assert not (hosts and dedicated)


# ---- the channel matrix ---------------------------------------------------------------

# kind -> caller -> (channel it comes in on, whether require_owner_channel lets it through).
# channel None = not let in (401); a refused-by-401 caller never reaches the guard.
PLAIN = {
    "window": (None, False),  # api_token set: a tokenless window is not signed in
    "api_token": ("api_token", True),
    "device": ("device", True),
    "phone": ("phone", True),
    "contact": ("contact", True),
    "owner_main": ("user", True),
    "member": ("user", True),
}
HOSTING = {
    "window": ("desktop", True),
    "api_token": ("api_token", True),
    "device": ("device", True),
    "phone": ("phone", False),
    # A contact is let in on /api/share/ as a contact (api/auth.py `_contact_principal`), and is
    # not the owner's channel.
    "contact": ("contact", False),
    "owner_main": ("user", True),
    "owner_team": ("user", False),
    "owner_team_loopback": ("user", False),
    "owner_proxied": ("user", False),
    "owner_tunnel": ("user", False),
    "member": ("user", False),
    "reviewer": ("user", False),
    "other_admin": ("user", False),
}
DEDICATED = {
    # No owner at this machine: a tokenless window is nobody. Nothing is guarded here: the
    # refusals it already has stay as they are.
    "window": (None, False),
    "api_token": ("api_token", True),
    "device": ("device", True),
    "phone": ("phone", True),
    "contact": (None, False),
    "owner_main": ("user", True),
    "owner_team": ("user", True),
    "member": ("user", True),
    "reviewer": ("user", True),
    "other_admin": ("user", True),
}
# Hosting turned off (team_mode False, team_owner_id kept): a plain main computer again.
OFF = PLAIN


def _matrix_cases():
    for kind, table in (
        ("plain", PLAIN),
        ("hosting", HOSTING),
        ("dedicated", DEDICATED),
        ("off", OFF),
    ):
        for caller, expected in table.items():
            yield pytest.param(kind, caller, expected, id=f"{kind}-{caller}")


@pytest.mark.parametrize(("kind", "caller", "expected"), list(_matrix_cases()))
def test_channel_and_owner_guard_for_every_kind_of_caller(make_machine, kind, caller, expected):
    channel, passes = expected
    p = make_machine(kind).probe(caller)
    assert p.channel == channel, p
    if channel is None:
        assert p.status == 401 and p.owner_status == 401
    else:
        assert p.status == 200
        assert p.owner_status == (200 if passes else 403), p


def test_a_plain_computer_with_no_sign_in_is_local(make_machine):
    m = make_machine("plain_open")
    p = m.probe("window")
    assert p.channel == "local" and p.passes
    assert p.state["on_team_port"] is False and p.state["loopback_main"] is True


def test_the_refusal_says_whose_job_it_is(make_machine):
    m = make_machine("hosting")
    client, headers, _ = m.client("member")
    r = client.get("/api/_t/owner", headers=headers)
    assert r.status_code == 403
    assert r.json()["detail"] == "Only the owner of this computer can do this"


def test_the_tokenless_window_is_the_desktop_channel_only_on_the_main_port(make_machine):
    m = make_machine("hosting")
    # the same request on the team port, or passed on by a proxy, is nobody
    for base, addr, headers in (
        (f"http://127.0.0.1:{TEAM_PORT}", "127.0.0.1", {}),
        (MAIN, "127.0.0.1", {"X-Forwarded-For": "100.64.0.7"}),
        (MAIN, "127.0.0.1", {"Host": "desk.tail1234.ts.net"}),
        (MAIN, LAN, {}),
    ):
        client = TestClient(m.app, base_url=base, client=(addr, 50000))
        assert client.get("/api/_t/channel", headers=headers).status_code == 401, (base, headers)


def test_the_state_says_who_and_where(make_machine):
    m = make_machine("hosting")
    owner = m.probe("owner_main").state
    assert owner["user_id"] == m.ctx.settings.team_owner_id
    assert owner["on_team_port"] is False and owner["loopback_main"] is True
    on_team = m.probe("owner_team_loopback").state
    assert on_team["on_team_port"] is True and on_team["loopback_main"] is False
    assert m.probe("device").state["device_id"]
    assert m.probe("member").state["user_id"] != m.ctx.settings.team_owner_id


def test_the_app_window_on_a_local_path_is_the_desktop_channel(make_machine, monkeypatch):
    """With an API token set (no owner, no team) the shell's own tokenless calls to LOCAL_PATHS
    still come in as the desktop."""
    monkeypatch.setattr(auth, "LOCAL_PATHS", auth.LOCAL_PATHS | {"/api/_t/channel"})
    m = make_machine("plain")
    assert m.probe("window").channel == "desktop"
    # not from the network, nor through the tunnel
    client = TestClient(m.app, base_url=MAIN, client=("127.0.0.2", 50000))
    assert client.get("/api/_t/channel").status_code == 401


def test_the_owner_must_still_be_the_owner(make_machine):
    """Another computer's channel does not make anyone the owner: the owner's user token stops
    being the owner's when someone else is recorded as the owner of the computer."""
    m = make_machine("hosting")
    assert m.probe("owner_main").passes
    other = m.ctx.users.list()[1].id
    m.ctx.settings.team_owner_id = other
    assert not m.probe("owner_main").passes


def test_turning_hosting_off_makes_every_caller_the_owner_again(make_machine):
    m = make_machine("hosting")
    assert not m.probe("member").passes
    m.ctx.settings.team_mode = False
    assert m.probe("member").passes and m.ctx.settings.team_owner_id


def test_the_principal_is_still_set_for_users(make_machine):
    """The channel changes nothing about who a user is (access.current)."""
    m = make_machine("hosting")

    async def who():
        p = access.principal()
        return {"who": p.user_id if p else None}

    m.app.add_api_route("/api/_t/who", who)
    client, headers, _ = m.client("member")
    assert client.get("/api/_t/who", headers=headers).json()["who"]
    client, headers, _ = m.client("api_token")
    assert client.get("/api/_t/who", headers=headers).json()["who"] is None


# ---- the personal routes (plan task 4, I3) -----------------------------------------------------

OWNER_ONLY = auth.OWNER_ONLY
FAKE_SHA = "a" * 64

# Every route that is the owner's own business: (method, path). Concrete paths: nothing here
# needs to exist, a caller who gets past the guard may answer 404, 409 or 422.
PERSONAL_ROUTES = [
    ("GET", "/api/sync/peers"),
    ("GET", "/api/sync/status"),
    ("GET", "/api/sync/hello"),
    ("GET", "/api/sync/changes"),
    ("POST", "/api/sync/poke"),
    ("POST", "/api/sync/now"),
    ("POST", "/api/sync/pair"),
    ("DELETE", "/api/sync/peers/x"),
    ("GET", "/api/sync/person-key"),
    ("PUT", "/api/sync/person-key"),
    ("GET", "/api/sync/contacts"),
    ("GET", f"/api/sync/media/s1/{FAKE_SHA}"),
    ("POST", "/api/pairing/codes"),
    ("POST", "/api/pairing/prepare"),
    ("GET", "/api/pairing/devices"),
    ("DELETE", "/api/pairing/devices/x"),
    ("GET", "/api/pairing/remote"),
    ("GET", "/api/contacts"),
    ("POST", "/api/contacts/codes"),
    ("POST", "/api/contacts/pair"),
    ("PATCH", "/api/contacts/p1"),
    ("POST", "/api/contacts/p1/pull"),
    ("DELETE", "/api/contacts/p1"),
    ("GET", "/api/sessions/s1/peer-shares"),
    ("PUT", "/api/sessions/s1/peer-shares/p1"),
    ("DELETE", "/api/sessions/s1/peer-shares/p1"),
    ("GET", "/api/connections/devices"),
    ("POST", "/api/connections/handoff"),
    ("GET", "/api/system/main"),
    ("PUT", "/api/system/main"),
    ("DELETE", "/api/system/main"),
    ("PUT", "/api/settings"),
    ("PUT", "/api/team"),
]
# Not guarded on purpose: redeeming a pairing code (the device being paired has nothing yet),
# the phone's `/api/pairing/me`, `/api/share/*` (contacts authenticate there as contacts), and
# `GET /api/team` (the status; `can_change` says whether the caller may change it).

# On a host: who is refused (always 403 "Only the owner ...") and who is let past the guard.
REFUSED_ON_HOST = (
    "member",
    "reviewer",
    "other_admin",
    "owner_team",
    "owner_team_loopback",
    "owner_proxied",
    "owner_tunnel",
    "phone",
)
OWNER_CHANNELS_ON_HOST = ("window", "api_token", "device", "owner_main")


def _send(m: Machine, caller: str, method: str, path: str):
    client, headers, _ = m.client(caller)
    # An empty JSON object is a valid body for every route that takes one and changes nothing.
    return client.request(method, path, headers=headers, json={})


def _owner_refusal(r) -> bool:
    return r.status_code == 403 and r.json().get("detail") == OWNER_ONLY


@pytest.fixture
def personal_paths_for_phone(monkeypatch):
    """A phone token opens only DEVICE_PATHS (api/auth.py): widen them to every route here so
    the guard itself is what refuses a phone, were that list ever widened."""
    monkeypatch.setattr(auth, "DEVICE_PATHS", auth.DEVICE_PATHS | {p for _, p in PERSONAL_ROUTES})


def test_on_a_host_every_personal_route_is_refused_to_everyone_but_the_owner(
    make_machine, personal_paths_for_phone
):
    m = make_machine("hosting")
    wrong = []
    for method, path in PERSONAL_ROUTES:
        for caller in REFUSED_ON_HOST:
            r = _send(m, caller, method, path)
            if not _owner_refusal(r):
                wrong.append((method, path, caller, r.status_code))
    assert not wrong


def test_on_a_host_the_owner_channels_get_past_the_guard(make_machine):
    m = make_machine("hosting")
    wrong = []
    # PUT /api/settings last: it applies the settings again
    for method, path in sorted(PERSONAL_ROUTES, key=lambda r: r[1] == "/api/settings"):
        for caller in OWNER_CHANNELS_ON_HOST:
            r = _send(m, caller, method, path)
            if r.status_code == 401 or _owner_refusal(r):
                wrong.append((method, path, caller, r.status_code))
    assert not wrong


def test_a_contact_and_a_phone_do_not_reach_the_personal_routes_even_unguarded(make_machine):
    """Without the widened DEVICE_PATHS a phone is not let in at all, and on a host a contact
    token opens only /api/share/ (as a contact: never the personal routes)."""
    m = make_machine("hosting")
    for method, path in PERSONAL_ROUTES:
        for caller in ("phone", "contact"):
            assert _send(m, caller, method, path).status_code in (401, 403), (path, caller)


@pytest.mark.parametrize("kind", ["plain", "dedicated", "off"])
def test_elsewhere_the_guard_changes_nothing(make_machine, kind, personal_paths_for_phone):
    """A plain computer, a dedicated server and a computer whose hosting is off: no caller is
    ever told "only the owner" (the routes answer as before: their own refusals stand)."""
    m = make_machine(kind)
    callers = ("api_token", "device", "owner_main", "member", "reviewer", "other_admin", "phone")
    wrong = []
    for method, path in PERSONAL_ROUTES:
        for caller in callers:
            if caller == "owner_main" and path == "/api/settings" and kind == "dedicated":
                continue
            r = _send(m, caller, method, path)
            if _owner_refusal(r):
                wrong.append((method, path, caller))
    assert not wrong


def test_a_dedicated_server_keeps_its_own_refusals(make_machine):
    """The existing team_mode refusals are still what answers (the guard is not in the way)."""
    m = make_machine("dedicated")
    client, headers, _ = m.client("api_token")
    r = client.get("/api/sync/hello", headers=headers)
    assert r.status_code == 409 and "team server" in r.json()["detail"]
    r = client.post("/api/pairing/codes", headers=headers, json={"kind": "sync"})
    assert r.status_code == 409 and "team server" in r.json()["detail"]
    r = client.get("/api/contacts", headers=headers)
    assert r.status_code == 409 and "team server" in r.json()["detail"]


def test_a_member_is_refused_before_the_feature_says_anything(make_machine):
    """The guard answers first: the same words whatever the route, never the route's own reason."""
    m = make_machine("hosting")
    details = set()
    for method, path in PERSONAL_ROUTES:
        r = _send(m, "member", method, path)
        details.add((r.status_code, r.json()["detail"]))
    assert details == {(403, OWNER_ONLY)}


# ---- the team on/off route and a proxy on this machine (R6) -----------------------------


def test_the_team_route_refuses_a_proxy_on_this_machine(make_machine):
    """`tailscale serve` / Caddy pass requests on from 127.0.0.1: they carry the owner's token
    perhaps, but they are not the desktop app. On a plain computer the old `_local` check let
    them through."""
    m = make_machine("plain")
    client, headers, _ = m.client("api_token")
    assert client.get("/api/team", headers=headers).json()["can_change"] is True
    assert client.put("/api/team", headers=headers, json={}).status_code == 200
    proxied = {**headers, "X-Forwarded-For": "100.64.0.7"}
    assert client.get("/api/team", headers=proxied).json()["can_change"] is False
    r = client.put("/api/team", headers=proxied, json={"enabled": True})
    assert r.status_code == 403
    assert "desktop app" in r.json()["detail"]
    assert not m.ctx.settings.team_mode
    # a Host header that is not this machine's, too
    other = {**headers, "Host": "desk.tail1234.ts.net"}
    assert client.get("/api/team", headers=other).json()["can_change"] is False
    # and the link tunnel
    tunnel = TestClient(m.app, base_url=MAIN, client=("127.0.0.2", 50000))
    assert tunnel.put("/api/team", headers=headers, json={}).status_code == 403


def test_the_team_route_on_a_host_is_the_owners_window_only(make_machine):
    m = make_machine("hosting")
    ok, headers, _ = m.client("window")
    assert ok.get("/api/team", headers=headers).json()["can_change"] is True
    assert ok.put("/api/team", headers=headers, json={}).status_code == 200
    for caller in ("owner_proxied", "owner_team_loopback", "member"):
        client, headers, _ = m.client(caller)
        r = client.put("/api/team", headers=headers, json={"enabled": False})
        assert _owner_refusal(r), caller
        assert client.get("/api/team", headers=headers).json()["can_change"] is False, caller
    assert m.ctx.settings.team_mode


# ---- only the owner is an admin on a host (I2, D2) -----------------------------------------

ONLY_OWNER_ADMIN = "On a computer that hosts a team only its owner is an admin"


def _owner_client(m: Machine):
    client, headers, _ = m.client("owner_main")
    return client, headers


def test_on_a_host_nobody_else_is_added_as_an_admin(make_machine):
    m = make_machine("hosting")
    client, headers = _owner_client(m)
    before = len(m.ctx.users.list())
    r = client.post("/api/users", headers=headers, json={"name": "Pat", "role": "admin"})
    assert r.status_code == 409 and r.json()["detail"] == ONLY_OWNER_ADMIN
    assert len(m.ctx.users.list()) == before
    for role in ("member", "reviewer"):
        r = client.post("/api/users", headers=headers, json={"name": f"P-{role}", "role": role})
        assert r.status_code == 200, r.text
        assert r.json()["user"]["role"] == role


def test_on_a_host_nobody_else_is_made_an_admin(make_machine):
    m = make_machine("hosting")
    client, headers = _owner_client(m)
    sam = next(u for u in m.ctx.users.list() if u.name == "Sam")
    r = client.patch(f"/api/users/{sam.id}", headers=headers, json={"role": "admin"})
    assert r.status_code == 409 and r.json()["detail"] == ONLY_OWNER_ADMIN
    assert m.ctx.users.get(sam.id).role == "member"
    # other changes, and other roles, are fine; so is the owner staying an admin
    assert (
        client.patch(f"/api/users/{sam.id}", headers=headers, json={"role": "reviewer"}).status_code
        == 200
    )
    assert (
        client.patch(f"/api/users/{sam.id}", headers=headers, json={"name": "Samuel"}).status_code
        == 200
    )
    owner = m.ctx.settings.team_owner_id
    r = client.patch(f"/api/users/{owner}", headers=headers, json={"role": "admin"})
    assert r.status_code == 200


def test_on_a_host_an_invite_for_another_admin_is_refused(make_machine):
    """Redeeming it and asking for it: someone who was an admin before hosting began (or whose
    file was edited) is not let in as one."""
    m = make_machine("hosting")
    client, headers = _owner_client(m)
    alex = next(u for u in m.ctx.users.list() if u.name == "Alex")
    assert alex.role == "admin"
    r = client.post(f"/api/users/{alex.id}/invite", headers=headers)
    assert r.status_code == 409 and r.json()["detail"] == ONLY_OWNER_ADMIN
    tokens = len(alex.tokens)
    code, _ = m.ctx.users.invite(alex.id)  # an invite made while it was still allowed
    anyone = TestClient(m.app, base_url=f"http://{LAN}:{TEAM_PORT}", client=(LAN, 50000))
    r = anyone.post("/api/users/redeem", json={"code": code, "device": "Browser"})
    assert r.status_code == 409 and r.json()["detail"] == ONLY_OWNER_ADMIN
    assert len(m.ctx.users.get(alex.id).tokens) == tokens  # nobody was signed in
    # the owner's own invite (another browser of theirs) and a member's still work
    owner = m.ctx.settings.team_owner_id
    own_code, _ = m.ctx.users.invite(owner)
    assert anyone.post("/api/users/redeem", json={"code": own_code}).status_code == 200
    sam = next(u for u in m.ctx.users.list() if u.name == "Sam")
    sam_code, _ = m.ctx.users.invite(sam.id)
    assert anyone.post("/api/users/redeem", json={"code": sam_code}).status_code == 200
    # an unknown code is still the invite's own error
    r = anyone.post("/api/users/redeem", json={"code": "nope"})
    assert r.status_code == 400


@pytest.mark.parametrize("kind", ["plain", "dedicated", "off"])
def test_elsewhere_admins_are_added_as_before(make_machine, kind):
    m = make_machine(kind)
    client, headers, _ = m.client("api_token")
    r = client.post("/api/users", headers=headers, json={"name": "Pat", "role": "admin"})
    assert r.status_code == 200, r.text
    pat = r.json()["user"]["id"]
    sam = next(u for u in m.ctx.users.list() if u.name == "Sam")
    assert (
        client.patch(f"/api/users/{sam.id}", headers=headers, json={"role": "admin"}).status_code
        == 200
    )
    assert client.post(f"/api/users/{pat}/invite", headers=headers).status_code == 200
    code, _ = m.ctx.users.invite(pat)
    assert client.post("/api/users/redeem", headers=headers, json={"code": code}).status_code == 200


def _cli_settings(settings, tmp_path, monkeypatch, **changes):
    s = settings.model_copy(update={"data_dir": tmp_path / "data", **changes})
    s.data_dir.mkdir(exist_ok=True)
    monkeypatch.setattr("mnemosyne.config.load_settings", lambda: s)
    return s


def test_the_cli_adds_no_other_admin_on_a_host(settings, tmp_path, monkeypatch, capsys):
    from mnemosyne.cli import users_command
    from mnemosyne.services.users import UserService

    s = _cli_settings(settings, tmp_path, monkeypatch, team_mode=True, team_owner_id="u-owner")
    with pytest.raises(SystemExit) as e:
        users_command(["add", "Pat", "--role", "admin"])
    assert str(e.value) == ONLY_OWNER_ADMIN
    assert UserService(s.data_dir / "users.json").list() == []
    users_command(["add", "Pat", "--role", "reviewer"])
    assert "Invite link" in capsys.readouterr().out
    # an invite for someone who is an admin but not the owner
    users = UserService(s.data_dir / "users.json")
    other = users.add("Alex", "", "admin")
    with pytest.raises(SystemExit) as e:
        users_command(["invite", other.id])
    assert str(e.value) == ONLY_OWNER_ADMIN
    mine = users.add("Corey", "", "admin")
    s.team_owner_id = mine.id
    users_command(["invite", mine.id])
    assert "Invite link" in capsys.readouterr().out


@pytest.mark.parametrize("changes", [{}, {"team_mode": True}, {"team_owner_id": "u-owner"}])
def test_the_cli_adds_admins_as_before_elsewhere(settings, tmp_path, monkeypatch, capsys, changes):
    """A plain computer, a dedicated server (team_mode, no owner) and hosting turned off (an
    owner kept, no team_mode)."""
    from mnemosyne.cli import users_command

    _cli_settings(settings, tmp_path, monkeypatch, **changes)
    users_command(["add", "Pat", "--role", "admin"])
    assert "Invite link" in capsys.readouterr().out


# ---- people and devices on a host (task 6) --------------------------------------------


@pytest.mark.parametrize(("kind", "lists_people"), [("hosting", True), ("dedicated", False)])
def test_the_connected_devices_of_a_host_include_contacts_and_sync_peers(
    make_machine, kind, lists_people
):
    """A dedicated server lists neither (it refuses both); a host lists them for its owner."""
    from mnemosyne.services.sync_peers import SyncPeer

    m = make_machine(kind)
    m.ctx.sync_peers.add(
        SyncPeer("peer-1", "Desk 2", "ee" * 32, "ticket", "tok", "2026-11-01T00:00:00+00:00")
    )
    client, headers, _ = m.client("api_token")
    r = client.get("/api/connections/devices", headers=headers)
    assert r.status_code == 200, r.text
    kinds = {d["kind"] for d in r.json()}
    assert {"contact", "sync"} <= kinds if lists_people else not ({"contact", "sync"} & kinds)
    assert {"computer", "phone"} <= kinds


def test_a_members_token_never_reaches_contacts_on_a_host(make_machine):
    m = make_machine("hosting")
    for caller in ("member", "reviewer", "other_admin", "contact"):
        client, headers, _ = m.client(caller)
        for method, path in (("GET", "/api/contacts"), ("POST", "/api/contacts/codes")):
            r = client.request(method, path, headers=headers, json={})
            assert r.status_code in (401, 403), (caller, path, r.status_code)
    client, headers, _ = m.client("api_token")
    assert client.get("/api/contacts", headers=headers).status_code == 200
