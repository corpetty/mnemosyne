"""Encryption at rest: the key, the encrypted audio file format, and plaintext views of it.

One random 256-bit master key per data directory, kept in the system keyring (Secret Service,
unlocked with the login session). The database key (SQLCipher) and the file key (AES-256-GCM)
are derived from it with HKDF, so neither is used for two purposes. The recovery code shown
when encryption is turned on is the master key itself, in base32.

Encrypted audio (`<name>.enc`) is a header and AES-GCM chunks of 64 KiB, each with its own
random nonce and its index in the associated data, so a range can be decrypted without reading
the whole file (playback seeks) and chunks cannot be reordered or truncated unnoticed.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import hashlib
import os
import secrets
import struct
import tempfile
from collections.abc import AsyncIterator, Iterator
from pathlib import Path
from typing import Protocol

MAGIC = b"MNEMENC1"
CHUNK = 64 * 1024
NONCE = 12
TAG = 16
HEADER = struct.Struct("<8sIQ")  # magic, chunk size, plaintext size
SUFFIX = ".enc"
KEYRING_SERVICE = "Mnemosyne"


# ---- keys ----------------------------------------------------------------------------


def new_key() -> bytes:
    return secrets.token_bytes(32)


def derive(master: bytes, purpose: str) -> bytes:
    """A 32-byte key for one purpose ("db", "files") from the master key (HKDF-SHA256)."""
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.kdf.hkdf import HKDF

    return HKDF(
        algorithm=hashes.SHA256(), length=32, salt=None, info=f"mnemosyne/{purpose}".encode()
    ).derive(master)


def recovery_code(master: bytes) -> str:
    """The master key as groups of base32, for writing down."""
    text = base64.b32encode(master).decode().rstrip("=")
    return "-".join(text[i : i + 4] for i in range(0, len(text), 4))


def parse_recovery_code(code: str) -> bytes:
    text = "".join(ch for ch in code.upper() if ch.isalnum())
    try:
        key = base64.b32decode(text + "=" * (-len(text) % 8))
    except ValueError as e:
        raise ValueError("That is not a Mnemosyne recovery code") from e
    if len(key) != 32:
        raise ValueError("That is not a Mnemosyne recovery code")
    return key


def key_check(master: bytes) -> str:
    """Short fingerprint stored with the settings, to tell a wrong key from a right one."""
    return hashlib.sha256(derive(master, "check")).hexdigest()[:16]


class KeyStore(Protocol):
    def get(self) -> bytes | None: ...

    def set(self, key: bytes) -> None: ...

    def delete(self) -> None: ...


class SystemKeyStore:
    """The master key in the desktop keyring, one entry per data directory."""

    def __init__(self, data_dir: Path):
        self.account = f"data key for {Path(data_dir).resolve()}"

    def get(self) -> bytes | None:
        import keyring

        try:
            value = keyring.get_password(KEYRING_SERVICE, self.account)
        except Exception:
            return None
        return base64.b64decode(value) if value else None

    def set(self, key: bytes) -> None:
        import keyring

        try:
            keyring.set_password(KEYRING_SERVICE, self.account, base64.b64encode(key).decode())
        except Exception as e:
            raise RuntimeError(
                "No system keyring to keep the key in (is GNOME Keyring or KWallet running?)"
            ) from e

    def delete(self) -> None:
        import keyring

        with contextlib.suppress(Exception):
            keyring.delete_password(KEYRING_SERVICE, self.account)


class FileKeyStore:
    """The key in a file next to the data. Demo mode only (the browser tests): it protects
    nothing, but keeps test runs out of the user's real keyring."""

    def __init__(self, path: Path):
        self.path = Path(path)

    def get(self) -> bytes | None:
        return self.path.read_bytes() if self.path.is_file() else None

    def set(self, key: bytes) -> None:
        self.path.write_bytes(key)

    def delete(self) -> None:
        self.path.unlink(missing_ok=True)


class MemoryKeyStore:
    """For tests."""

    def __init__(self, key: bytes | None = None):
        self.key = key

    def get(self) -> bytes | None:
        return self.key

    def set(self, key: bytes) -> None:
        self.key = key

    def delete(self) -> None:
        self.key = None


# ---- files ---------------------------------------------------------------------------


def is_encrypted(path: str | Path) -> bool:
    return str(path).endswith(SUFFIX)


def _aad(header: bytes, index: int, last: bool) -> bytes:
    return header + struct.pack("<Q?", index, last)


def encrypt_file(src: Path, file_key: bytes, remove: bool = True) -> Path:
    """Write `src` + ".enc" and (by default) delete `src`. Returns the new path."""
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    aes = AESGCM(file_key)
    size = src.stat().st_size
    header = HEADER.pack(MAGIC, CHUNK, size)
    count = max(1, -(-size // CHUNK))
    dst = src.with_name(src.name + SUFFIX)
    tmp = dst.with_name(dst.name + ".part")
    with src.open("rb") as fin, tmp.open("wb") as fout:
        fout.write(header)
        for i in range(count):
            nonce = os.urandom(NONCE)
            fout.write(nonce + aes.encrypt(nonce, fin.read(CHUNK), _aad(header, i, i == count - 1)))
        fout.flush()
        os.fsync(fout.fileno())
    tmp.replace(dst)
    if remove:
        src.unlink()
    return dst


class EncryptedFile:
    """Random access to the plaintext of an encrypted file."""

    def __init__(self, path: Path, file_key: bytes):
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM

        self.path = Path(path)
        self._aes = AESGCM(file_key)
        with self.path.open("rb") as f:
            self._header = f.read(HEADER.size)
        magic, self.chunk, self.size = HEADER.unpack(self._header)
        if magic != MAGIC:
            raise ValueError(f"{path} is not a Mnemosyne encrypted file")
        self.count = max(1, -(-self.size // self.chunk))

    def _chunk(self, f, index: int) -> bytes:
        stored = NONCE + self.chunk + TAG
        f.seek(HEADER.size + index * stored)
        blob = f.read(stored)
        last = index == self.count - 1
        return self._aes.decrypt(blob[:NONCE], blob[NONCE:], _aad(self._header, index, last))

    def read(self, start: int = 0, end: int | None = None) -> bytes:
        """Plaintext bytes [start, end) (end exclusive; None = to the end)."""
        end = self.size if end is None else min(end, self.size)
        if start >= end:
            return b""
        first, last = start // self.chunk, (end - 1) // self.chunk
        with self.path.open("rb") as f:
            data = b"".join(self._chunk(f, i) for i in range(first, last + 1))
        offset = first * self.chunk
        return data[start - offset : end - offset]

    def write_plain(self, dst: Path) -> None:
        with self.path.open("rb") as f, dst.open("wb") as out:
            for i in range(self.count):
                out.write(self._chunk(f, i))


def decrypt_file(src: Path, file_key: bytes, remove: bool = True) -> Path:
    """Write the plaintext next to `src` (without ".enc") and (by default) delete `src`."""
    dst = src.with_name(src.name[: -len(SUFFIX)])
    tmp = dst.with_name(dst.name + ".part")
    EncryptedFile(src, file_key).write_plain(tmp)
    tmp.replace(dst)
    if remove:
        src.unlink()
    return dst


def _scratch_root() -> Path:
    """Where plaintext copies live briefly: the per-user runtime dir (RAM, private)."""
    base = os.environ.get("XDG_RUNTIME_DIR")
    if base and Path(base).is_dir():
        return Path(base) / "mnemosyne"
    return Path(tempfile.gettempdir()) / f"mnemosyne-{os.getuid()}"


def _scratch_dir() -> Path:
    """This process's own folder, so a backend never touches another one's copies."""
    root = _scratch_root() / str(os.getpid())
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    return root


def clean_scratch() -> None:
    """Remove plaintext copies left behind by backends that crashed (called at startup)."""
    import shutil

    root = _scratch_root()
    if not root.is_dir():
        return
    for folder in root.iterdir():
        if folder.name.isdigit() and not Path(f"/proc/{folder.name}").exists():
            shutil.rmtree(folder, ignore_errors=True)


@contextlib.contextmanager
def plaintext(path: str | Path, file_key: bytes | None) -> Iterator[Path]:
    """A path to read the file's plaintext from: the file itself when it is not encrypted, else a
    private temporary copy that is removed afterwards."""
    path = Path(path)
    if not is_encrypted(path):
        yield path
        return
    if file_key is None:
        raise PermissionError("Meetings are encrypted and the key is not available")
    fd, name = tempfile.mkstemp(dir=_scratch_dir(), suffix=Path(path.name[: -len(SUFFIX)]).suffix)
    os.close(fd)
    tmp = Path(name)
    try:
        EncryptedFile(path, file_key).write_plain(tmp)
        yield tmp
    finally:
        tmp.unlink(missing_ok=True)


@contextlib.asynccontextmanager
async def plaintext_async(path: str | Path, file_key: bytes | None) -> AsyncIterator[Path]:
    """plaintext() for async code: the copy (seconds, for hours of audio) is made in a thread,
    not on the event loop, and removed even when the caller is cancelled while it is made."""
    cm = plaintext(path, file_key)
    made = asyncio.ensure_future(asyncio.to_thread(cm.__enter__))
    try:
        source = await asyncio.shield(made)
    except asyncio.CancelledError:
        made.add_done_callback(
            lambda t: t.cancelled() or t.exception() or cm.__exit__(None, None, None)
        )
        raise
    try:
        yield source
    finally:
        cm.__exit__(None, None, None)
