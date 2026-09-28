"""Backups and restores (services/backup.py, routes/backup.py)."""

import os
import tarfile
import time
from pathlib import Path

from fastapi.testclient import TestClient

from mnemosyne.api.app import create_app
from mnemosyne.config import Settings
from mnemosyne.services.backup import PREFIX, due, prune
from mnemosyne.storage.crypto import MemoryKeyStore


def _wait(client, job_id, timeout=20):
    deadline = time.monotonic() + timeout
    while (job := client.get(f"/api/jobs/{job_id}").json())["status"] not in (
        "completed",
        "failed",
        "cancelled",
    ):
        assert time.monotonic() < deadline
        time.sleep(0.05)
    return job


def _meeting(ctx, name="Budget review") -> str:
    session = ctx.sessions.create_session(name)
    folder = ctx.settings.recordings_dir / session.id
    folder.mkdir(parents=True)
    audio = folder / "a_mixed.ogg"
    audio.write_bytes(b"OggS" + os.urandom(2000))
    ctx.sessions.set_audio(session.id, str(audio), [])
    (folder / "leftover_device_1.wav").write_bytes(b"RIFF")  # a capture in progress: skipped
    return session.id


def _back_up(client) -> dict:
    job = client.post("/api/backup").json()
    done = _wait(client, job["id"])
    assert done["status"] == "completed", done["error"]
    return done["result"]


def test_back_up_and_restore(settings, keystore):
    app = create_app(settings, keystore=keystore)
    ctx = app.state.ctx
    sid = _meeting(ctx)
    audio = Path(ctx.sessions.get_session(sid).audio_file).read_bytes()
    ctx.settings.summary_style = "brief"
    with TestClient(app) as client:
        info = _back_up(client)
        assert info["sessions"] == 1 and not info["encrypted"]
        backup = settings.data_dir.parent / "backups" / info["name"]
        with tarfile.open(backup) as tar:
            names = tar.getnames()
        assert names[:3] == ["manifest.json", "settings.json", "mnemosyne.db"]
        assert f"recordings/{sid}/a_mixed.ogg" in names
        assert not any(n.endswith(".wav") for n in names)
        status = client.get("/api/backup").json()
        assert [b["name"] for b in status["backups"]] == [info["name"]]

        # Life goes on: the meeting is renamed, a setting and a secret change.
        client.patch(f"/api/sessions/{sid}", json={"name": "Renamed"})
        ctx.settings.summary_style = "detailed"
        ctx.settings.openai_api_key = "sk-new"
        res = client.post("/api/backup/restore", json={"name": info["name"]})
        assert res.status_code == 200 and res.json()["restart_required"] is True
        assert client.get("/api/backup").json()["restore_pending"] == info["name"]
        assert client.post("/api/backup/restore", json={"name": "../etc"}).status_code == 400

    restarted = create_app(settings, keystore=keystore)  # the restore happens here
    with TestClient(restarted) as client:
        session = client.get(f"/api/sessions/{sid}").json()
        assert session["name"] == "Budget review"
        assert Path(session["audio_file"]).read_bytes() == audio
        status = client.get("/api/backup").json()
        assert status["restore_pending"] is None
        result = status["last_restore"]
        assert result["name"] == info["name"] and result["error"] is None
    assert settings.summary_style == "brief"  # the backup's settings...
    assert settings.openai_api_key == "sk-new"  # ...but not its (absent) secrets
    kept = Path(result["kept_in"])  # the data from before the restore, not deleted
    assert (kept / "mnemosyne.db").exists() and (kept / "recordings" / sid).is_dir()


def test_an_encrypted_backup_opens_elsewhere_with_the_recovery_code(
    settings, keystore, tmp_path, monkeypatch
):
    app = create_app(settings, keystore=keystore)
    sid = _meeting(app.state.ctx)
    with TestClient(app) as client:
        code = client.post("/api/encryption/enable").json()["recovery_code"]
        info = _back_up(client)
        assert info["encrypted"] is True
    with tarfile.open(settings.data_dir.parent / "backups" / info["name"]) as tar:
        db = tar.extractfile("mnemosyne.db").read()
    assert b"Budget review" not in db  # as encrypted as the data

    # A new machine: its own config and data folder, an empty keyring, the same backup folder.
    monkeypatch.setenv("MNEMOSYNE_CONFIG_FILE", str(tmp_path / "new-machine.toml"))
    new = Settings(data_dir=tmp_path / "new-machine", backup_dir=settings.backup_dir)
    with TestClient(create_app(new, keystore=MemoryKeyStore())) as client:
        assert client.post("/api/backup/restore", json={"name": info["name"]}).status_code == 200
    empty = MemoryKeyStore()
    with TestClient(create_app(new, keystore=empty)) as client:
        assert client.get("/api/encryption").json() == {"enabled": True, "locked": True}
        client.post("/api/encryption/unlock", json={"recovery_code": code})
        session = client.get(f"/api/sessions/{sid}").json()
        assert session["name"] == "Budget review"
        # Its audio paths now point into this machine's recordings folder, and play.
        assert session["audio_file"].startswith(str(new.recordings_dir))
        assert client.get(f"/api/audio/file/{sid}").content.startswith(b"OggS")


def test_a_broken_backup_leaves_the_data_as_it_was(settings, keystore):
    app = create_app(settings, keystore=keystore)
    sid = _meeting(app.state.ctx)
    with TestClient(app) as client:
        info = _back_up(client)
        client.post("/api/backup/restore", json={"name": info["name"]})
    (settings.data_dir.parent / "backups" / info["name"]).write_bytes(b"not a tar")
    with TestClient(create_app(settings, keystore=keystore)) as client:
        assert client.get(f"/api/sessions/{sid}").json()["name"] == "Budget review"
        result = client.get("/api/backup").json()["last_restore"]
        assert result["error"] and result["kept_in"] == ""
    assert not list(settings.data_dir.glob("pre-restore-*"))


def test_no_backup_while_recording(client, fake_pipewire):
    client.post("/api/audio/start", json={"device_ids": [1]})
    assert client.post("/api/backup").status_code == 409
    assert client.post("/api/backup/restore", json={"name": f"{PREFIX}x.tar"}).status_code == 409


def test_schedule_and_pruning(tmp_path):
    s = Settings(data_dir=tmp_path / "d", backup_dir=str(tmp_path / "b"), backup_keep=2)
    assert not due(s)  # automatic backups are off by default
    s.backup_interval_days = 7
    assert due(s)  # none yet
    folder = tmp_path / "b"
    folder.mkdir()
    for day in (1, 2, 3):
        f = folder / f"{PREFIX}2026090{day}-120000.tar"
        f.write_bytes(b"")
        os.utime(f, (day * 86400, day * 86400))
    assert not due(s, now=3 * 86400 + 6 * 86400)
    assert due(s, now=3 * 86400 + 7 * 86400)
    (folder / "unrelated.tar").write_bytes(b"")
    assert prune(s) == [f"{PREFIX}20260901-120000.tar"]
    assert sorted(p.name for p in folder.iterdir())[-1] == "unrelated.tar"
