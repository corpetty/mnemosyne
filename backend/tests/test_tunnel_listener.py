"""The tunnel listener (services/tunnel_listener.py): what the link sidecar carries in arrives on a
loopback port of its own and is never this computer's own request, whatever its source address
(docs/plans/2026-11-09-macos.md finding 2: on macOS the sidecar could not bind 127.0.0.2 and fell
back to 127.0.0.1, so a remote peer became this computer). Real TCP to a real second listener."""

from __future__ import annotations

import httpx
import pytest
from fastapi.testclient import TestClient

from mnemosyne.api.app import create_app
from tests.test_hosting_access import (
    CONTACT_TOKEN,
    _add_contact,
    _add_probe_routes,
    _user_token,
)

pytestmark = pytest.mark.anyio


class Box:
    """One computer with its tunnel listener running, and the tokens of its callers."""

    def __init__(self, app, lifespan: TestClient):
        self.app = app
        self.ctx = app.state.ctx
        self.lifespan = lifespan
        self.port = lifespan.portal.call(self.ctx.tunnel.ensure, app)
        _, self.device = self.ctx.pairing.add_desktop("Laptop", "cd" * 32)
        _add_contact(self.ctx)
        self.owner_id, self.owner = _user_token(self.ctx, "Corey", "admin")

    def tunnel(self, token: str | None = None, **kw) -> httpx.Response:
        """A request through the tunnel listener, as the sidecar makes it (from 127.0.0.1)."""
        headers = kw.pop("headers", {})
        if token:
            headers["Authorization"] = f"Bearer {token}"
        method = kw.pop("method", "GET")
        path = kw.pop("path", "/api/_t/channel")
        with httpx.Client(base_url=f"http://127.0.0.1:{self.port}", timeout=10) as c:
            return c.request(method, path, headers=headers, **kw)


@pytest.fixture
def box(settings, keystore):
    app = create_app(settings, keystore=keystore)
    app.state.ctx.live_watcher.interval = 0
    app.state.ctx.live_bridges.enabled = False
    app.state.ctx.record_sealer.interval = 0
    _add_probe_routes(app)
    with TestClient(app, base_url="http://127.0.0.1:8008", client=("127.0.0.1", 50000)) as c:
        yield Box(app, c)


def test_a_plain_computer_without_a_token_still_wants_one_on_the_tunnel_port(box):
    # No api_token, no team mode: this machine's own window needs no sign-in at all...
    assert not box.ctx.settings.api_token and not box.ctx.settings.team_mode
    assert box.lifespan.get("/api/_t/channel").json()["channel"] == "local"
    # ...but nothing that came through the tunnel gets in without a token.
    assert box.port and box.port != 8008
    assert box.tunnel().status_code == 401
    assert box.tunnel(path="/api/sessions").status_code == 401
    assert box.tunnel("not-a-token").status_code == 401


def test_a_paired_computer_comes_in_as_a_device(box):
    r = box.tunnel(box.device)
    assert r.status_code == 200
    body = r.json()
    assert body["channel"] == "device" and body["loopback_main"] is False


def test_a_contact_comes_in_as_a_contact_on_share_paths_only(box):
    r = box.tunnel(CONTACT_TOKEN, path="/api/share/_t/channel")
    assert r.status_code == 200 and r.json()["channel"] == "contact"
    assert box.tunnel(CONTACT_TOKEN).status_code == 401


def test_the_shells_local_paths_are_refused_there(box):
    box.ctx.settings.api_token = "s3cret"
    assert box.lifespan.get("/api/system").status_code == 200  # the desktop shell, tokenless
    assert box.tunnel(path="/api/system").status_code == 401
    assert box.tunnel(method="POST", path="/api/system/attach").status_code == 401
    # A device token reaches the route, which still refuses anything not from the shell.
    assert box.tunnel(box.device, path="/api/system/main").status_code == 403


def test_never_the_tokenless_desktop_owner_of_a_hosting_computer(box):
    st = box.ctx.settings
    st.team_mode, st.team_owner_id = True, box.owner_id
    assert box.lifespan.get("/api/_t/channel").json()["channel"] == "desktop"
    assert box.tunnel().status_code == 401
    # Headers naming loopback change nothing: it is the port that decides.
    assert box.tunnel(headers={"Host": "127.0.0.1:8008"}).status_code == 401
    # The owner's own token through the tunnel is a user, never the owner's main-port channel.
    r = box.tunnel(box.owner)
    assert r.json()["channel"] == "user" and r.json()["loopback_main"] is False
    assert box.tunnel(box.owner, path="/api/_t/owner").status_code == 403


def test_saving_to_a_path_is_only_for_this_computer(box):
    body = {"path": "/tmp/never-written.ogg"}
    r = box.tunnel(box.device, method="POST", path="/api/sessions/x/clips/y/save", json=body)
    assert r.status_code == 403 and "this computer" in r.json()["detail"]


def test_an_older_sidecar_from_127_0_0_2_is_still_tunnelled(app):
    with TestClient(app, base_url="http://127.0.0.1:8008", client=("127.0.0.2", 50000)) as c:
        assert c.get("/api/sessions").status_code == 401
        assert c.get("/api/system").status_code == 401


async def test_without_a_listener_remote_access_is_not_started(settings, keystore):
    """Fail closed: no app to serve the tunnel port (never the main port instead)."""
    app = create_app(settings, keystore=keystore)
    ctx = app.state.ctx
    assert ctx.app is None
    ctx.settings.remote_access = True
    await ctx.apply_remote_access()
    assert not ctx.link.active and not ctx.tunnel.running
    assert "remote access could not start" in ctx.link.status.error
