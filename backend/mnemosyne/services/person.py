"""A key per person: the Ed25519 signing key that says "this was me" (docs/plans/2026-10-12-
hashseq-collaboration.md, "A key per person").

One key per library. Without encryption at rest it is `<data_dir>/person.key` (64 hex characters
of the raw private key, mode 0600, like link.key). With it, `<data_dir>/person.key.enc`:
AES-256-GCM under `derive(master, "person")`, a random 12-byte nonce in front, and the AAD
`mnemosyne/person/1`. Never both files at once (a crash between writing the new file and removing
the old one leaves both, with the same key; the next load tidies up).

The private key is never logged and only leaves through `export_private`.
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
import re
import threading
from collections.abc import Callable
from pathlib import Path
from typing import Any

from ..storage.crypto import NONCE, derive

logger = logging.getLogger(__name__)

AAD = b"mnemosyne/person/1"
PLAIN = "person.key"
SEALED = "person.key.enc"
_HEX = re.compile(r"[0-9a-f]+")


class PersonLocked(RuntimeError):
    """Meetings are encrypted and the key is not available, so neither is the person key."""


def _is_hex(text: object, length: int) -> bool:
    return (
        isinstance(text, str) and len(text) == length and _HEX.fullmatch(text.lower()) is not None
    )


class PersonKeys:
    """`master` returns the master key (None when it is not available); `encrypted` says whether
    encryption at rest is on. Both are read when needed, so they follow lock and unlock."""

    def __init__(
        self,
        data_dir: Path,
        master: Callable[[], bytes | None] = lambda: None,
        encrypted: Callable[[], bool] = lambda: False,
    ):
        self.data_dir = Path(data_dir)
        self._master = master
        self._encrypted = encrypted
        self._lock = threading.RLock()
        self._private: Any = None  # the loaded Ed25519PrivateKey

    # ---- files ---------------------------------------------------------------------

    @property
    def plain_path(self) -> Path:
        return self.data_dir / PLAIN

    @property
    def sealed_path(self) -> Path:
        return self.data_dir / SEALED

    @staticmethod
    def _write(path: Path, data: bytes) -> None:
        """Atomically, readable by this user only."""
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".part")
        tmp.unlink(missing_ok=True)
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            with os.fdopen(fd, "wb") as f:
                f.write(data)
                f.flush()
                os.fsync(f.fileno())
            tmp.replace(path)
        except BaseException:
            tmp.unlink(missing_ok=True)
            raise

    @staticmethod
    def _encrypt(master: bytes, raw: bytes) -> bytes:
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM

        nonce = os.urandom(NONCE)
        return nonce + AESGCM(derive(master, "person")).encrypt(nonce, raw, AAD)

    @staticmethod
    def _decrypt(master: bytes, blob: bytes) -> bytes:
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM

        return AESGCM(derive(master, "person")).decrypt(blob[:NONCE], blob[NONCE:], AAD)

    @staticmethod
    def _from_raw(raw: bytes) -> Any:
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

        return Ed25519PrivateKey.from_private_bytes(raw)

    @staticmethod
    def _raw(private: Any) -> bytes:
        from cryptography.hazmat.primitives import serialization

        return private.private_bytes(
            serialization.Encoding.Raw,
            serialization.PrivateFormat.Raw,
            serialization.NoEncryption(),
        )

    def _store(self, private: Any, master: bytes | None) -> None:
        """Write the key where the current mode wants it, then remove the other file."""
        raw = self._raw(private)
        if master is not None:
            self._write(self.sealed_path, self._encrypt(master, raw))
            self.plain_path.unlink(missing_ok=True)
        else:
            self._write(self.plain_path, raw.hex().encode())
            self.sealed_path.unlink(missing_ok=True)

    def _mode_master(self) -> bytes | None:
        """The master key when the key belongs in the sealed file; raises when it should be but
        cannot be read."""
        if not self._encrypted():
            return None
        master = self._master()
        if master is None:
            raise PersonLocked("Meetings are encrypted and the key is not available")
        return master

    def _read_plain(self) -> Any:
        text = self.plain_path.read_text().strip()
        if not _is_hex(text, 64):
            raise ValueError(f"{PLAIN} does not hold a key")
        return self._from_raw(bytes.fromhex(text))

    def _load(self) -> Any:
        """The key from disk, or a new one. Tolerates both files (an interrupted conversion) and
        a file in the wrong mode, and puts it right."""
        master = self._mode_master()
        has_plain, has_sealed = self.plain_path.is_file(), self.sealed_path.is_file()
        if master is not None:
            if has_sealed:
                private = self._from_raw(self._decrypt(master, self.sealed_path.read_bytes()))
                if has_plain:
                    self.plain_path.unlink(missing_ok=True)
                return private
            if has_plain:
                private = self._read_plain()
                self._store(private, master)
                return private
        else:
            if has_plain:
                private = self._read_plain()
                if has_sealed:
                    self.sealed_path.unlink(missing_ok=True)
                return private
            if has_sealed:  # sealed but encryption is off: only the master key can open it
                known = self._master()
                if known is None:
                    raise PersonLocked("The person key is sealed and the key is not available")
                private = self._from_raw(self._decrypt(known, self.sealed_path.read_bytes()))
                self._store(private, None)
                return private
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

        private = Ed25519PrivateKey.generate()
        self._store(private, master)
        logger.info("Made this person's signing key")
        return private

    # ---- the key ---------------------------------------------------------------------

    def ensure(self) -> None:
        """Load the key, making it the first time. Raises PersonLocked while locked."""
        self._get()

    def _get(self) -> Any:
        with self._lock:
            self._mode_master()  # a loaded key still waits for the unlock
            if self._private is None:
                self._private = self._load()
            return self._private

    def _public_bytes(self) -> bytes:
        from cryptography.hazmat.primitives import serialization

        return (
            self._get()
            .public_key()
            .public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
        )

    @property
    def public_hex(self) -> str:
        return self._public_bytes().hex()

    @property
    def pid(self) -> str:
        return "p:" + self.public_hex[:16]

    @property
    def fingerprint(self) -> str:
        """For reading out loud: 8 groups of 4 characters of the public key's SHA-256."""
        digest = hashlib.sha256(self._public_bytes()).digest()
        text = base64.b32encode(digest).decode().rstrip("=")[:32]
        return " ".join(text[i : i + 4] for i in range(0, 32, 4))

    def sign(self, data: bytes) -> str:
        """Hex signature of `data`. Raises PersonLocked while the app is locked."""
        return self._get().sign(data).hex()

    @staticmethod
    def verify(public_hex: str, sig_hex: str, data: bytes) -> bool:
        """Whether `sig_hex` is `public_hex`'s signature of `data`. False for anything malformed."""
        try:
            if not (_is_hex(public_hex, 64) and _is_hex(sig_hex, 128)):
                return False
            from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

            Ed25519PublicKey.from_public_bytes(bytes.fromhex(public_hex)).verify(
                bytes.fromhex(sig_hex), data
            )
            return True
        except Exception:
            return False

    @staticmethod
    def public_of(private_hex: str) -> str:
        """The public key (hex) of a private key given as hex. ValueError when it is not one."""
        from cryptography.hazmat.primitives import serialization

        text = private_hex.strip().lower() if isinstance(private_hex, str) else ""
        if not _is_hex(text, 64):
            raise ValueError("That is not a person key (64 hex characters)")
        public = PersonKeys._from_raw(bytes.fromhex(text)).public_key()
        return public.public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw).hex()

    @staticmethod
    def canonical(obj: Any) -> bytes:
        """The bytes that get signed for a JSON value: sorted keys, no spaces, UTF-8."""
        return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()

    # ---- changing it -------------------------------------------------------------------

    def rotate(self) -> None:
        """A new key; the old one is gone."""
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

        with self._lock:
            master = self._mode_master()
            private = Ed25519PrivateKey.generate()
            self._store(private, master)
            self._private = private

    def export_private(self) -> str:
        """The private key as hex. Only for library sync, which hands it to another computer
        of the same person over a paired, end-to-end encrypted tunnel (a later phase's route)."""
        return self._raw(self._get()).hex()

    def replace(self, private_hex: str) -> None:
        """Adopt another computer's key (the one `export_private` gave). ValueError when it is
        not a key."""
        text = private_hex.strip().lower() if isinstance(private_hex, str) else ""
        if not _is_hex(text, 64):
            raise ValueError("That is not a person key (64 hex characters)")
        private = self._from_raw(bytes.fromhex(text))
        with self._lock:
            master = self._mode_master()
            self._store(private, master)
            self._private = private

    # ---- turning encryption at rest on and off -----------------------------------------

    def seal(self, master: bytes) -> None:
        """Encryption just turned on: person.key becomes person.key.enc (the new file is written
        before the old one is removed). Nothing to do when no key was made yet."""
        with self._lock:
            if not self.plain_path.is_file():
                return
            private = self._read_plain()
            self._write(self.sealed_path, self._encrypt(master, self._raw(private)))
            self.plain_path.unlink(missing_ok=True)
            self._private = private

    def unseal(self) -> None:
        """Encryption is turning off: person.key.enc becomes person.key. Needs the master key
        (still held by the caller)."""
        with self._lock:
            if not self.sealed_path.is_file():
                return
            master = self._master()
            if master is None:
                raise PersonLocked("Meetings are encrypted and the key is not available")
            private = self._from_raw(self._decrypt(master, self.sealed_path.read_bytes()))
            self._write(self.plain_path, self._raw(private).hex().encode())
            self.sealed_path.unlink(missing_ok=True)
            self._private = private
