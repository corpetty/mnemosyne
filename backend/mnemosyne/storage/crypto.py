"""Encryption at rest: the key, the encrypted audio file format, and plaintext views of it.

One random 256-bit master key per data directory, kept in the system keyring (Secret Service,
unlocked with the login session; on a Mac started by the app, the app's keychain item:
ShellKeyStore). The database key (SQLCipher) and the file key (AES-256-GCM)
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
import logging
import os
import secrets
import struct
import subprocess
import sys
import tempfile
from collections.abc import AsyncIterator, Iterator
from pathlib import Path
from typing import Protocol

import psutil

MAGIC = b"MNEMENC1"
CHUNK = 64 * 1024
NONCE = 12
TAG = 16
HEADER = struct.Struct("<8sIQ")  # magic, chunk size, plaintext size
SUFFIX = ".enc"
KEYRING_SERVICE = "Mnemosyne"  # also the app's keychain service (src-tauri/src/keychain.rs)

logger = logging.getLogger(__name__)


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


class ShellUnavailable(RuntimeError):
    """The app binary cannot keep the key: not there, timed out, denied or failed."""


class ShellKeyStore:
    """macOS: the master key in the keychain, kept by the signed app (`mnemosyne --keychain`,
    src-tauri/src/keychain.rs; the app passes its path as MNEMOSYNE_SHELL_BIN). An item the
    venv's Python creates trusts that Python binary, so a Python upgrade made macOS ask
    "python3.13 wants to use your confidential information"; one the app creates trusts the
    app's signature, which stays the same across updates.

    The app's item is a separate account (" (app)" appended) under the same service: keyring's
    macOS backend uses the same SecItem namespace, so the same account would be the very item
    Python created. The secret goes to the binary on stdin and comes back on stdout, never argv.

    `legacy` is where the key was before (SystemKeyStore). When the app has no key but the old
    place has one, it is copied over and the old item deleted once it reads back the same
    (_migrate). Whenever the app cannot be used (binary gone, timeout, the keychain prompt
    denied) this run falls back to the old place, so the backend still starts.

    A backend started without the app on a Mac (`mnemosyne-backend` in a terminal, no
    MNEMOSYNE_SHELL_BIN) uses SystemKeyStore and, after a migration, finds no key: it opens
    locked and asks for the recovery code (nothing is lost; unlocking writes the key back to
    the old place, which the app's backend then ignores since its own item exists). Leaving
    the old item in place would spare that, but would keep a second copy that any process
    running that Python reads without asking, and the item that prompts after each upgrade.

    Later uses: unlock (set), turning encryption on (set) and off (delete) run the binary again;
    MNEMOSYNE_SHELL_BIN is read on each call. A backend that outlives the app (a recording after
    a crash, keep_sharing_after_quit) still reaches it: the CLI mode needs no running app, and
    an update replaces the binary at the same path. When it is gone, set falls back to the old
    place (the next start with the app migrates it) and the error says why."""

    TIMEOUT = 60.0  # seconds; a keychain prompt waits for the user
    NOT_FOUND = 3
    DENIED = 4

    def __init__(self, data_dir: Path, legacy: KeyStore | None = None):
        self.legacy = legacy if legacy is not None else SystemKeyStore(data_dir)
        self.account = f"data key for {Path(data_dir).resolve()} (app)"

    @staticmethod
    def binary() -> str | None:
        """The app binary, when it is there to run."""
        path = os.environ.get("MNEMOSYNE_SHELL_BIN")
        return path if path and os.path.isfile(path) and os.access(path, os.X_OK) else None

    def _run(self, op: str, secret: str | None = None) -> subprocess.CompletedProcess:
        binary = self.binary()
        if binary is None:
            raise ShellUnavailable(
                "The Mnemosyne app is not there to reach the keychain (MNEMOSYNE_SHELL_BIN: "
                f"{os.environ.get('MNEMOSYNE_SHELL_BIN') or 'not set'})"
            )
        try:
            return subprocess.run(
                [binary, "--keychain", op, self.account],
                input=(secret + "\n").encode() if secret is not None else b"",
                capture_output=True,
                timeout=self.TIMEOUT,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as e:
            raise ShellUnavailable(f"The keychain did not answer ({op}): {e}") from e

    def _fail(self, op: str, done: subprocess.CompletedProcess) -> ShellUnavailable:
        if done.returncode == self.DENIED:
            return ShellUnavailable(f"Access to the keychain was denied ({op})")
        # stderr carries Apple's message for the status, never the secret
        why = done.stderr.decode(errors="replace").strip() or f"exit {done.returncode}"
        return ShellUnavailable(f"The keychain refused ({op}): {why}")

    def _get(self) -> bytes | None:
        done = self._run("get")
        if done.returncode == self.NOT_FOUND:
            return None
        if done.returncode != 0:
            raise self._fail("get", done)
        try:
            return base64.b64decode(done.stdout.strip(), validate=True) or None
        except ValueError as e:
            raise ShellUnavailable("The keychain item does not hold a key") from e

    def _set(self, key: bytes) -> None:
        done = self._run("set", base64.b64encode(key).decode())
        if done.returncode != 0:
            raise self._fail("set", done)

    def get(self) -> bytes | None:
        try:
            key = self._get()
        except ShellUnavailable as e:
            logger.warning("%s; using the old keyring item for this run", e)
            return self.legacy.get()
        if key is not None:
            return key
        old = self.legacy.get()  # may ask "python3.13 wants to use…" one last time
        if old is not None:
            self._migrate(old)
        return old

    def _migrate(self, key: bytes) -> None:
        """Copy the key from the old place into the app's item; delete the old item only once
        the app reads the same key back. Any failure (including the user denying a keychain
        prompt) leaves the old item alone: this run uses the key it read from there, and the
        next start tries again."""
        try:
            self._set(key)
            if self._get() != key:
                raise ShellUnavailable("the key read back differs")
        except ShellUnavailable as e:
            logger.warning("Could not move the key into the app's keychain item: %s", e)
            return
        self.legacy.delete()
        logger.info("The encryption key is now kept by the app's keychain item")

    def set(self, key: bytes) -> None:
        try:
            self._set(key)
        except ShellUnavailable as e:
            # The old place keeps it (raising if there is none); a start with the app moves it.
            logger.warning("%s; keeping the key in the old keyring item", e)
            self.legacy.set(key)

    def delete(self) -> None:
        try:
            done = self._run("delete")
            if done.returncode != 0:
                raise self._fail("delete", done)
        except ShellUnavailable as e:
            logger.warning("Could not delete the app's keychain item: %s", e)
        self.legacy.delete()  # a copy left there by a fallback or an older version


def system_keystore(data_dir: Path) -> KeyStore:
    """The desktop's key store: the app's keychain item on a Mac started by the app, else the
    system keyring."""
    legacy = SystemKeyStore(data_dir)
    if sys.platform == "darwin" and ShellKeyStore.binary() is not None:
        return ShellKeyStore(data_dir, legacy)
    return legacy


class CredentialKeyStore:
    """The key provided by the system, for a server with no desktop keyring: a file named by
    MNEMOSYNE_KEY_FILE holding 32 random bytes in base64, typically a systemd credential
    (`LoadCredentialEncrypted=`, sealed to the machine's TPM; docs/team-server.md). It is
    `fixed`: turning encryption on adopts it instead of making a new key."""

    fixed = True

    def __init__(self, path: Path):
        self.path = Path(path)

    def get(self) -> bytes | None:
        try:
            key = base64.b64decode(self.path.read_text().strip(), validate=True)
        except (OSError, ValueError):
            return None
        return key if len(key) == 32 else None

    def set(self, key: bytes) -> None:
        if key != self.get():
            raise RuntimeError(
                f"The key comes from {self.path} (MNEMOSYNE_KEY_FILE) and cannot be replaced here"
            )

    def delete(self) -> None:
        pass  # the system owns it


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


def _pid_alive(pid: int) -> bool:
    try:
        return psutil.pid_exists(pid)
    except (OverflowError, ValueError, OSError):
        return False  # not a pid at all


def scratch_dir() -> Path:
    """A private folder for plaintext audio that must not outlive this backend (removed at the
    next start when it crashes): audio another computer sent to be transcribed here, too."""
    return _scratch_dir()


def clean_scratch() -> None:
    """Remove plaintext copies left behind by backends that crashed (called at startup)."""
    import shutil

    root = _scratch_root()
    if not root.is_dir():
        return
    for folder in root.iterdir():
        if folder.name.isdigit() and not _pid_alive(int(folder.name)):
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
