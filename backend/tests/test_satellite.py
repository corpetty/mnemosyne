"""A laptop that uses a main computer records (services/satellite.py, api/routes/satellite.py;
docs/plans/2026-11-09-connections-design.md, task 7). The main computer is this process's `app`
served by uvicorn on a free loopback port (its /api/record WebSocket needs a real socket); the
laptop is a second app driven over httpx.ASGITransport. Capture is `FakeCapture`: synthetic PCM
written by the test, never a device. Encoding and mixing on the main computer are the conftest
fakes (`fake_pipewire`), transcription is `fake_engine`."""

import asyncio
import socket
import struct
import time

import httpx
import pytest

from mnemosyne.api.app import create_app
from mnemosyne.config import Settings
from mnemosyne.services import satellite
from mnemosyne.services.satellite import FakeCapture, Satellite
from mnemosyne.storage.crypto import MemoryKeyStore, new_key

pytestmark = pytest.mark.anyio

RATE = 16000
LOOPBACK = ("127.0.0.1", 50000)
TUNNEL = ("127.0.0.2", 50000)


def pcm(seconds: float, value: int) -> bytes:
    return struct.pack("<h", value) * int(RATE * seconds)


async def until(check, timeout: float = 10.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        value = check()
        if value:
            return value
        await asyncio.sleep(0.01)
    raise AssertionError("timed out")


def client(app, token: str | None = None, peer=LOOPBACK):
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app, client=peer),
        base_url="http://127.0.0.1:8008",
        headers=headers,
    )


@pytest.fixture(autouse=True)
def quick(monkeypatch):
    monkeypatch.setattr(satellite, "POLL", 0.01)
    monkeypatch.setattr(satellite, "RECONNECT", (0.05,))
    monkeypatch.setattr(satellite, "DRAIN_SECONDS", 0.1)
    monkeypatch.setattr(satellite, "FLUSH_SECONDS", 5.0)
    monkeypatch.setattr(satellite, "PROBE_SECONDS", 0.0)
    monkeypatch.setattr(satellite, "BACKOFF", (1000.0,))  # a retry only when kicked


@pytest.fixture
def settings(settings):
    """The main computer: paired with the laptop, so it has an API token."""
    settings.api_token = "admin-main"
    settings.live_transcription = False
    settings.copilot = False
    settings.auto_transcribe = True
    settings.auto_summarize = False
    return settings


@pytest.fixture
async def main_url(app, fake_pipewire, fake_engine):
    import uvicorn

    config = uvicorn.Config(app, host="127.0.0.1", port=0, lifespan="off", log_level="warning")
    server = uvicorn.Server(config)
    task = asyncio.create_task(server.serve())
    await until(lambda: server.started)
    port = server.servers[0].sockets[0].getsockname()[1]
    yield f"http://127.0.0.1:{port}"
    server.should_exit = True
    await asyncio.wait_for(task, 10)


@pytest.fixture
def dead_url():
    """A loopback port nothing listens on: the main computer is not reachable."""
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    return f"http://127.0.0.1:{port}"


@pytest.fixture
def laptop_settings(tmp_path):
    return Settings(
        data_dir=tmp_path / "laptop" / "data",
        backup_dir=str(tmp_path / "laptop" / "backups"),
        use_our_servers=False,
        api_token="admin-laptop",
    )


@pytest.fixture
def laptop(laptop_settings):
    return create_app(laptop_settings, keystore=MemoryKeyStore())


@pytest.fixture
def fake():
    return FakeCapture(auto=False, sample_rate=RATE)


@pytest.fixture
async def sat(laptop, fake):
    lctx = laptop.state.ctx
    lctx.satellite = Satellite(lctx, fake)
    yield lctx.satellite
    await lctx.satellite.shutdown()


@pytest.fixture
def laptop_token(ctx):
    _, token = ctx.pairing.add_desktop("Corey's laptop", "e" * 64)
    return token


def use_main(laptop, url: str, token: str) -> None:
    laptop.state.ctx.main_computer_store().set(url, token, "Desktop")


def main_wavs(ctx, sid: str) -> dict:
    rec = ctx.active_recordings[sid]
    return {p.source: p.output_path for p in rec.processes}


def data_of(path) -> bytes:
    return path.read_bytes()[44:]


async def jobs_done(ctx, sid: str) -> list:
    """Every job of the meeting, finished (a finish job queues the transcription)."""
    for _ in range(5):
        for job in ctx.jobs.list(session_id=sid):
            done = await ctx.jobs.wait(job.id)
            assert done.status == "completed", (job.kind, done.error)
    return ctx.jobs.list(session_id=sid)


def nothing_here(laptop) -> None:
    """A recording for the main computer never enters the laptop's own library."""
    lctx = laptop.state.ctx
    assert lctx.sessions.list_sessions() == []
    rec = lctx.settings.recordings_dir
    assert not rec.exists() or not any(rec.iterdir())


def outbox_folders(laptop) -> list:
    root = laptop.state.ctx.settings.data_dir / "outbox"
    return sorted(p.name for p in root.iterdir()) if root.exists() else []


# ---- connected: streamed -----------------------------------------------------------------


async def test_a_recording_streams_to_the_main_computer(
    app, ctx, main_url, laptop, sat, fake, laptop_token
):
    use_main(laptop, main_url, laptop_token)
    mic, call, more = pcm(0.5, 1000), pcm(0.5, 50), pcm(0.25, -700)
    async with client(laptop, "admin-laptop") as c:
        r = await c.post(
            "/api/satellite/start",
            json={"mic_device": 1, "system_device": 2, "title": "Standup on the train"},
        )
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["streaming"] is True and body["session_id"]
        sid = body["session_id"]
        paths = main_wavs(ctx, sid)
        fake.write("mic", mic)
        fake.write("system", call)
        await until(lambda: data_of(paths["mic"]) == mic and data_of(paths["system"]) == call)

        status = (await c.get("/api/satellite")).json()
        assert status["main"] == {"name": "Desktop"} and status["reachable"] is True
        rec = status["recording"]
        assert rec["session_id"] == sid and rec["streaming"] is True
        assert {s["source"]: (s["sent"], s["gap"]) for s in rec["sources"]} == {
            "mic": (len(mic), False),
            "system": (len(call), False),
        }
        assert [i["state"] for i in status["outbox"]] == ["recording"]

        fake.write("mic", more)  # captured just before Stop: flushed at stop
        r = await c.post("/api/satellite/stop")
        assert r.status_code == 200, r.text
        assert r.json() == {"local_id": body["local_id"], "session_id": sid, "outcome": "streamed"}
        status = (await c.get("/api/satellite")).json()
        assert status["recording"] is None and status["outbox"] == []

    assert data_of(paths["mic"]) == mic + more and data_of(paths["system"]) == call
    jobs = await jobs_done(ctx, sid)
    assert [j.kind for j in jobs].count("transcribe") == 1
    session = ctx.sessions.get_session(sid)
    assert session.name == "Standup on the train" and session.transcript
    started = [e for e in ctx.repo.events(sid) if e["kind"] == "recording_started"]
    assert started[0]["detail"]["by_device"]
    assert outbox_folders(laptop) == []
    nothing_here(laptop)


async def test_a_stream_with_a_gap_is_completed_by_a_handoff(
    app, ctx, main_url, laptop, sat, fake, laptop_token
):
    use_main(laptop, main_url, laptop_token)
    first, second, third = pcm(0.25, 100), pcm(0.25, 200), pcm(0.25, 300)
    async with client(laptop, "admin-laptop") as c:
        r = await c.post("/api/satellite/start", json={"mic_device": 1, "system_device": 2})
        sid = r.json()["session_id"]
        paths = main_wavs(ctx, sid)
        stream = sat.active.streams["mic"]
        fake.write("mic", first)
        fake.write("system", first)
        await until(lambda: data_of(paths["mic"]) == first)
        # The network drops under the mic's socket: what was in flight may be lost.
        stream.ws.transport.abort()
        fake.write("mic", second)
        await until(lambda: stream.drops == 1 and stream.connected)
        fake.write("mic", third)
        await until(lambda: data_of(paths["mic"]).endswith(third))
        status = (await c.get("/api/satellite")).json()
        mic = next(s for s in status["recording"]["sources"] if s["source"] == "mic")
        assert mic["gap"] is True
        r = await c.post("/api/satellite/stop")
        assert r.status_code == 200, r.text
        assert r.json()["outcome"] == "handing_over" and r.json()["session_id"] == sid

        await until(lambda: outbox_folders(laptop) == [])

    handed = [e for e in ctx.repo.events(sid) if e["kind"] == "handed_over"]
    assert len(handed) == 1 and handed[0]["detail"]["replaced"] is True
    jobs = await jobs_done(ctx, sid)
    assert [j.kind for j in jobs].count("transcribe") == 1
    session = ctx.sessions.get_session(sid)
    assert session.transcript
    # The meeting's audio is now the laptop's complete files.
    folder = ctx.settings.recordings_dir / sid
    handed_mic = next(folder.glob("*_handoff_mic.wav"))
    assert data_of(handed_mic) == first + second + third
    assert len(ctx.sessions.list_sessions()) == 1
    nothing_here(laptop)


async def test_stop_on_the_main_computer_stops_the_laptop_too(
    app, ctx, main_url, laptop, sat, fake, laptop_token
):
    use_main(laptop, main_url, laptop_token)
    async with client(laptop, "admin-laptop") as c:
        r = await c.post("/api/satellite/start", json={"mic_device": 1})
        sid = r.json()["session_id"]
        paths = main_wavs(ctx, sid)
        fake.write("mic", pcm(0.25, 100))
        await until(lambda: len(data_of(paths["mic"])) == int(RATE * 0.25) * 2)
    async with client(app, laptop_token) as main_ui:  # Stop pressed on the main computer
        assert (await main_ui.post(f"/api/audio/stop/{sid}")).status_code == 200
    fake.write("mic", pcm(0.25, 200))  # the next audio finds the stream closed
    await until(lambda: sat.active is None)
    assert outbox_folders(laptop) == []
    jobs = await jobs_done(ctx, sid)
    assert [j.kind for j in jobs].count("transcribe") == 1


# ---- not connected: the outbox -----------------------------------------------------------


async def test_an_unreachable_main_computer_gets_the_recording_when_it_is_back(
    app, ctx, main_url, dead_url, laptop, sat, fake, laptop_token
):
    use_main(laptop, dead_url, laptop_token)
    lctx = laptop.state.ctx
    lctx.settings.encrypt_at_rest, lctx.master_key = True, new_key()  # sealed while it waits
    audio = pcm(0.5, 400)
    async with client(laptop, "admin-laptop") as c:
        r = await c.post(
            "/api/satellite/start", json={"mic_device": 1, "system_device": 2, "title": "Offline"}
        )
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["streaming"] is False and body["session_id"] is None
        fake.write("mic", audio)
        fake.write("system", audio)
        r = await c.post("/api/satellite/stop", json={"notes": "- typed on the train"})
        assert r.json()["outcome"] == "waiting"
        item = sat.outbox.items[body["local_id"]]
        await until(lambda: item.attempts == 1)
        folder = sat.outbox.folder(item.id)
        assert sorted(p.name for p in folder.iterdir()) == [
            "fake_mic.wav.enc",
            "fake_system.wav.enc",
            "manifest.json",
        ]
        assert audio not in (folder / "fake_mic.wav.enc").read_bytes()
        status = (await c.get("/api/satellite")).json()
        assert status["reachable"] is False
        (entry,) = status["outbox"]
        assert entry["id"] == body["local_id"] and entry["state"] == "waiting"
        assert entry["parts"] == 1 and entry["title"] == "Offline" and entry["last_error"]
        assert ctx.sessions.list_sessions() == []

    # The shell's tunnel is up again: it tells the backend, which hands the recording over now.
    async with client(laptop) as shell:
        r = await shell.put(
            "/api/system/main", json={"url": main_url, "token": laptop_token, "name": "Desktop"}
        )
        assert r.status_code == 200, r.text
    await until(lambda: outbox_folders(laptop) == [])

    (session,) = ctx.sessions.list_sessions()
    jobs = await jobs_done(ctx, session.id)
    assert [j.kind for j in jobs].count("transcribe") == 1
    session = ctx.sessions.get_session(session.id)
    assert session.name == "Offline" and session.notes == "- typed on the train"
    handed = [e for e in ctx.repo.events(session.id) if e["kind"] == "handed_over"]
    assert handed[0]["detail"]["handoff_id"] == body["local_id"]
    assert handed[0]["detail"]["replaced"] is False
    folder = ctx.settings.recordings_dir / session.id
    assert data_of(next(folder.glob("*_handoff_mic.wav"))) == audio
    nothing_here(laptop)


async def test_waiting_recordings_are_handed_over_after_a_restart(
    app, ctx, main_url, dead_url, laptop_settings, laptop, sat, fake, laptop_token
):
    use_main(laptop, dead_url, laptop_token)
    async with client(laptop, "admin-laptop") as c:
        r = await c.post("/api/satellite/start", json={"mic_device": 1, "title": "Stopped"})
        stopped = r.json()["local_id"]
        fake.write("mic", pcm(0.25, 10))
        await c.post("/api/satellite/stop")
        # A second one is cut off by a crash: no stop, no shutdown.
        r = await c.post("/api/satellite/start", json={"mic_device": 1, "title": "Cut off"})
        cut = r.json()["local_id"]
        fake.write("mic", pcm(0.25, 20))
    sat.active = None
    sat._worker.cancel()
    assert sorted(outbox_folders(laptop)) == sorted([stopped, cut])

    again = create_app(laptop_settings, keystore=MemoryKeyStore())
    actx = again.state.ctx
    actx.satellite = Satellite(actx, FakeCapture(auto=False, sample_rate=RATE))
    try:
        assert {i.state for i in actx.satellite.outbox.items.values()} == {"waiting", "recording"}
        use_main(again, main_url, laptop_token)
        await actx._start_satellite()  # what startup does
        await until(lambda: outbox_folders(again) == [])
    finally:
        await actx.satellite.shutdown()
    names = sorted(s.name for s in ctx.sessions.list_sessions())
    assert names == ["Cut off", "Stopped"]
    for s in ctx.sessions.list_sessions():
        await jobs_done(ctx, s.id)
    assert actx.sessions.list_sessions() == []


async def test_a_refusal_stops_the_retries(app, ctx, main_url, laptop, sat, fake, laptop_token):
    device = ctx.pairing.verify(laptop_token)
    ctx.pairing.revoke(device.id)  # the main computer forgot this laptop
    use_main(laptop, main_url, laptop_token)
    async with client(laptop, "admin-laptop") as c:
        r = await c.post("/api/satellite/start", json={"mic_device": 1})
        assert r.status_code == 200 and r.json()["streaming"] is False  # 401 there: kept here
        fake.write("mic", pcm(0.25, 10))
        await c.post("/api/satellite/stop")
        local_id = r.json()["local_id"]
        item = sat.outbox.items[local_id]
        await until(lambda: item.state == "failed")
        assert item.attempts == 0 and "token" in item.last_error
        sat.kick()  # the main computer is back: a refused item stays refused
        await asyncio.sleep(0.2)
        (entry,) = (await c.get("/api/satellite")).json()["outbox"]
        assert entry["state"] == "failed" and entry["last_error"]
    assert outbox_folders(laptop) == [local_id]  # kept, not deleted
    assert ctx.sessions.list_sessions() == []


async def test_a_busy_main_computer_is_tried_again(monkeypatch, laptop, sat, fake, dead_url):
    """409 and 429 are "later", like not reaching it at all."""
    calls = []

    async def busy(main, method, path, **kw):
        calls.append(path)
        raise satellite.MainError(409 if len(calls) == 1 else 429, "busy")

    monkeypatch.setattr(satellite, "_call", busy)
    use_main(laptop, dead_url, "t")
    await sat.start(1, None)
    fake.write("mic", pcm(0.1, 1))
    await sat.stop()
    item = next(iter(sat.outbox.items.values()))
    await until(lambda: item.attempts == 1)
    sat.kick()
    await until(lambda: item.attempts == 2)
    assert item.state == "waiting"


# ---- a way out for an item ---------------------------------------------------------------


async def test_a_refused_replace_goes_as_a_new_meeting(
    app, ctx, main_url, laptop, sat, fake, laptop_token
):
    """The stream had a gap, and the main computer transcribed its partial meeting before the
    complete audio arrived: the replace is refused for good, the item can go as a new one."""
    use_main(laptop, main_url, laptop_token)
    first, second, third = pcm(0.25, 100), pcm(0.25, 200), pcm(0.25, 300)
    async with client(laptop, "admin-laptop") as c:
        r = await c.post("/api/satellite/start", json={"mic_device": 1, "title": "Gappy"})
        sid, local_id = r.json()["session_id"], r.json()["local_id"]
        paths = main_wavs(ctx, sid)
        stream = sat.active.streams["mic"]
        fake.write("mic", first)
        await until(lambda: data_of(paths["mic"]) == first)
        stream.ws.transport.abort()
        fake.write("mic", second)  # may be lost with the connection
        await until(lambda: stream.drops == 1 and stream.connected)
        fake.write("mic", third)
        await until(lambda: data_of(paths["mic"]).endswith(third))
        # Stop pressed on the main computer, which transcribes what it has.
        async with client(app, laptop_token) as main_ui:
            r = await main_ui.post(f"/api/audio/stop/{sid}", json={"transcribe": True})
            assert r.status_code == 200
        fake.write("mic", pcm(0.1, 400))  # captured after that: the stream finds it closed
        await until(lambda: sat.active is None)
        item = sat.outbox.items[local_id]
        await until(lambda: item.state == "failed")
        assert "Transcription has started" in item.last_error and item.attempts == 0
        (entry,) = (await c.get("/api/satellite")).json()["outbox"]
        assert entry["state"] == "failed" and entry["replaces"] == sid

        r = await c.post(f"/api/satellite/outbox/{local_id}/new-meeting")
        assert r.status_code == 200, r.text
        assert r.json()["replaces"] is None and r.json()["state"] in ("waiting", "sending")
        await until(lambda: outbox_folders(laptop) == [])
        r = await c.post(f"/api/satellite/outbox/{local_id}/new-meeting")
        assert r.status_code == 404  # handed over: nothing left here

    sessions = ctx.sessions.list_sessions()
    assert len(sessions) == 2
    new = next(s for s in sessions if s.id != sid)
    handed = [e for e in ctx.repo.events(new.id) if e["kind"] == "handed_over"]
    assert handed[0]["detail"]["handoff_id"] == f"{local_id}-new"
    assert handed[0]["detail"]["replaced"] is False
    for s in sessions:
        await jobs_done(ctx, s.id)
    # The new meeting has everything the laptop recorded; the streamed one is untouched.
    folder = ctx.settings.recordings_dir / new.id
    assert data_of(next(folder.glob("*_handoff_mic.wav"))) == first + second + third + pcm(0.1, 400)
    assert not [e for e in ctx.repo.events(sid) if e["kind"] == "handed_over"]
    nothing_here(laptop)


async def test_a_waiting_or_refused_recording_can_be_discarded(
    laptop, sat, fake, dead_url, laptop_token
):
    use_main(laptop, dead_url, laptop_token)
    async with client(laptop, "admin-laptop") as c:
        r = await c.post("/api/satellite/start", json={"mic_device": 1})
        local_id = r.json()["local_id"]
        # Never while it is being recorded.
        assert (await c.delete(f"/api/satellite/outbox/{local_id}")).status_code == 409
        r = await c.post(f"/api/satellite/outbox/{local_id}/new-meeting")
        assert r.status_code == 409
        fake.write("mic", pcm(0.1, 5))
        await c.post("/api/satellite/stop")
        item = sat.outbox.items[local_id]
        await until(lambda: item.attempts == 1)
        r = await c.delete(f"/api/satellite/outbox/{local_id}")
        assert r.status_code == 200 and r.json() == {"id": local_id}
        assert (await c.get("/api/satellite")).json()["outbox"] == []
        assert (await c.delete(f"/api/satellite/outbox/{local_id}")).status_code == 404
    assert outbox_folders(laptop) == []
    # A refused one too.
    await sat.start(1, None)
    fake.write("mic", pcm(0.1, 5))
    await sat.stop()
    item = next(iter(sat.outbox.items.values()))
    await until(lambda: item.attempts == 1)
    item.state, item.last_error = "failed", "Missing or invalid API token"
    sat.discard(item.id)
    assert outbox_folders(laptop) == []


# ---- only this computer ------------------------------------------------------------------


async def test_the_routes_are_this_computers_only(laptop, sat):
    lctx = laptop.state.ctx
    _, other = lctx.pairing.add_desktop("Other", "f" * 64)
    for token in ("admin-laptop", other):
        async with client(laptop, token, peer=TUNNEL) as c:
            assert (await c.get("/api/satellite")).status_code == 403
            r = await c.post("/api/satellite/start", json={"mic_device": 1})
            assert r.status_code == 403
    async with client(laptop) as no_token:
        assert (await no_token.get("/api/satellite")).status_code == 401
    async with client(laptop, "admin-laptop") as c:
        r = await c.get("/api/satellite")
        assert r.status_code == 200
        assert r.json() == {"main": None, "reachable": False, "recording": None, "outbox": []}
        assert (await c.post("/api/satellite/start", json={})).status_code == 400
        assert (await c.post("/api/satellite/stop")).status_code == 404
