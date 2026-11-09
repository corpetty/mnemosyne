"""Encryption at rest: the file format, turning it on and off, locking and unlocking."""

import shutil
import subprocess
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from mnemosyne.api.app import create_app
from mnemosyne.models.session import Recording, Session
from mnemosyne.models.transcript import TranscriptSegment
from mnemosyne.services import notes_media
from mnemosyne.storage import crypto
from mnemosyne.storage.crypto import (
    CHUNK,
    EncryptedFile,
    MemoryKeyStore,
    decrypt_file,
    derive,
    encrypt_file,
    parse_recovery_code,
    plaintext,
    recovery_code,
)
from tests.test_notes_media import sha, webp

needs_ffmpeg = pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="needs ffmpeg")
KEY = bytes(range(32))

def test_database_made_by_the_linux_wheel_opens_with_the_configured_key(tmp_path):
    """tests/vectors/sqlcipher_v0_6_0.db is a SQLCipher database made with sqlcipher3-binary 0.6.0
    (the Linux wheel) and KEY, the way storage.sqlite._connect keys one:

        c = sqlcipher3.connect(path)
        c.execute("PRAGMA key = \"x'<KEY hex>'\"")
        c.execute("CREATE TABLE note (id INTEGER PRIMARY KEY, body TEXT NOT NULL)")
        c.execute("INSERT INTO note (id, body) VALUES (1, 'Mnemosyne remembers')")

    Whatever wheel provides `sqlcipher3` (macOS builds its own) must still read it, or a user's
    encrypted library would be locked out after a switch."""
    from mnemosyne.storage.sqlite import _connect

    db = tmp_path / "copy.db"
    shutil.copy(Path(__file__).parent / "vectors" / "sqlcipher_v0_6_0.db", db)
    conn = _connect(db, KEY)
    try:
        assert conn.execute("SELECT body FROM note WHERE id = 1").fetchone()["body"] == (
            "Mnemosyne remembers"
        )
    finally:
        conn.close()
    with pytest.raises(Exception):  # a wrong key does not open it
        _connect(db, bytes(32)).close()


# ---- the file format -------------------------------------------------------------


@pytest.mark.parametrize("size", [0, 1, CHUNK - 1, CHUNK, CHUNK + 1, 3 * CHUNK + 17])
def test_files_round_trip_and_read_any_range(tmp_path, size):
    data = bytes((i * 7) % 256 for i in range(size))
    src = tmp_path / "a.ogg"
    src.write_bytes(data)
    enc = encrypt_file(src, KEY)
    assert enc.name == "a.ogg.enc" and not src.exists()
    if size >= 64:  # the plaintext does not appear in the file
        assert data[:64] not in enc.read_bytes()
    f = EncryptedFile(enc, KEY)
    assert f.size == size and f.read() == data
    for start, end in [(0, 10), (CHUNK - 5, CHUNK + 5), (size // 2, size), (size, size + 9)]:
        assert f.read(start, end) == data[start:end]
    assert decrypt_file(enc, KEY).read_bytes() == data


def test_tampering_and_truncation_are_detected(tmp_path):
    src = tmp_path / "a.ogg"
    src.write_bytes(b"x" * (2 * CHUNK + 5))
    enc = encrypt_file(src, KEY)
    raw = bytearray(enc.read_bytes())
    raw[crypto.HEADER.size + 40] ^= 1
    (tmp_path / "flipped.enc").write_bytes(raw)
    with pytest.raises(Exception):  # noqa: B017 - InvalidTag
        EncryptedFile(tmp_path / "flipped.enc", KEY).read()
    stored = crypto.NONCE + CHUNK + crypto.TAG
    (tmp_path / "short.enc").write_bytes(enc.read_bytes()[: crypto.HEADER.size + stored])
    with pytest.raises(Exception):  # noqa: B017 - any failure, never silent short data
        EncryptedFile(tmp_path / "short.enc", KEY).read()
    with pytest.raises(Exception):  # noqa: B017
        EncryptedFile(enc, bytes(32)).read()  # wrong key


def test_recovery_codes_and_derived_keys():
    code = recovery_code(KEY)
    assert parse_recovery_code(code) == KEY
    assert parse_recovery_code(code.lower().replace("-", " ")) == KEY
    with pytest.raises(ValueError):
        parse_recovery_code("not-a-code")
    assert derive(KEY, "db") != derive(KEY, "files") != KEY


def test_plaintext_view_is_private_and_removed(tmp_path):
    src = tmp_path / "a.ogg"
    src.write_bytes(b"secret audio")
    enc = encrypt_file(src, KEY)
    with plaintext(enc, KEY) as p:
        assert p.suffix == ".ogg" and p.read_bytes() == b"secret audio"
        seen = p
    assert not seen.exists()
    with plaintext(tmp_path / "plain.wav", None) as p:
        assert p == tmp_path / "plain.wav"


# ---- the app ------------------------------------------------------------------------


def _audio(path: Path, seconds: int = 3) -> Path:
    subprocess.run(
        [
            "ffmpeg",
            "-loglevel",
            "error",
            "-f",
            "lavfi",
            "-i",
            f"sine=f=330:d={seconds}",
            "-c:a",
            "libopus",
            str(path),
        ],
        check=True,
    )
    return path


def _meeting(ctx) -> Session:
    folder = ctx.settings.recordings_dir / "m1"
    folder.mkdir(parents=True)
    mixed, mic = _audio(folder / "mixed.ogg"), _audio(folder / "mic.ogg")
    (folder / "clips").mkdir()
    shutil.copy(mixed, folder / "clips" / "0-0.ogg")
    return ctx.repo.save(
        Session(
            id="m1",
            name="Quarterly budget",
            audio_file=str(mixed),
            recordings=[Recording(source="mic", device_id=1, device_name="Mic", path=str(mic))],
            transcript=[
                TranscriptSegment(text="the budget is tight", speaker="Ana", start=0, end=2)
            ],
        )
    )


@needs_ffmpeg
def test_turn_on_lock_unlock_turn_off(settings, keystore, tmp_path):
    app = create_app(settings, keystore=keystore)
    ctx = app.state.ctx
    original = _meeting(ctx)
    audio_before = Path(original.audio_file).read_bytes()

    with TestClient(app) as client:
        on = client.post("/api/encryption/enable").json()
        assert on["files"] == 3 and on["errors"] == []  # mix, mic, clip
        code = on["recovery_code"]
        assert keystore.get() is not None and ctx.settings.encrypt_at_rest
        session = client.get("/api/sessions/m1").json()
        assert session["audio_file"].endswith(".ogg.enc")
        assert all(r["path"].endswith(".enc") for r in session["recordings"])
        assert not list(settings.recordings_dir.rglob("*.ogg"))  # no plaintext audio left
        assert b"budget" not in settings.db_path.read_bytes()
        assert client.get("/api/search", params={"q": "budget"}).json()  # search still works
        # Playback, whole and by range, is the original audio.
        assert client.get("/api/audio/file/m1").content == audio_before
        part = client.get("/api/audio/file/m1", headers={"Range": "bytes=10-99"})
        assert part.status_code == 206 and part.content == audio_before[10:100]
        assert part.headers["content-range"] == f"bytes 10-99/{len(audio_before)}"
        assert client.post("/api/encryption/enable").status_code == 400  # already on

    # The keyring loses the key: locked until the recovery code arrives.
    empty = MemoryKeyStore()
    app = create_app(settings, keystore=empty)
    with TestClient(app) as client:
        assert client.get("/api/encryption").json() == {"enabled": True, "locked": True}
        assert client.get("/api/sessions").status_code == 423
        wrong = recovery_code(bytes(32))
        assert (
            client.post("/api/encryption/unlock", json={"recovery_code": wrong}).status_code == 400
        )
        ok = client.post("/api/encryption/unlock", json={"recovery_code": code})
        assert ok.json() == {"enabled": True, "locked": False}
        assert empty.get() == parse_recovery_code(code)  # back in the keyring
        assert client.get("/api/sessions/m1").json()["name"] == "Quarterly budget"

        off = client.post("/api/encryption/disable").json()
        assert off == {"enabled": False, "locked": False}
        assert empty.get() is None
        session = client.get("/api/sessions/m1").json()
        assert session["audio_file"].endswith(".ogg")
        assert Path(session["audio_file"]).read_bytes() == audio_before
        assert (settings.recordings_dir / "m1" / "clips" / "0-0.ogg").exists()
    import sqlite3  # plain SQLite again

    rows = sqlite3.connect(settings.db_path).execute("SELECT name FROM sessions").fetchall()
    assert rows == [("Quarterly budget",)]


@needs_ffmpeg
def test_transcription_reads_a_private_copy_that_is_removed(client, ctx, fake_engine):
    from tests.conftest import drain_until_job

    _meeting(ctx)
    ctx.settings.per_source_transcription = False
    client.post("/api/encryption/enable")
    with client.websocket_connect("/ws") as ws:
        assert ws.receive_json()["type"] == "hello"
        job = client.post("/api/sessions/m1/transcribe").json()
        drain_until_job(ws, job["id"])
    (path,) = fake_engine.transcribed_paths
    assert path.endswith(".ogg") and not path.endswith(".enc")
    assert not Path(path).exists()  # removed after the job


@needs_ffmpeg
def test_new_recordings_are_sealed_at_stop(client, ctx, fake_pipewire, monkeypatch):
    from mnemosyne.api.routes import audio as audio_routes

    async def stop_recording(session):  # real audio this time, so it can be encrypted
        session.is_recording = False
        return [_audio(p.output_path.with_suffix(".ogg")) for p in session.processes]

    def mix_audio_files(inputs, output):
        return shutil.copy(inputs[0], Path(output).with_suffix(".ogg"))

    monkeypatch.setattr(audio_routes, "stop_recording", stop_recording)
    monkeypatch.setattr(audio_routes, "mix_audio_files", lambda i, o: Path(mix_audio_files(i, o)))
    client.post("/api/encryption/enable")
    sid = client.post("/api/audio/start", json={"device_ids": [1]}).json()["session_id"]
    from tests.conftest import stop_and_finish

    stop_and_finish(client, sid, {"transcribe": False})
    session = client.get(f"/api/sessions/{sid}").json()
    assert session["audio_file"].endswith(".enc")
    assert [r["path"][-4:] for r in session["recordings"]] == [".enc"]


def test_settings_cannot_flip_encryption(client, ctx):
    client.put("/api/settings", json={"encrypt_at_rest": True, "encryption_check": "x"})
    assert ctx.settings.encrypt_at_rest is False and ctx.settings.encryption_check == ""


def test_no_keyring_means_nothing_changes(client, ctx, keystore, monkeypatch):
    def fail(key):
        raise RuntimeError("No system keyring to keep the key in")

    monkeypatch.setattr(keystore, "set", fail)
    r = client.post("/api/encryption/enable")
    assert r.status_code == 400 and "keyring" in r.json()["detail"]
    assert ctx.settings.encrypt_at_rest is False and not ctx.repo.encrypted


def test_stale_copies_of_dead_backends_are_removed(tmp_path, monkeypatch):
    import os

    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
    mine = crypto._scratch_dir()
    (mine / "in-use.ogg").write_bytes(b"x")
    dead = tmp_path / "mnemosyne" / "999999999"
    dead.mkdir()
    (dead / "left.ogg").write_bytes(b"x")
    crypto.clean_scratch()
    assert not dead.exists()
    assert (mine / "in-use.ogg").exists() and mine.name == str(os.getpid())


@needs_ffmpeg
def test_quotes_from_encrypted_meetings(client, ctx, tmp_path):
    _meeting(ctx)
    client.post("/api/encryption/enable")
    body = client.post("/api/sessions/m1/clip", json={"first_idx": 0, "last_idx": 0}).json()
    stored = ctx.settings.recordings_dir / "m1" / "clips" / f"{body['clip']['id']}.ogg.enc"
    assert stored.exists() and not stored.with_suffix("").exists()  # cut, then sealed
    got = client.get(f"/api/sessions/m1/clips/{body['clip']['id']}")
    assert got.status_code == 200 and got.content[:4] == b"OggS"
    target = tmp_path / "quote.ogg"
    client.post(f"/api/sessions/m1/clips/{body['clip']['id']}/save", json={"path": str(target)})
    assert target.read_bytes() == got.content


def test_readers_wait_while_the_database_is_swapped(client, ctx, monkeypatch):
    """Turning encryption on rewrites the database; a request or the search indexer reading
    meanwhile waits for it instead of failing on a closed database."""
    import threading

    from mnemosyne.services import encryption

    session = ctx.sessions.create_session("during the swap")
    seen: dict = {}
    real = encryption.convert_database

    def convert(*args):
        def read():
            try:
                seen["stamp"] = ctx.repo.session_stamp(session.id)
            except Exception as e:  # noqa: BLE001
                seen["error"] = e

        reader = threading.Thread(target=read)
        reader.start()
        reader.join(0.2)
        seen["waited"] = reader.is_alive()
        real(*args)
        seen["reader"] = reader

    monkeypatch.setattr(encryption, "convert_database", convert)
    assert client.post("/api/encryption/enable").status_code == 200
    seen["reader"].join(5)
    assert seen["waited"] and "error" not in seen and seen["stamp"]


def test_turning_encryption_on_and_off_converts_the_notes_images(settings, keystore):
    app = create_app(settings, keystore=keystore)
    ctx = app.state.ctx
    sids = [ctx.repo.save(Session(name=n)).id for n in ("One", "Two")]
    data = [webp(pad=b"a" * 100), webp(pad=b"b" * 100)]
    for sid, blob in zip(sids, data, strict=True):
        notes_media.store(ctx, sid, sha(blob), blob)
    folder = settings.notes_media_dir / sids[0]
    (folder / f"{sha(data[1])}.webp.part").write_bytes(b"half")  # an upload in progress

    def files(sid):
        return sorted(p.name for p in (settings.notes_media_dir / sid).iterdir())

    with TestClient(app) as client:
        on = client.post("/api/encryption/enable").json()
        assert on["files"] == 2 and on["errors"] == []
        for sid, blob in zip(sids, data, strict=True):
            sealed = settings.notes_media_dir / sid / f"{sha(blob)}.webp.enc"
            assert sealed.is_file() and blob not in sealed.read_bytes()
            assert f"{sha(blob)}.webp" not in files(sid)
            assert notes_media.read(ctx, sid, sha(blob)) == blob  # reads through the key
        assert f"{sha(data[1])}.webp.part" in files(sids[0])  # untouched
        (folder / f"{sha(data[1])}.webp.part").unlink()

        assert client.post("/api/encryption/disable").status_code == 200
        for sid, blob in zip(sids, data, strict=True):
            assert files(sid) == [f"{sha(blob)}.webp"]
            assert notes_media.read(ctx, sid, sha(blob)) == blob
