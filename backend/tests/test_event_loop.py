"""Slow or stuck work kept off the event loop, and what happens when it fails or is cut short
(audio/capture.py, storage/crypto.py plaintext_async, api/context.py shutdown)."""

import asyncio
import os
import subprocess

import pytest
from fastapi.testclient import TestClient

from mnemosyne.api.routes import audio as audio_routes
from mnemosyne.audio import capture
from mnemosyne.storage import crypto


def test_pipewire_not_answering_is_a_clear_error(monkeypatch):
    def hang(*args, **kwargs):
        raise subprocess.TimeoutExpired(args[0], 5)

    monkeypatch.setattr(capture.subprocess, "run", hang)
    with pytest.raises(RuntimeError, match="not answering"):
        capture.list_devices()


def test_recording_start_reports_pipewire_trouble(client, monkeypatch):
    async def fails(device_ids, output_dir, **_):
        raise RuntimeError("PipeWire is not answering (pw-dump timed out)")

    monkeypatch.setattr(audio_routes, "start_recording", fails)
    r = client.post("/api/audio/start", json={"device_ids": [1]})
    assert r.status_code == 503
    assert "not answering" in r.json()["detail"]


def _encrypted(tmp_path):
    key = os.urandom(32)
    src = tmp_path / "meeting.ogg"
    src.write_bytes(b"audio" * 1000)
    return crypto.encrypt_file(src, key), key


def _copies(monkeypatch, tmp_path):
    scratch = tmp_path / "scratch"
    monkeypatch.setattr(crypto, "_scratch_dir", lambda: scratch.mkdir(exist_ok=True) or scratch)
    return scratch


@pytest.mark.anyio
async def test_plaintext_async_reads_and_removes_its_copy(tmp_path, monkeypatch):
    scratch = _copies(monkeypatch, tmp_path)
    path, key = _encrypted(tmp_path)
    async with crypto.plaintext_async(path, key) as plain:
        assert plain.read_bytes() == b"audio" * 1000
    assert list(scratch.iterdir()) == []


@pytest.mark.anyio
async def test_plaintext_async_cancelled_while_copying_leaves_no_copy(tmp_path, monkeypatch):
    scratch = _copies(monkeypatch, tmp_path)
    path, key = _encrypted(tmp_path)
    real = crypto.EncryptedFile.write_plain

    def slow(self, out):
        import time

        time.sleep(0.3)
        return real(self, out)

    monkeypatch.setattr(crypto.EncryptedFile, "write_plain", slow)

    async def use():
        async with crypto.plaintext_async(path, key):
            pytest.fail("cancelled before the copy was made")

    task = asyncio.create_task(use())
    await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    await asyncio.sleep(0.5)  # the copying thread finishes, then its copy is removed
    assert list(scratch.iterdir()) == []


def test_shutdown_stops_recordings_still_running(app, fake_pipewire):
    with TestClient(app) as c:
        sid = c.post("/api/audio/start", json={"device_ids": [1, 2]}).json()["session_id"]
        procs = [p.process for p in app.state.ctx.active_recordings[sid].processes]
        assert all(p.returncode is None for p in procs)
    assert all(p.returncode is not None for p in procs)  # pw-record does not outlive us


# ---- startup that survives what it finds ----------------------------------------------


@pytest.fixture
def config_file(tmp_path, monkeypatch):
    from mnemosyne import config

    path = tmp_path / "config.toml"
    monkeypatch.setenv("MNEMOSYNE_CONFIG_FILE", str(path))
    monkeypatch.setenv("MNEMOSYNE_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setattr(config, "STARTUP_PROBLEMS", [])
    return path


def test_a_settings_file_that_is_not_toml_is_set_aside(config_file):
    from mnemosyne import config

    config_file.write_text('auto_transcribe = false\nobsidian_vault_path = "unterminated\n')
    settings = config.load_settings()
    assert settings.auto_transcribe is True  # defaults
    assert config_file.with_name("config.toml.broken").read_text().startswith("auto_transcribe")
    assert "could not be read" in config.STARTUP_PROBLEMS[0]


def test_only_the_settings_that_no_longer_validate_are_reset(config_file):
    from mnemosyne import config

    config_file.write_text('auto_transcribe = false\nsetup_complete = "maybe"\n')
    settings = config.load_settings()
    assert settings.auto_transcribe is False  # kept
    assert settings.setup_complete is False  # reset
    assert "setup_complete" in config.STARTUP_PROBLEMS[0]
    assert '"maybe"' not in config_file.read_text()


def test_a_failing_startup_step_is_reported_not_fatal(app, monkeypatch):
    from mnemosyne.services import recovery

    def boom(_app):
        raise OSError("disk on fire")

    monkeypatch.setattr(recovery, "recover_interrupted", boom)
    with TestClient(app) as c:
        assert c.get("/health").status_code == 200
        problems = c.get("/api/system").json()["problems"]
    assert problems == ["Recovering interrupted recordings failed at startup: disk on fire"]


@pytest.mark.anyio
async def test_finished_jobs_are_forgotten_beyond_a_limit(monkeypatch):
    from mnemosyne.events import EventBus
    from mnemosyne.jobs import JobManager

    jobs = JobManager(EventBus())
    monkeypatch.setattr(JobManager, "KEEP_FINISHED", 3)

    async def quick(ctx):
        return {}

    async def forever(ctx):
        await asyncio.Event().wait()

    running = jobs.submit("live", forever)
    done = [jobs.submit("x", quick) for _ in range(6)]
    for j in done:
        await jobs.wait(j.id)
    jobs.submit("x", quick)
    kept = {j.id for j in jobs.list()}
    assert running.id in kept  # never a job still running
    assert {j.id for j in done[-3:]} <= kept and not {j.id for j in done[:3]} & kept
    await jobs.shutdown()


def test_a_stuck_subscriber_is_logged_rarely(caplog):
    from mnemosyne.events import EventBus

    bus = EventBus(maxsize=1)
    bus.subscribe()
    for _ in range(500):
        bus.publish({"type": "level"})
    assert len([r for r in caplog.records if "dropped" in r.getMessage()]) == 2  # 1st, 100th
