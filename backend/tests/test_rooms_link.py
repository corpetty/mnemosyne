"""The guest rooms sidecar service (`mnemosyne-link rooms`, services/link.py RoomsLink), against
the stand-in in tests/fake_link.py. The real sidecar is tested in link/tests."""

import asyncio
import stat

import pytest

from mnemosyne.services.link import LinkError, RoomsLink, ensure_upload_token, parse_relays
from tests import fake_link

pytestmark = pytest.mark.anyio

ROOM = "0123456789abcdef"


@pytest.fixture
def fake(tmp_path, monkeypatch):
    # PUBLISH_* in the backend's environment must not reach the sidecar.
    monkeypatch.setenv("PUBLISH_TOKEN", "secret")
    monkeypatch.setenv("PUBLISH_RELAY_ORIGINS", "https://x")
    return fake_link.install(tmp_path, monkeypatch)


def make(tmp_path) -> RoomsLink:
    return RoomsLink(tmp_path / "data", tmp_path / "guest", tmp_path / "viewer")


def test_parse_relays():
    assert parse_relays("") == []
    assert parse_relays("https://a/, https://b/  https://c/") == [
        "https://a/",
        "https://b/",
        "https://c/",
    ]


def test_upload_token_is_created_private_and_reused(tmp_path):
    path = tmp_path / "x" / "upload.token"
    token = ensure_upload_token(path)
    assert len(token) >= 40
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert ensure_upload_token(path) == token
    path.chmod(0o644)  # looser permissions are tightened, the token kept
    assert ensure_upload_token(path) == token
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    path.write_text("")  # an empty file is refilled
    assert ensure_upload_token(path)


async def test_starts_with_the_right_arguments(tmp_path, fake):
    link = make(tmp_path)
    link.relays = ["https://r1/", "https://r2/"]
    fake.config.update(listen="127.0.0.1:41999", relay_kind="custom")
    fake.sync()
    link.start()
    await link.wait_ready()
    try:
        assert link.listen == "127.0.0.1:41999" and link.relay_kind == "custom"
        assert link.base_url == "http://127.0.0.1:41999"
        (start,) = fake.rooms_starts
        token_file = str(tmp_path / "data" / "guest_rooms" / "upload.token")
        assert start["argv"] == [
            "rooms",
            "--data-dir",
            str(tmp_path / "data"),
            "--guest-dir",
            str(tmp_path / "guest"),
            "--viewer-dir",
            str(tmp_path / "viewer"),
            "--upload-token-file",
            token_file,
            "--relay",
            "https://r1/",
            "--relay",
            "https://r2/",
        ]
        # The token file existed, 0600, when the sidecar started; the service knows the token.
        assert start["token"] == link.upload_token
        assert start["token_mode"] == "0o600"
        assert start["publish_env"] == []
    finally:
        await link.stop()
    assert link.listen is None and not link.status.running


async def test_no_relay_argument_without_settings(tmp_path, fake):
    link = make(tmp_path)
    link.start()
    await link.wait_ready()
    try:
        assert link.relay_kind == "n0"
        assert "--relay" not in fake.rooms_starts[0]["argv"]
    finally:
        await link.stop()


async def test_room_round_trips(tmp_path, fake):
    link = make(tmp_path)
    with pytest.raises(LinkError, match="Guest rooms is not running"):
        await link.room_start(ROOM)
    link.start()
    await link.wait_ready()
    try:
        assert await link.room_list() == []
        started = await link.room_start(ROOM)
        assert started == {"endpoint_id": "e" * 64, "relay": "https://relay.example/"}
        assert await link.room_start(ROOM, relay="https://other/") == started  # unchanged
        other = await link.room_start("f" * 16, relay="https://other/")
        assert other["relay"] == "https://other/"
        assert await link.room_list() == [
            {"room": ROOM, **started},
            {"room": "f" * 16, **other},
        ]
        assert await link.room_stop(ROOM) is True
        assert await link.room_stop(ROOM) is False
        assert [r["room"] for r in await link.room_list()] == ["f" * 16]
        assert fake.ops[1] == {"op": "room_start", "room": ROOM}
        assert fake.ops[2] == {"op": "room_start", "room": ROOM, "relay": "https://other/"}
    finally:
        await link.stop()


async def test_a_refused_request_raises(tmp_path, fake):
    fake.config["fail"] = {"room_start": "no such relay"}
    fake.sync()
    link = make(tmp_path)
    link.start()
    await link.wait_ready()
    try:
        with pytest.raises(LinkError, match="no such relay"):
            await link.room_start(ROOM)
    finally:
        await link.stop()


async def test_wait_ready_reports_a_missing_binary(tmp_path, monkeypatch):
    monkeypatch.setenv("MNEMOSYNE_LINK_BIN", str(tmp_path / "nope"))
    link = make(tmp_path)
    link.start()
    with pytest.raises(LinkError, match="not installed"):
        await link.wait_ready()
    link.start()  # it tries again
    with pytest.raises(LinkError, match="not installed"):
        await link.wait_ready()
    await link.stop()


async def test_ensure_rooms_is_idempotent_and_stops_on_shutdown(ctx, fake):
    ctx.settings.remote_relays = "https://r1/, https://r2/"
    assert not ctx.rooms_link.active  # built, not started; remote access is off
    assert not ctx.link.active
    link = await ctx.ensure_rooms()
    assert link is ctx.rooms_link and link.status.running
    again = await asyncio.gather(ctx.ensure_rooms(), ctx.ensure_rooms())
    assert all(x is link for x in again)
    assert len(fake.rooms_starts) == 1
    assert fake.rooms_starts[0]["argv"].count("--relay") == 2
    assert link.guest_dir.name == "guest" and link.viewer_dir.name == "viewer"
    assert link.guest_dir.parent.name == "static"
    proc = link._proc
    await ctx.shutdown()
    assert proc.returncode is not None
    assert not link.active and not link.status.running
    assert not ctx.link.active
