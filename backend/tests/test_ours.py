"""Mnemosyne's own servers (services/ours.py, docs/plans/2026-10-26-our-relays-and-host.md):
the `use_our_servers` setting, the relay and host defaults, the sidecar arguments, the guest
notes plan and the publish default. Nothing here reaches petty.services (conftest refuses the
name)."""

import socket

import pytest

from mnemosyne.api.routes.publishing import guest_csp
from mnemosyne.config import Settings, load_settings, save_settings
from mnemosyne.services import guest_notes, ours
from mnemosyne.services.link import LinkService, RoomsLink, relay_args
from tests import fake_link


def make(**kw) -> Settings:
    return Settings(**kw)


# ---- the setting ----


def test_constants():
    assert ours.OUR_RELAYS == ["https://relay.petty.services"]
    assert ours.OUR_HOST == "https://notes.petty.services"
    assert ours.PRIVACY_URL == "https://notes.petty.services/privacy"


def test_on_by_default_off_for_team_servers_and_local_only():
    assert ours.enabled(make()) is True
    assert ours.enabled(make(team_mode=True)) is False
    assert ours.enabled(make(cloud_models=False)) is False
    assert make(team_mode=True).our_servers is False


def test_a_value_the_person_set_wins():
    assert ours.enabled(make(use_our_servers=False)) is False
    assert ours.enabled(make(team_mode=True, use_our_servers=True)) is True
    assert ours.enabled(make(cloud_models=False, use_our_servers=True)) is True


def test_automatic_is_not_saved_and_stays_automatic(settings):
    save_settings(settings.model_copy(update={"use_our_servers": None}))
    loaded = load_settings()
    assert loaded.use_our_servers is None and loaded.our_servers is True
    loaded.team_mode = True  # later becoming a team server turns it off with no migration
    assert loaded.our_servers is False


def test_the_settings_api_shows_the_effective_value(client, ctx):
    values = client.get("/api/settings").json()["values"]
    assert values["use_our_servers"] is False  # the test settings fixture turns it off
    values = client.put("/api/settings", json={"use_our_servers": None}).json()["values"]
    assert values["use_our_servers"] is True  # automatic: on
    values = client.put("/api/settings", json={"cloud_models": False}).json()["values"]
    assert values["use_our_servers"] is False  # automatic: local-only turns it off
    assert ctx.settings.use_our_servers is None
    values = client.put("/api/settings", json={"use_our_servers": True}).json()["values"]
    assert values["use_our_servers"] is True and ctx.settings.use_our_servers is True


# ---- the helpers ----


def test_relays():
    assert ours.relays(make()) == ours.OUR_RELAYS
    assert ours.relays(make(use_our_servers=False)) == []  # no relay, never n0's
    assert ours.relays(make(team_mode=True)) == []
    mine = make(remote_relays="https://a.example, https://b.example")
    assert ours.relays(mine) == ["https://a.example", "https://b.example"]
    assert ours.relays(make(remote_relays="https://a.example", use_our_servers=False)) == [
        "https://a.example"
    ]


def test_publish_and_guest_host():
    assert ours.publish_host(make()) == ours.OUR_HOST
    assert ours.publish_host(make(use_our_servers=False)) == ""
    assert ours.publish_host(make(publish_host="https://pub.example.org/")) == (
        "https://pub.example.org"
    )
    assert ours.guest_host(make()) == ours.OUR_HOST
    assert ours.guest_host(make(publish_host="https://pub.example.org")) == (
        "https://pub.example.org"
    )
    both = make(publish_host="https://pub.example.org", guest_host="https://g.example/")
    assert ours.guest_host(both) == "https://g.example"
    assert ours.guest_host(make(team_mode=True)) == ""
    assert ours.is_our_host(ours.OUR_HOST + "/") and not ours.is_our_host("https://x.example")


def test_the_test_suite_cannot_reach_the_real_servers():
    with pytest.raises(socket.gaierror):
        socket.getaddrinfo("notes.petty.services", 443)


# ---- the sidecars ----


def test_relay_args():
    assert relay_args([]) == ["--no-relay"]
    assert relay_args(["https://a", "https://b"]) == [
        "--relay",
        "https://a",
        "--relay",
        "https://b",
    ]
    home = LinkService.__new__(LinkService)
    home.relays, home.port, home.data_dir = [], 8008, "/d"
    assert "--no-relay" in home._argv()
    home.relays = ours.relays(make())
    assert home._argv()[-2:] == ["--relay", "https://relay.petty.services"]


def test_the_rooms_sidecar_gets_the_same(tmp_path):
    rooms = RoomsLink(tmp_path / "d", tmp_path / "g", tmp_path / "v")
    assert rooms._argv()[-1] == "--no-relay"
    rooms.relays = list(ours.OUR_RELAYS)
    assert rooms._argv()[-2:] == ["--relay", "https://relay.petty.services"]


def test_relay_kind_names_ours():
    assert guest_notes.relay_kind("https://relay.petty.services/") == "ours"
    assert guest_notes.relay_kind("https://relay.petty.services:7443") == "ours"
    assert guest_notes.relay_kind("https://x.relay.n0.iroh.link.") == "n0"
    assert guest_notes.relay_kind("https://relay.example.org") == "custom"
    assert guest_notes.relay_kind("") == "none"


@pytest.mark.anyio
async def test_ensure_rooms_starts_with_our_relay_or_none(ctx, settings, tmp_path, monkeypatch):
    side = fake_link.install(tmp_path, monkeypatch)
    settings.use_our_servers = True
    link = await ctx.ensure_rooms()
    try:
        argv = side.rooms_starts[-1]["argv"]
        assert argv[argv.index("--relay") :] == ["--relay", "https://relay.petty.services"]
        assert "--no-relay" not in argv
    finally:
        await link.stop()
    settings.use_our_servers = False
    link = await ctx.ensure_rooms()
    try:
        argv = side.rooms_starts[-1]["argv"]
        assert "--no-relay" in argv and "--relay" not in argv
    finally:
        await link.stop()


@pytest.mark.anyio
async def test_apply_remote_access_restarts_when_the_default_changes(
    ctx, settings, tmp_path, monkeypatch
):
    fake_link.install(tmp_path, monkeypatch)
    settings.remote_access = True
    settings.use_our_servers = False
    await ctx.apply_remote_access()
    try:
        assert ctx.link.relays == []
        settings.use_our_servers = True
        await ctx.apply_remote_access()
        assert ctx.link.relays == ours.OUR_RELAYS and ctx.link.active
    finally:
        await ctx.link.stop()


# ---- guest notes ----


@pytest.mark.anyio
async def test_auto_plan_is_iroh_with_the_mirror_on_our_host(ctx, settings, tmp_path, monkeypatch):
    fake_link.install(tmp_path, monkeypatch)
    settings.use_our_servers = True
    settings.guest_transport = "auto"
    try:
        p = await guest_notes.plan(ctx)
        assert p.transport == "iroh" and p.mirror_host == ours.OUR_HOST
        assert guest_notes.configured_host(ctx) == ours.OUR_HOST
        # a publish host of your own is what the mirror uses instead
        settings.publish_host = "https://pub.example.org"
        assert (await guest_notes.plan(ctx)).mirror_host == "https://pub.example.org"
    finally:
        await ctx.rooms_link.stop()


@pytest.mark.anyio
async def test_without_our_servers_there_is_no_mirror_and_no_iroh(
    ctx, settings, tmp_path, monkeypatch
):
    fake_link.install(tmp_path, monkeypatch)
    settings.use_our_servers = False
    settings.guest_transport = "auto"
    try:
        p = await guest_notes.plan(ctx)
        assert p.transport == "tunnel" and p.mirror_host == ""
        settings.guest_transport = "iroh"
        with pytest.raises(guest_notes.GuestNotesUnavailable, match="need a relay"):
            await guest_notes.plan(ctx)
    finally:
        await ctx.rooms_link.stop()


@pytest.mark.anyio
async def test_a_team_server_has_no_default_host(ctx, settings):
    settings.use_our_servers = None
    settings.team_mode = True
    assert guest_notes.configured_host(ctx) == ""
    with pytest.raises(guest_notes.GuestNotesUnavailable):
        guest_notes.host_url(ctx)


# ---- publishing ----


def test_publish_defaults_to_our_host(client, ctx, settings):
    s = ctx.repo.save(guest_notes_meeting())
    d = client.get(f"/api/sessions/{s.id}/publication/defaults").json()
    assert d["where"] == "local" and d["host_url"] == "" and d["host_is_ours"] is False
    settings.use_our_servers = True
    d = client.get(f"/api/sessions/{s.id}/publication/defaults").json()
    assert d["where"] == "host" and d["host_url"] == ours.OUR_HOST and d["host_is_ours"] is True
    assert d["expires_days"] == 90
    settings.publish_host = "https://pub.example.org"
    d = client.get(f"/api/sessions/{s.id}/publication/defaults").json()
    assert d["where"] == "host" and d["host_url"] == "https://pub.example.org"
    assert d["host_is_ours"] is False


def guest_notes_meeting():
    from mnemosyne.models.session import Session

    return Session(name="Planning", notes="hello\n")


def test_the_upload_token_is_never_sent_to_our_host(ctx, settings):
    import asyncio

    import httpx

    from mnemosyne.services import publishing

    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append((str(request.url), request.headers.get("x-upload-token")))
        return httpx.Response(200, json={})

    ctx.http_transport = httpx.MockTransport(handler)
    settings.publish_host_token = "open-sesame"
    settings.use_our_servers = True  # no publish_host: ours
    asyncio.run(publishing._host(ctx, "GET", ours.OUR_HOST + "/pub/x", ""))
    settings.publish_host = "https://pub.example.org"
    asyncio.run(publishing._host(ctx, "GET", "https://pub.example.org/pub/x", ""))
    assert seen == [
        (ours.OUR_HOST + "/pub/x", None),
        ("https://pub.example.org/pub/x", "open-sesame"),
    ]


# ---- the guest page's CSP ----


def test_the_guest_csp_lists_our_relay_beside_n0s():
    csp = guest_csp(make(use_our_servers=False))  # always, so older links keep working
    for origin in (
        "https://relay.petty.services",
        "wss://relay.petty.services",
        "https://*.relay.n0.iroh.link",
        "wss://*.relay.n0.iroh.link",
    ):
        assert origin in csp
    assert csp.count("https://relay.petty.services") == 1
    assert "relay.example.org" in guest_csp(make(remote_relays="https://relay.example.org"))
