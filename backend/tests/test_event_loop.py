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
