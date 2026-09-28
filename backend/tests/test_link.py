"""Remote access: the backend runs the mnemosyne-link sidecar while remote_access is on
(a stand-in script here; the real tunnel is tested in link/tests)."""

import asyncio
import stat
import sys

import pytest

from mnemosyne.services.link import LinkService

pytestmark = pytest.mark.anyio


def fake_link(tmp_path, body: str) -> str:
    script = tmp_path / "mnemosyne-link"
    script.write_text(f"#!{sys.executable}\nimport json, sys, time\n{body}\n")
    script.chmod(script.stat().st_mode | stat.S_IXUSR)
    return str(script)


READY = (
    'assert sys.argv[1:4] == ["home", "--backend", "127.0.0.1:8008"], sys.argv\n'
    'print(json.dumps({"endpoint_id": "e1d", "ticket": "endpointe1d"}), flush=True)\n'
    "time.sleep(60)"
)


async def wait_for(cond, timeout=5.0):
    for _ in range(int(timeout / 0.05)):
        if cond():
            return
        await asyncio.sleep(0.05)
    raise AssertionError("timed out")


async def test_runs_and_stops_the_sidecar(tmp_path):
    link = LinkService(tmp_path, 8008, binary=fake_link(tmp_path, READY))
    link.start()
    await wait_for(lambda: link.status.running)
    assert (link.status.endpoint_id, link.status.ticket) == ("e1d", "endpointe1d")
    proc = link._proc
    await link.stop()
    assert proc.returncode is not None and not link.status.running


async def test_reports_a_sidecar_that_dies(tmp_path, monkeypatch):
    monkeypatch.setattr("mnemosyne.services.link.RESTART_DELAY", 60)
    link = LinkService(tmp_path, 8008, binary=fake_link(tmp_path, "sys.exit(3)"))
    link.start()
    await wait_for(lambda: link.status.error is not None)
    assert "before it was ready" in link.status.error
    await link.stop()


async def test_missing_binary(tmp_path, monkeypatch):
    monkeypatch.setenv("MNEMOSYNE_LINK_BIN", str(tmp_path / "nope"))
    link = LinkService(tmp_path, 8008)
    link.start()
    await wait_for(lambda: link.status.error is not None)
    assert "not installed" in link.status.error
    await link.stop()


def test_setting_turns_it_on(client, ctx, tmp_path, monkeypatch):
    monkeypatch.setenv("MNEMOSYNE_LINK_BIN", fake_link(tmp_path, READY))
    assert client.get("/api/pairing/remote").json()["enabled"] is False
    assert client.put("/api/settings", json={"remote_access": True}).status_code == 200
    for _ in range(100):
        body = client.get("/api/pairing/remote").json()
        if body["running"]:
            break
        import time

        time.sleep(0.05)
    assert body == {"enabled": True, "running": True, "endpoint_id": "e1d", "error": None}
    assert client.put("/api/settings", json={"remote_access": False}).status_code == 200
    assert client.get("/api/pairing/remote").json()["running"] is False
