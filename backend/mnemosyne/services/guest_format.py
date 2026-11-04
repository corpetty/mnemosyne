"""The guest-notes wire format (phase 9): sealed frames that guests (a browser page) and the
owner's backend (the bridge) exchange through a relay that cannot read them.

The guest page (guest/src/format.ts) and the relay (publish-host, room token only) implement the
same rules; backend/tests/vectors/guest_v1.json ties them together. Change one, change the others
and the vectors, and bump `FORMAT` if old links would no longer open.

Rules, exactly as implemented:

- a room id is 16 lowercase hex characters (8 random bytes); the key is 32 random bytes written
  as unpadded base64url (43 characters) and travels only in the link fragment.
- `frames_key = HKDF-SHA256(key, salt=b"", info=b"mnemosyne/guest/frames", length=32)`;
  `room_token = b64url(HKDF-SHA256(key, salt=b"", info=b"mnemosyne/guest/room", length=32))`.
  The host sees and compares the room token only; it never gets the key or the frames key.
- kinds (one byte): NOTES=1, SNAPSHOT=2, COMMENT=3, META=4, PRESENCE=5, MEDIA=6.
- envelope = kind(1) || nonce(12, random) || AES-256-GCM(frames_key, nonce, padded) where
  padded = 4-byte big-endian length, the plaintext, zeros up to a multiple of 256 bytes
  (4096 for SNAPSHOT, 65536 for MEDIA), i.e. publication_format.pad/unpad. The ciphertext carries
  a 16-byte tag.
- AAD = UTF-8 of "mnemosyne/guest/1|<room>|<kind as decimal>". The kind byte is also in the
  AAD, so changing it, or moving an envelope to another room, fails to open. MEDIA's AAD has one
  more field, "mnemosyne/guest/1|<room>|6|<sha>", where sha is the plaintext's SHA-256 as 64
  lowercase hex characters; it is required for MEDIA and refused for every other kind.
- NOTES plaintext = u8 len(gid) || gid || u8 len(name) || name || frame (gid and name UTF-8).
  `gid` is the guest page's random id ("" for the owner's side); `name` is what the person typed
  (truncated at a character boundary to 255 bytes); `frame` is an opaque hashseq frame that must
  start with 0xDE (delta) or 0xAF (artifact).
- the other kinds carry canonical JSON (sorted keys, no whitespace, UTF-8, non-ASCII as is):
  snapshot  {format, notes: b64url hashseq snapshot or "", origin, frames: [b64url...],
             threads: [...], meta: {...}}
  comment   {op: "create"|"reply"|"edit"|"delete", id, parent_id, text, quote, gid, name,
             notes_ids: [first, last]}
  meta      {title, date, owner, agenda: [{text, covered}], origin, ended, ended_at,
             clock?: {part, offset, started_at}}
            (`origin`: the notes document's hashseq origin, which a guest needs to open it)
  presence  {gid, name, typing: bool, sel?: {a, f} | null, gone?: true}
            (`sel`: the selection's anchor and focus, each the element id (64 hex) of the
            character before it or null; `gone`: the page is closing. gid "" is the owner's
            heartbeat, "o:<member key>" one of the owner-side editors; older pages ignore the
            optional fields)
  (this module checks that the JSON parses; the shapes belong to the bridge and the page.)
- MEDIA (an image of the notes) plaintext = the raw bytes, at most MEDIA_MAX (2,000,000); not JSON,
  and `unseal_media` checks that sha256(bytes) == sha. A reader asks for a blob by its sha, which
  the notes carry, but the relay stores and serves it under `media_name(key, sha)` =
  hex(HMAC-SHA256(media_name_key, sha as the 64 ASCII hex characters)) with
  `media_name_key = HKDF-SHA256(key, salt=b"", info=b"mnemosyne/guest/media-name", length=32)`:
  every holder of the key can compute it, the host sees an opaque name (a sealed blob's own hash
  cannot serve, AES-GCM nonces are random). The plain `seal`/`unseal` take the sha as a keyword
  (`unseal` refuses a MEDIA envelope without it); the MEDIA kind is additive, FORMAT stays 1.
"""

from __future__ import annotations

import json
import re
import secrets
from enum import IntEnum

from . import publication_format as pf

FORMAT = 1
AAD_PREFIX = f"mnemosyne/guest/{FORMAT}"
INFO_FRAMES = b"mnemosyne/guest/frames"
INFO_ROOM = b"mnemosyne/guest/room"
INFO_MEDIA_NAME = b"mnemosyne/guest/media-name"
KEY_SIZE = pf.KEY_SIZE
NONCE_SIZE = pf.NONCE_SIZE
TAG_SIZE = pf.TAG_SIZE
ROOM_HEX_LEN = 16
BLOCK = 256
SNAPSHOT_BLOCK = 4096
MEDIA_BLOCK = 65536
MEDIA_MAX = 2_000_000
MAX_FIELD = 255
FRAME_START = (0xDE, 0xAF)

_ROOM_RE = re.compile(r"^[0-9a-f]{16}$")
_SHA_RE = re.compile(r"^[0-9a-f]{64}$")


class Kind(IntEnum):
    NOTES = 1
    SNAPSHOT = 2
    COMMENT = 3
    META = 4
    PRESENCE = 5
    MEDIA = 6


JSON_KINDS = (Kind.SNAPSHOT, Kind.COMMENT, Kind.META, Kind.PRESENCE)


class GuestFormatError(ValueError):
    """The data cannot be sealed or opened; the message says why."""


# ---- ids and keys --------------------------------------------------------------------


def new_room() -> str:
    return secrets.token_bytes(ROOM_HEX_LEN // 2).hex()


def new_key() -> bytes:
    return pf.new_key()


def check_room(room: str) -> str:
    if not isinstance(room, str) or not _ROOM_RE.match(room):
        raise GuestFormatError("A room id is 16 lowercase hex characters")
    return room


def key_to_fragment(key: bytes) -> str:
    _check_key(key)
    return pf.b64url_encode(key)


def key_from_fragment(text: str) -> bytes:
    try:
        return pf.decode_key(text)
    except pf.PublicationFormatError as e:
        raise GuestFormatError("That link's key is not valid") from e


def _check_key(key: bytes) -> None:
    if not isinstance(key, bytes | bytearray) or len(key) != KEY_SIZE:
        raise GuestFormatError("A guest key is 32 bytes")


def _hkdf(key: bytes, info: bytes) -> bytes:
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.kdf.hkdf import HKDF

    _check_key(key)
    return HKDF(algorithm=hashes.SHA256(), length=32, salt=b"", info=info).derive(bytes(key))


def frames_key(key: bytes) -> bytes:
    return _hkdf(key, INFO_FRAMES)


def room_token(key: bytes) -> str:
    return pf.b64url_encode(_hkdf(key, INFO_ROOM))


def media_name_key(key: bytes) -> bytes:
    return _hkdf(key, INFO_MEDIA_NAME)


def check_sha(sha: str) -> str:
    if not isinstance(sha, str) or not _SHA_RE.match(sha):
        raise GuestFormatError("A media sha is 64 lowercase hex characters")
    return sha


def media_name(key: bytes, sha: str) -> str:
    """The name a room stores the blob of plaintext `sha` under (64 hex characters)."""
    import hmac

    return hmac.new(media_name_key(key), check_sha(sha).encode("ascii"), "sha256").hexdigest()


# ---- sealing -------------------------------------------------------------------------


def _kind(kind: int) -> Kind:
    try:
        return Kind(kind)
    except ValueError as e:
        raise GuestFormatError(f"Unknown frame kind {kind!r}") from e


def block_for(kind: int) -> int:
    k = _kind(kind)
    if k is Kind.SNAPSHOT:
        return SNAPSHOT_BLOCK
    return MEDIA_BLOCK if k is Kind.MEDIA else BLOCK


def aad(room: str, kind: int, sha: str | None = None) -> bytes:
    """`sha` (the plaintext's SHA-256, hex) is required for MEDIA and refused for other kinds."""
    k = _kind(kind)
    base = f"{AAD_PREFIX}|{check_room(room)}|{int(k)}"
    if k is Kind.MEDIA:
        if sha is None:
            raise GuestFormatError("A media frame needs its sha")
        return f"{base}|{check_sha(sha)}".encode()
    if sha is not None:
        raise GuestFormatError("Only a media frame has a sha")
    return base.encode()


def seal(
    key: bytes,
    room: str,
    kind: int,
    plaintext: bytes,
    nonce: bytes | None = None,
    sha: str | None = None,
) -> bytes:
    """Pad and encrypt into an envelope. `nonce` is for the test vectors only."""
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    k = _kind(kind)
    additional = aad(room, k, sha)
    try:
        padded = pf.pad(bytes(plaintext), block_for(k))
    except pf.PublicationFormatError as e:
        raise GuestFormatError(str(e)) from e
    if nonce is None:
        nonce = secrets.token_bytes(NONCE_SIZE)
    elif len(nonce) != NONCE_SIZE:
        raise GuestFormatError("A nonce is 12 bytes")
    sealed = AESGCM(frames_key(key)).encrypt(nonce, padded, additional)
    return bytes([k]) + nonce + sealed


def unseal(key: bytes, room: str, envelope: bytes, sha: str | None = None) -> tuple[Kind, bytes]:
    """Decrypt and unpad. Raises GuestFormatError for a wrong key or room, a changed kind
    byte, a short or altered envelope, or bad padding. A MEDIA envelope opens only with its
    `sha` (use `unseal_media`, which also checks the bytes against it)."""
    from cryptography.exceptions import InvalidTag
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    check_room(room)
    if len(envelope) < 1 + NONCE_SIZE + TAG_SIZE:
        raise GuestFormatError("The data is too short to be a guest frame")
    kind = _kind(envelope[0])
    nonce = envelope[1 : 1 + NONCE_SIZE]
    try:
        padded = AESGCM(frames_key(key)).decrypt(
            nonce, envelope[1 + NONCE_SIZE :], aad(room, kind, sha)
        )
    except InvalidTag as e:
        raise GuestFormatError(
            "Could not decrypt the frame: wrong key or room, or it was altered"
        ) from e
    try:
        return kind, pf.unpad(padded, block_for(kind))
    except pf.PublicationFormatError as e:
        raise GuestFormatError(str(e)) from e


# ---- notes frames --------------------------------------------------------------------


def _truncate(text: str, limit: int = MAX_FIELD) -> bytes:
    data = text.encode("utf-8")
    if len(data) <= limit:
        return data
    return data[:limit].decode("utf-8", errors="ignore").encode("utf-8")


def notes_plaintext(gid: str, name: str, frame: bytes) -> bytes:
    gid_b = gid.encode("utf-8")
    if len(gid_b) > MAX_FIELD:
        raise GuestFormatError("A guest id is at most 255 bytes")
    name_b = _truncate(name)
    frame = bytes(frame)
    if not frame or frame[0] not in FRAME_START:
        raise GuestFormatError("A notes frame starts with 0xDE or 0xAF")
    return bytes([len(gid_b)]) + gid_b + bytes([len(name_b)]) + name_b + frame


def parse_notes(plaintext: bytes) -> tuple[str, str, bytes]:
    pos = 0
    fields: list[str] = []
    for _ in range(2):
        if pos >= len(plaintext):
            raise GuestFormatError("The notes frame is truncated")
        n = plaintext[pos]
        raw = plaintext[pos + 1 : pos + 1 + n]
        if len(raw) != n:
            raise GuestFormatError("The notes frame is truncated")
        try:
            fields.append(raw.decode("utf-8"))
        except UnicodeDecodeError as e:
            raise GuestFormatError("The notes frame has a bad name") from e
        pos += 1 + n
    frame = plaintext[pos:]
    if not frame or frame[0] not in FRAME_START:
        raise GuestFormatError("A notes frame starts with 0xDE or 0xAF")
    return fields[0], fields[1], bytes(frame)


def seal_notes(
    key: bytes, room: str, gid: str, name: str, frame: bytes, nonce: bytes | None = None
) -> bytes:
    return seal(key, room, Kind.NOTES, notes_plaintext(gid, name, frame), nonce)


def unseal_notes(key: bytes, room: str, envelope: bytes) -> tuple[str, str, bytes]:
    kind, plain = unseal(key, room, envelope)
    if kind is not Kind.NOTES:
        raise GuestFormatError("That is not a notes frame")
    return parse_notes(plain)


# ---- JSON frames ---------------------------------------------------------------------


def canonical_json(obj: object) -> bytes:
    try:
        return pf.canonical_json(obj)
    except (TypeError, ValueError) as e:
        raise GuestFormatError("That value cannot be written as JSON") from e


def seal_json(key: bytes, room: str, kind: int, obj: object, nonce: bytes | None = None) -> bytes:
    if _kind(kind) not in JSON_KINDS:
        raise GuestFormatError("That frame kind does not carry JSON")
    return seal(key, room, kind, canonical_json(obj), nonce)


def unseal_json(key: bytes, room: str, envelope: bytes) -> tuple[Kind, object]:
    kind, plain = unseal(key, room, envelope)
    if kind not in JSON_KINDS:
        raise GuestFormatError("That frame kind does not carry JSON")
    try:
        return kind, json.loads(plain.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as e:
        raise GuestFormatError("The frame is not valid JSON") from e


# ---- media frames --------------------------------------------------------------------


def seal_media(key: bytes, room: str, sha: str, data: bytes, nonce: bytes | None = None) -> bytes:
    """Seal an image's bytes under the sha they must hash to (checked here too)."""
    import hashlib

    data = bytes(data)
    if len(data) > MEDIA_MAX:
        raise GuestFormatError(f"A media blob is at most {MEDIA_MAX} bytes")
    if hashlib.sha256(data).hexdigest() != check_sha(sha):
        raise GuestFormatError("The media bytes do not match their sha")
    return seal(key, room, Kind.MEDIA, data, nonce, sha)


def unseal_media(key: bytes, room: str, sha: str, envelope: bytes) -> bytes:
    """The bytes of the blob sealed under `sha`; refuses any other kind, a wrong sha, and
    plaintext that does not hash to `sha`."""
    import hashlib

    if len(envelope) > 0 and envelope[0] != Kind.MEDIA:
        raise GuestFormatError("That is not a media frame")
    _, data = unseal(key, room, envelope, check_sha(sha))
    if len(data) > MEDIA_MAX:
        raise GuestFormatError(f"A media blob is at most {MEDIA_MAX} bytes")
    if hashlib.sha256(data).hexdigest() != sha:
        raise GuestFormatError("The media bytes do not match their sha")
    return data
