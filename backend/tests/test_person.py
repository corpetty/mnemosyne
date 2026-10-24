"""The per-person signing key: files, signatures, and following encryption at rest."""

import re
import stat

import pytest
from fastapi.testclient import TestClient

from mnemosyne.api.app import create_app
from mnemosyne.services.person import PersonKeys, PersonLocked
from mnemosyne.storage.crypto import MemoryKeyStore, derive, new_key


def keys(tmp_path, master=None, encrypted=False) -> PersonKeys:
    return PersonKeys(tmp_path, lambda: master, lambda: encrypted)


def test_create_then_load_keeps_the_key(tmp_path):
    first = keys(tmp_path)
    first.ensure()
    path = tmp_path / "person.key"
    assert re.fullmatch(r"[0-9a-f]{64}", path.read_text())
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    again = keys(tmp_path)
    assert again.public_hex == first.public_hex
    assert len(first.public_hex) == 64
    assert not list(tmp_path.glob("*.part")) and not (tmp_path / "person.key.enc").exists()


def test_pid_and_fingerprint_formats(tmp_path):
    k = keys(tmp_path)
    assert k.pid == "p:" + k.public_hex[:16]
    assert re.fullmatch(r"([A-Z2-7]{4} ){7}[A-Z2-7]{4}", k.fingerprint)
    assert k.fingerprint == keys(tmp_path).fingerprint


def test_sign_and_verify(tmp_path):
    k = keys(tmp_path)
    sig = k.sign(b"hello")
    assert re.fullmatch(r"[0-9a-f]{128}", sig)
    assert PersonKeys.verify(k.public_hex, sig, b"hello")
    assert not PersonKeys.verify(k.public_hex, sig, b"hellp")
    other = keys(tmp_path / "other")
    assert not PersonKeys.verify(other.public_hex, sig, b"hello")
    for bad in ("", "zz", "g" * 64, "0" * 63, "00" * 64):
        assert not PersonKeys.verify(bad, sig, b"hello")
        assert not PersonKeys.verify(k.public_hex, bad, b"hello")
    assert not PersonKeys.verify(None, sig, b"hello")  # type: ignore[arg-type]
    assert not PersonKeys.verify(k.public_hex, " " * 128, b"hello")


def test_canonical_is_stable():
    a = {"b": 1, "a": {"y": [1, 2], "x": "é"}}
    b = {"a": {"x": "é", "y": [1, 2]}, "b": 1}
    assert PersonKeys.canonical(a) == PersonKeys.canonical(b)
    assert PersonKeys.canonical(a) == '{"a":{"x":"é","y":[1,2]},"b":1}'.encode()
    assert "日本".encode() in PersonKeys.canonical({"n": "日本"})


def test_rotate_and_replace(tmp_path):
    k = keys(tmp_path)
    old = k.public_hex
    k.rotate()
    assert k.public_hex != old
    assert keys(tmp_path).public_hex == k.public_hex  # the new one is what is on disk
    assert not PersonKeys.verify(old, k.sign(b"x"), b"x")

    donor = keys(tmp_path / "donor")
    k.replace(donor.export_private().upper())
    assert k.public_hex == donor.public_hex
    assert keys(tmp_path).public_hex == donor.public_hex
    for bad in ("", "nothex", "00" * 31, "00" * 33, "zz" * 32):
        with pytest.raises(ValueError):
            k.replace(bad)
    assert k.public_hex == donor.public_hex  # a rejected key changes nothing


def test_sealed_file_and_lock(tmp_path):
    master = new_key()
    k = keys(tmp_path, master, encrypted=True)
    pub = k.public_hex
    assert (tmp_path / "person.key.enc").is_file() and not (tmp_path / "person.key").exists()
    assert stat.S_IMODE((tmp_path / "person.key.enc").stat().st_mode) == 0o600
    assert keys(tmp_path, master, True).public_hex == pub
    raw = (tmp_path / "person.key.enc").read_bytes()
    assert k._raw(k._get()) not in raw and k._raw(k._get()).hex().encode() not in raw
    # the documented format: 12-byte nonce, AES-GCM under derive(master, "person"), fixed AAD
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    plain = AESGCM(derive(master, "person")).decrypt(raw[:12], raw[12:], b"mnemosyne/person/1")
    assert plain.hex() == k.export_private()

    locked = keys(tmp_path, None, True)
    with pytest.raises(PersonLocked):
        locked.sign(b"x")
    with pytest.raises(PersonLocked):
        locked.ensure()
    wrong = keys(tmp_path, new_key(), True)
    with pytest.raises(Exception):  # noqa: B017 - InvalidTag
        wrong.sign(b"x")
    # a loaded key stays unusable while locked
    state = {"master": master}
    live = PersonKeys(tmp_path, lambda: state["master"], lambda: True)
    live.sign(b"x")
    state["master"] = None
    with pytest.raises(PersonLocked):
        live.sign(b"x")


def test_seal_and_unseal_convert_the_file(tmp_path):
    master = new_key()
    k = keys(tmp_path)
    pub = k.public_hex
    k.seal(master)
    assert (tmp_path / "person.key.enc").is_file() and not (tmp_path / "person.key").exists()
    sealed = PersonKeys(tmp_path, lambda: master, lambda: True)
    assert sealed.public_hex == pub
    sealed.unseal()
    assert (tmp_path / "person.key").is_file() and not (tmp_path / "person.key.enc").exists()
    assert stat.S_IMODE((tmp_path / "person.key").stat().st_mode) == 0o600
    assert keys(tmp_path).public_hex == pub
    empty = tmp_path / "none"
    empty.mkdir()
    keys(empty).seal(master)  # no key yet: nothing to do
    assert not list(empty.iterdir())


def test_an_interrupted_conversion_is_tidied_without_losing_the_key(tmp_path):
    master = new_key()
    k = keys(tmp_path)
    pub = k.public_hex
    plain = (tmp_path / "person.key").read_text()
    # encryption on, sealed file written, plain not yet removed
    PersonKeys(tmp_path, lambda: master, lambda: True).seal(master)
    (tmp_path / "person.key").write_text(plain)
    assert PersonKeys(tmp_path, lambda: master, lambda: True).public_hex == pub
    assert not (tmp_path / "person.key").exists()
    # encryption on but the key is still plain (sealing failed): sealed on the next load
    (tmp_path / "person.key.enc").unlink()
    (tmp_path / "person.key").write_text(plain)
    assert PersonKeys(tmp_path, lambda: master, lambda: True).public_hex == pub
    assert (tmp_path / "person.key.enc").is_file() and not (tmp_path / "person.key").exists()


# ---- in the app ---------------------------------------------------------------------


def test_app_makes_the_key_and_enabling_encryption_seals_it(settings, keystore):
    app = create_app(settings, keystore=keystore)
    ctx = app.state.ctx
    data = settings.data_dir
    pub = ctx.person.public_hex
    assert (data / "person.key").is_file()
    with TestClient(app) as client:
        on = client.post("/api/encryption/enable").json()
        assert on["errors"] == []
        assert (data / "person.key.enc").is_file() and not (data / "person.key").exists()
        assert ctx.person.public_hex == pub
        code = on["recovery_code"]

    # the keyring lost the key: locked, no signing, until the recovery code arrives
    empty = MemoryKeyStore()
    app = create_app(settings, keystore=empty)
    locked = app.state.ctx
    assert locked.locked
    with pytest.raises(PersonLocked):
        locked.person.sign(b"x")
    with TestClient(app) as client:
        client.post("/api/encryption/unlock", json={"recovery_code": code})
        assert locked.person.public_hex == pub
        assert PersonKeys.verify(pub, locked.person.sign(b"x"), b"x")
        assert client.post("/api/encryption/disable").status_code == 200
        assert (data / "person.key").is_file() and not (data / "person.key.enc").exists()
        assert locked.person.public_hex == pub

    app = create_app(settings, keystore=MemoryKeyStore())
    assert app.state.ctx.person.public_hex == pub
