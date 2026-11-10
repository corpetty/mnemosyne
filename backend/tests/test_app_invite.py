"""The app invite on a hosting computer's team invites (docs/plans/2026-11-10-team-members-in-
the-app.md T2): `<ticket>#<code>`, the form `pair` takes, from the computer's own sidecar."""

# ruff: noqa: F811 - fixtures imported from the hosting tests are used as arguments
from __future__ import annotations

from tests.test_hosting_access import make_machine  # noqa: F401
from tests.test_hosting_visibility import host  # noqa: F401
from tests.test_member_devices import via


def _fake_prepare(monkeypatch, host, *, ticket="ticket-of-host", problem=""):
    from mnemosyne.api.routes import users as users_route
    from mnemosyne.services.pairing_prep import PairingUnavailable, Prepared

    calls = []

    async def prepare(ctx, *, link=True, timeout=None):
        calls.append(link)
        if problem:
            raise PairingUnavailable(problem)
        return Prepared()

    monkeypatch.setattr(users_route, "prepare_pairing", prepare)
    monkeypatch.setattr(host.ctx.link.status, "ticket", ticket)
    return calls


def test_a_hosting_computers_invite_carries_the_app_invite(host, monkeypatch):
    calls = _fake_prepare(monkeypatch, host)
    owner = {"Authorization": f"Bearer {host.tokens['api_token']}"}
    client = via(host, "main")[0]
    r = client.post(f"/api/users/{host.users['Sam']}/invite", headers=owner)
    assert r.status_code == 200, r.text
    body = r.json()
    assert (
        body["app_invite"] == f"ticket-of-host#{body['code']}" and body["app_invite_problem"] == ""
    )
    assert calls == [True]
    r = client.post("/api/users", json={"name": "Zed"}, headers=owner)
    assert r.json()["app_invite"] == f"ticket-of-host#{r.json()['code']}"


def test_no_app_invite_when_the_sidecar_cannot_run(host, monkeypatch):
    _fake_prepare(monkeypatch, host, problem="Remote access needs the program")
    owner = {"Authorization": f"Bearer {host.tokens['api_token']}"}
    r = via(host, "main")[0].post(f"/api/users/{host.users['Sam']}/invite", headers=owner)
    body = r.json()
    assert r.status_code == 200 and body["code"]
    assert body["app_invite"] is None
    assert body["app_invite_problem"] == "Remote access needs the program"


def test_no_app_invite_on_a_dedicated_server(make_machine, monkeypatch):
    m = make_machine("dedicated")
    calls = _fake_prepare(monkeypatch, m)
    owner = {"Authorization": f"Bearer {m.tokens['api_token']}"}
    sam = next(u for u in m.ctx.users.list() if u.name == "Sam")
    r = via(m, "main")[0].post(f"/api/users/{sam.id}/invite", headers=owner)
    body = r.json()
    assert r.status_code == 200, r.text
    assert body["app_invite"] is None and body["app_invite_problem"] == "" and calls == []
