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
    # Contacts are still refused while team_mode is on (api/auth.py `_contact_principal`);
    # the people-sharing task lets them in on a host.
    "contact": (None, False),
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
