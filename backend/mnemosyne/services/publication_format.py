"""The publication bundle format (phase 7): a padded JSON manifest and clip blobs, each sealed
with AES-256-GCM under one random key that lives only in the link fragment.

The viewer (viewer/src/format.ts) implements the reading half of this file; the two are tied
together by backend/tests/vectors/publication_v1.json. Change one, change the other and the
vectors, and bump `FORMAT` if old links would no longer open.

Rules, exactly as implemented:

- ids are 16 lowercase hex characters (8 random bytes), never derived from a session id;
  keys are 32 random bytes written as unpadded base64url (43 characters); the publisher token
  a host uses to authorize updates is 32 random bytes in the same encoding.
- plaintext = canonical JSON (sorted keys, no whitespace, UTF-8, non-ASCII kept as is) for the
  manifest, raw Ogg Opus bytes for a clip.
- padded = 4-byte big-endian length of the plaintext, the plaintext, zero bytes up to a multiple
  of 4096 (manifest) or 65536 (clip). Reading checks the length and that the padding is zeros.
- blob = nonce(12, random) || AES-256-GCM ciphertext || tag(16).
- AAD = UTF-8 of "mnemosyne/pub/1|<id>|<kind>|<version>", kind `manifest` or `clip`. A clip's
  version is the one that FIRST uploaded it (`ClipRef.v`), so it can be reused by later versions.
- blob address = lowercase hex SHA-256 of the whole blob.
- caps are on the padded size: manifest 1 MiB, one clip 20 MiB, all clips of a publication 20 MiB.
- a key from a password (single-file export): PBKDF2-HMAC-SHA256, 16-byte salt, 32-byte key;
  the file carries salt and iteration count.
"""

from __future__ import annotations

import base64
import hashlib
import json
import re
import secrets
import struct
from collections.abc import Iterable
from typing import Literal

from pydantic import BaseModel, ConfigDict

FORMAT = 1
AAD_PREFIX = "mnemosyne/pub/1"
NONCE_SIZE = 12
TAG_SIZE = 16
KEY_SIZE = 32
ID_HEX_LEN = 16
KEY_B64_LEN = 43

MANIFEST_BLOCK = 4096
CLIP_BLOCK = 65536
MAX_MANIFEST = 1024 * 1024
MAX_CLIP = 20 * 1024 * 1024
MAX_CLIPS_TOTAL = 20 * 1024 * 1024

PBKDF2_ITERATIONS = 600_000
PBKDF2_MIN_ITERATIONS = 100_000
PBKDF2_MAX_ITERATIONS = 10_000_000
SALT_SIZE = 16

Kind = Literal["manifest", "clip"]
_BLOCK = {"manifest": MANIFEST_BLOCK, "clip": CLIP_BLOCK}
_CAP = {"manifest": MAX_MANIFEST, "clip": MAX_CLIP}
_ID_RE = re.compile(r"^[0-9a-f]{16}$")
_B64URL_RE = re.compile(r"^[A-Za-z0-9_-]+$")


class PublicationFormatError(ValueError):
    """The data cannot be sealed or opened; the message says why, for the person."""


# ---- ids and keys --------------------------------------------------------------------


def new_id() -> str:
    return secrets.token_bytes(8).hex()


def new_key() -> bytes:
    return secrets.token_bytes(KEY_SIZE)


def new_token() -> str:
    """A publisher token (what a host checks before it accepts an update or a delete)."""
    return b64url_encode(secrets.token_bytes(32))


def new_salt() -> bytes:
    return secrets.token_bytes(SALT_SIZE)


def b64url_encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode().rstrip("=")


def b64url_decode(text: str) -> bytes:
    if not _B64URL_RE.match(text) or len(text) % 4 == 1:
        raise PublicationFormatError("Not valid base64url")
    data = base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))
    if b64url_encode(data) != text:  # trailing bits must be zero: one spelling per value
        raise PublicationFormatError("Not valid base64url")
    return data


def encode_key(key: bytes) -> str:
    if len(key) != KEY_SIZE:
        raise PublicationFormatError("A publication key is 32 bytes")
    return b64url_encode(key)


def decode_key(text: str) -> bytes:
    if len(text) != KEY_B64_LEN:
        raise PublicationFormatError("That link's key is not the right length")
    key = b64url_decode(text)
    if len(key) != KEY_SIZE:
        raise PublicationFormatError("That link's key is not the right length")
    return key


def check_id(pub_id: str) -> str:
    if not _ID_RE.match(pub_id):
        raise PublicationFormatError("A publication id is 16 lowercase hex characters")
    return pub_id


# ---- the manifest --------------------------------------------------------------------


class _Strict(BaseModel):
    # Unknown fields are an error: nothing (a transcript, say) can ride along unnoticed.
    model_config = ConfigDict(extra="forbid")


class ClipRef(_Strict):
    sha256: str  # address of the sealed clip blob
    size: int  # bytes of the (unsealed) Ogg Opus clip
    seconds: float
    mime: str
    v: int  # the version that first uploaded this clip: part of its AAD


class Decision(_Strict):
    id: str
    text: str
    at: float | None = None


class Question(_Strict):
    id: str
    text: str
    at: float | None = None


class ActionItem(_Strict):
    id: str
    text: str
    owner: str | None = None
    done: bool = False
    due: str | None = None


class Chapter(_Strict):
    title: str
    start: float


class Quote(_Strict):
    id: str
    text: str
    speaker: str | None = None
    start: float
    end: float
    clip: ClipRef | None = None


class Manifest(_Strict):
    """What a reader sees. There is deliberately no field for transcript segments."""

    format: Literal[1] = FORMAT
    id: str
    version: int
    published_at: str
    expires_at: str | None = None
    title: str
    date: str
    duration_seconds: float | None = None
    summary: str = ""
    decisions: list[Decision] = []
    questions: list[Question] = []
    action_items: list[ActionItem] = []
    chapters: list[Chapter] = []
    attendees: list[str] = []
    topics: list[str] = []
    notes: str | None = None
    quotes: list[Quote] = []


def canonical_json(value: object) -> bytes:
    """Sorted keys, no whitespace, UTF-8: the same value always gives the same bytes."""
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    ).encode("utf-8")


def manifest_bytes(manifest: Manifest) -> bytes:
    return canonical_json(manifest.model_dump(mode="json"))


def parse_manifest(data: bytes) -> Manifest:
    try:
        raw = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as e:
        raise PublicationFormatError("The manifest is not valid JSON") from e
    if isinstance(raw, dict) and raw.get("format") != FORMAT:
        raise PublicationFormatError(
            f"This publication uses format {raw.get('format')!r}; this app reads format {FORMAT}"
        )
    try:
        return Manifest.model_validate(raw)
    except ValueError as e:
        raise PublicationFormatError(f"The manifest is malformed: {e}") from e


# ---- padding -------------------------------------------------------------------------


def padded_size(length: int, block: int) -> int:
    """Bytes after the 4-byte length prefix and zero padding up to a multiple of `block`."""
    total = 4 + length
    return total + (-total % block)


def pad(plaintext: bytes, block: int) -> bytes:
    if len(plaintext) > 0xFFFFFFFF:
        raise PublicationFormatError("Too large")
    body = struct.pack(">I", len(plaintext)) + plaintext
    return body + b"\0" * (-len(body) % block)


def unpad(padded: bytes, block: int) -> bytes:
    if len(padded) < 4 or len(padded) % block:
        raise PublicationFormatError("The padded data has the wrong size")
    (length,) = struct.unpack(">I", padded[:4])
    if 4 + length > len(padded) or len(padded) - (4 + length) >= block:
        raise PublicationFormatError("The padded data has a bad length")
    if any(padded[4 + length :]):
        raise PublicationFormatError("The padding is not zeros")
    return padded[4 : 4 + length]


# ---- sealing -------------------------------------------------------------------------


def aad(pub_id: str, kind: Kind, version: int) -> bytes:
    if kind not in _BLOCK:
        raise PublicationFormatError(f"Unknown blob kind {kind!r}")
    return f"{AAD_PREFIX}|{pub_id}|{kind}|{version}".encode()


def blob_hash(blob: bytes) -> str:
    return hashlib.sha256(blob).hexdigest()


def _aesgcm(key: bytes):
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    if len(key) != KEY_SIZE:
        raise PublicationFormatError("A publication key is 32 bytes")
    return AESGCM(key)


def seal(
    kind: Kind,
    pub_id: str,
    version: int,
    plaintext: bytes,
    key: bytes,
    *,
    nonce: bytes | None = None,
) -> tuple[bytes, str]:
    """Pad and encrypt; returns (blob, its address). `nonce` is for the test vectors only."""
    check_id(pub_id)
    additional = aad(pub_id, kind, version)
    padded = pad(plaintext, _BLOCK[kind])
    if len(padded) > _CAP[kind]:
        limit = _CAP[kind] // (1024 * 1024)
        raise PublicationFormatError(f"A {kind} may be at most {limit} MiB")
    if nonce is None:
        nonce = secrets.token_bytes(NONCE_SIZE)
    elif len(nonce) != NONCE_SIZE:
        raise PublicationFormatError("A nonce is 12 bytes")
    blob = nonce + _aesgcm(key).encrypt(nonce, padded, additional)
    return blob, blob_hash(blob)


def open_blob(kind: Kind, pub_id: str, version: int, blob: bytes, key: bytes) -> bytes:
    """Decrypt and unpad. Raises PublicationFormatError when the key, id, kind or version
    do not match what the blob was sealed with, or the blob was altered."""
    from cryptography.exceptions import InvalidTag

    additional = aad(pub_id, kind, version)
    if len(blob) < NONCE_SIZE + TAG_SIZE:
        raise PublicationFormatError("The data is too short to be a publication blob")
    try:
        padded = _aesgcm(key).decrypt(blob[:NONCE_SIZE], blob[NONCE_SIZE:], additional)
    except InvalidTag as e:
        raise PublicationFormatError(
            f"Could not decrypt the {kind}: wrong key, or it was altered, "
            "or it belongs to another publication or version"
        ) from e
    return unpad(padded, _BLOCK[kind])


def seal_manifest(
    manifest: Manifest, key: bytes, *, nonce: bytes | None = None
) -> tuple[bytes, str]:
    return seal(
        "manifest", manifest.id, manifest.version, manifest_bytes(manifest), key, nonce=nonce
    )


def open_manifest(pub_id: str, version: int, blob: bytes, key: bytes) -> Manifest:
    manifest = parse_manifest(open_blob("manifest", pub_id, version, blob, key))
    if manifest.id != pub_id or manifest.version != version:
        raise PublicationFormatError("The manifest is for another publication or version")
    return manifest


def seal_clip(
    pub_id: str, first_version: int, ogg: bytes, key: bytes, *, nonce: bytes | None = None
) -> tuple[bytes, str]:
    return seal("clip", pub_id, first_version, ogg, key, nonce=nonce)


def open_clip(pub_id: str, clip: ClipRef, blob: bytes, key: bytes) -> bytes:
    if blob_hash(blob) != clip.sha256:
        raise PublicationFormatError("The clip does not match its address")
    return open_blob("clip", pub_id, clip.v, blob, key)


def check_clips_total(padded_sizes: Iterable[int]) -> None:
    """Raise when the clips of one publication (padded sizes) exceed MAX_CLIPS_TOTAL."""
    if sum(padded_sizes) > MAX_CLIPS_TOTAL:
        raise PublicationFormatError(
            f"A publication's clips may total at most {MAX_CLIPS_TOTAL // (1024 * 1024)} MiB"
        )


# ---- a key from a password -----------------------------------------------------------


def derive_key(password: str, salt: bytes, iterations: int = PBKDF2_ITERATIONS) -> bytes:
    if not PBKDF2_MIN_ITERATIONS <= iterations <= PBKDF2_MAX_ITERATIONS:
        raise PublicationFormatError("The password iteration count is out of range")
    if len(salt) != SALT_SIZE:
        raise PublicationFormatError("The password salt is 16 bytes")
    return hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations, KEY_SIZE)
