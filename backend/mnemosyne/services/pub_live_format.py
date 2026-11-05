"""The live-publication wire format (docs/plans/2026-11-05-live-publications.md, "Data and
formats"): frames the owner's backend (the bridge) and the readers of a publication (the viewer)
exchange through a guest-notes room on the publication's host, which can read none of them.

The viewer (src/lib/sealed/signed.ts, viewer/src/live.ts) implements the same rules;
backend/tests/vectors/publication_live_v1.json ties them together
(scripts/make_live_vectors.py). Change one, change the other and the vectors.

Rules, exactly as implemented:

- the room is a guest-notes room (16 lowercase hex characters); the key is the publication's
  link key (32 bytes, the only secret in the link). The envelope is guest_format's in the
  `PUB_LIVE` space: kind(1) || nonce(12) || AES-256-GCM(frames_key, nonce, padded), padded to a
  multiple of 256 bytes for EVERY kind (THREADS, also the room's snapshot, included), AAD = UTF-8
  of "mnemosyne/pub-live/1|<room>|<kind as decimal>".
- `frames_key = HKDF-SHA256(key, salt=b"", info=b"mnemosyne/pub/frames", length=32)`;
  `room_token = b64url(HKDF-SHA256(key, salt=b"", info=b"mnemosyne/pub/room", length=32))`.
- kinds: VERSION=1, THREADS=2, META=3 (owner to readers, signed), COMMENT=4 (reader to owner,
  boxed and signed).
- owner frames: plaintext = sig(64) || body, body = canonical JSON of a JSON object (sorted keys,
  no whitespace, UTF-8, non-ASCII as is), sig = Ed25519 (RFC 8032, pure) by the owner's signing
  key over UTF-8("mnemosyne/pub-live/1|<room>|<kind>|") || body. Opening verifies against the
  manifest's `live.sign` and parses the body; the body bytes are not re-canonicalised.
- COMMENT: plaintext = eph(32) || nonce(12) || AES-256-GCM(box_key, nonce, content) (16-byte tag
  at the end, content NOT padded: the envelope pads), where eph is a fresh X25519 public key per
  frame, shared = X25519(eph_private, owner box public key) (RFC 7748; an all-zero result is
  refused), box_key = HKDF-SHA256(shared, salt=UTF-8 of the 16-character room id,
  info=b"mnemosyne/pub/box", length=32), and the inner AAD = UTF-8 of
  "mnemosyne/pub-live/1|<room>|4|<eph as 64 lowercase hex>".
  content = sig(64) || reader_pub(32) || op, op = canonical JSON of a JSON object, sig = Ed25519
  by the reader's key over UTF-8("mnemosyne/pub-live/1|<room>|4|") || reader_pub || op.
- `rid` = the first 16 lowercase hex characters of SHA-256(reader_pub) (the raw 32 bytes).
- keys are raw 32-byte private keys (Ed25519 seed, X25519 scalar before clamping); public keys
  are raw 32 bytes, written as 64 lowercase hex characters in `Manifest.live`.

This module only seals, signs and checks; the shapes of the JSON bodies belong to the bridge
(services/live_publications.py) and the viewer. Idempotence of a replayed COMMENT is the
bridge's: `open_box` opens the same frame as often as it is given it.
"""

from __future__ import annotations

import hashlib
import json
import secrets
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from enum import IntEnum

from . import guest_format as gf
from . import publication_format as pf

FORMAT = 1
AAD_PREFIX = f"mnemosyne/pub-live/{FORMAT}"
INFO_FRAMES = b"mnemosyne/pub/frames"
INFO_ROOM = b"mnemosyne/pub/room"
INFO_BOX = b"mnemosyne/pub/box"
SIG_SIZE = 64
PUBLIC_SIZE = 32
PRIVATE_SIZE = 32
NONCE_SIZE = gf.NONCE_SIZE
TAG_SIZE = gf.TAG_SIZE
RID_HEX_LEN = 16


class Kind(IntEnum):
    VERSION = 1
    THREADS = 2
    META = 3
    COMMENT = 4


OWNER_KINDS = (Kind.VERSION, Kind.THREADS, Kind.META)

PUB_LIVE = gf.Space(
    aad_prefix=AAD_PREFIX,
    info_frames=INFO_FRAMES,
    info_room=INFO_ROOM,
    kinds=Kind,
)


class PubLiveFormatError(ValueError):
    """The data cannot be sealed, signed or opened; the message says why."""


@contextmanager
def _errors() -> Iterator[None]:
    try:
        yield
    except gf.GuestFormatError as e:
        raise PubLiveFormatError(str(e)) from e


# ---- keys ----------------------------------------------------------------------------


def _check_private(raw: bytes) -> bytes:
    if not isinstance(raw, bytes | bytearray) or len(raw) != PRIVATE_SIZE:
        raise PubLiveFormatError("A private key is 32 bytes")
    return bytes(raw)


def _check_public(raw: bytes) -> bytes:
    if not isinstance(raw, bytes | bytearray) or len(raw) != PUBLIC_SIZE:
        raise PubLiveFormatError("A public key is 32 bytes")
    return bytes(raw)


def _ed_private(raw: bytes):
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    return Ed25519PrivateKey.from_private_bytes(_check_private(raw))


def _x_private(raw: bytes):
    from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey

    return X25519PrivateKey.from_private_bytes(_check_private(raw))


def new_private() -> bytes:
    """32 random bytes: an Ed25519 seed or an X25519 scalar, whichever it is used as."""
    return secrets.token_bytes(PRIVATE_SIZE)


def sign_public(private: bytes) -> bytes:
    """The Ed25519 public key of a raw signing key."""
    return _ed_private(private).public_key().public_bytes_raw()


def box_public(private: bytes) -> bytes:
    """The X25519 public key of a raw box key."""
    return _x_private(private).public_key().public_bytes_raw()


def ed25519_sign(private: bytes, message: bytes) -> bytes:
    """A raw 64-byte Ed25519 signature."""
    return _ed_private(private).sign(message)


def x25519(private: bytes, public: bytes) -> bytes:
    """The X25519 shared secret of a raw private and a raw public key (zero refused)."""
    return _exchange(_x_private(private), public)


def rid_of(reader_pub: bytes) -> str:
    """A reader's id: the first 16 hex characters of SHA-256 of their Ed25519 public key."""
    return hashlib.sha256(_check_public(reader_pub)).hexdigest()[:RID_HEX_LEN]


@dataclass(frozen=True)
class OwnerKeys:
    """A publication's owner keys: raw private bytes as the publications row keeps them."""

    sign: bytes  # Ed25519 seed
    box: bytes  # X25519 scalar

    def __post_init__(self) -> None:
        _check_private(self.sign)
        _check_private(self.box)

    @classmethod
    def new(cls) -> OwnerKeys:
        return cls(sign=new_private(), box=new_private())

    @property
    def sign_public(self) -> bytes:
        return sign_public(self.sign)

    @property
    def box_public(self) -> bytes:
        return box_public(self.box)

    def live(self) -> pf.LiveKeys:
        """The public halves, for `Manifest.live`."""
        return pf.LiveKeys(sign=self.sign_public.hex(), box=self.box_public.hex())

    def __repr__(self) -> str:  # never print private keys
        return f"OwnerKeys(sign_public={self.sign_public.hex()})"


def public_keys(live: pf.LiveKeys) -> tuple[bytes, bytes]:
    """(sign, box) public keys as raw bytes from a manifest's `live`."""
    return bytes.fromhex(live.sign), bytes.fromhex(live.box)


# ---- derivation and the envelope -----------------------------------------------------


def new_room() -> str:
    return gf.new_room()


def frames_key(key: bytes) -> bytes:
    with _errors():
        return gf.frames_key(key, space=PUB_LIVE)


def room_token(key: bytes) -> str:
    with _errors():
        return gf.room_token(key, space=PUB_LIVE)


def aad(room: str, kind: int) -> bytes:
    with _errors():
        return gf.aad(room, kind, space=PUB_LIVE)


def seal(key: bytes, room: str, kind: int, plaintext: bytes, nonce: bytes | None = None) -> bytes:
    """The bare envelope (no signature, no box): for the vectors and the refusal tests."""
    with _errors():
        return gf.seal(key, room, kind, plaintext, nonce, space=PUB_LIVE)


def unseal(key: bytes, room: str, envelope: bytes) -> tuple[Kind, bytes]:
    with _errors():
        kind, plain = gf.unseal(key, room, envelope, space=PUB_LIVE)
    return Kind(kind), plain


def _prefix(room: str, kind: int) -> bytes:
    """What a signature's subject starts with: UTF-8 "mnemosyne/pub-live/1|<room>|<kind>|"."""
    with _errors():
        gf.check_room(room)
    return f"{AAD_PREFIX}|{room}|{int(Kind(kind))}|".encode()


def _json_object(obj: object) -> bytes:
    if not isinstance(obj, dict):
        raise PubLiveFormatError("A live frame's body is a JSON object")
    with _errors():
        return gf.canonical_json(obj)


def _parse_object(data: bytes) -> dict:
    try:
        value = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as e:
        raise PubLiveFormatError("The frame is not valid JSON") from e
    if not isinstance(value, dict):
        raise PubLiveFormatError("A live frame's body is a JSON object")
    return value


def _verify(public: bytes, sig: bytes, message: bytes) -> bool:
    from cryptography.exceptions import InvalidSignature
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

    try:
        Ed25519PublicKey.from_public_bytes(_check_public(public)).verify(sig, message)
    except (InvalidSignature, ValueError):
        return False
    return True


# ---- owner frames: VERSION, THREADS, META --------------------------------------------


def _owner_kind(kind: int) -> Kind:
    try:
        k = Kind(kind)
    except ValueError as e:
        raise PubLiveFormatError(f"Unknown frame kind {kind!r}") from e
    if k not in OWNER_KINDS:
        raise PubLiveFormatError("Only VERSION, THREADS and META are signed by the owner")
    return k


def signed_message(room: str, kind: int, body: bytes) -> bytes:
    """The bytes an owner frame's signature covers."""
    return _prefix(room, _owner_kind(kind)) + body


def signed_plaintext(room: str, kind: int, obj: object, sign_private: bytes) -> bytes:
    """sig(64) || body: what goes inside the envelope of an owner frame."""
    body = _json_object(obj)
    sig = _ed_private(sign_private).sign(signed_message(room, kind, body))
    return sig + body


def sign_frame(
    key: bytes,
    room: str,
    kind: int,
    obj: object,
    sign_private: bytes,
    nonce: bytes | None = None,
) -> bytes:
    """Seal an owner frame signed by the publication's signing key. `nonce`: vectors only."""
    k = _owner_kind(kind)
    return seal(key, room, k, signed_plaintext(room, k, obj, sign_private), nonce)


def open_signed(
    key: bytes, room: str, envelope: bytes, sign_pub: bytes, kind: int | None = None
) -> tuple[Kind, dict]:
    """Open an owner frame and verify it against the manifest's `live.sign`. Raises
    PubLiveFormatError for anything that does not verify (the viewer drops such frames), for a
    COMMENT, and, with `kind`, for any other kind."""
    k, plain = unseal(key, room, envelope)
    _owner_kind(k)
    if kind is not None and k != kind:
        raise PubLiveFormatError("That is not the expected kind of frame")
    if len(plain) < SIG_SIZE:
        raise PubLiveFormatError("The frame is too short to be signed")
    sig, body = plain[:SIG_SIZE], plain[SIG_SIZE:]
    if not _verify(sign_pub, sig, signed_message(room, k, body)):
        raise PubLiveFormatError("The frame is not signed by this publication's owner")
    return k, _parse_object(body)


# ---- reader frames: COMMENT, boxed to the owner and signed by the reader -------------


def box_key(shared: bytes, room: str) -> bytes:
    """HKDF-SHA256(shared, salt = the room id's UTF-8, info = mnemosyne/pub/box)."""
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.kdf.hkdf import HKDF

    with _errors():
        gf.check_room(room)
    return HKDF(
        algorithm=hashes.SHA256(), length=32, salt=room.encode("ascii"), info=INFO_BOX
    ).derive(bytes(shared))


def box_aad(room: str, eph_pub: bytes) -> bytes:
    return _prefix(room, Kind.COMMENT) + _check_public(eph_pub).hex().encode("ascii")


def reader_message(room: str, reader_pub: bytes, op: bytes) -> bytes:
    """The bytes a reader's signature covers."""
    return _prefix(room, Kind.COMMENT) + _check_public(reader_pub) + op


def _exchange(private, public: bytes) -> bytes:
    from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PublicKey

    try:
        shared = private.exchange(X25519PublicKey.from_public_bytes(_check_public(public)))
    except ValueError as e:  # a low-order point: the shared secret would be zero
        raise PubLiveFormatError("That key cannot be used for a box") from e
    if not any(shared):
        raise PubLiveFormatError("That key cannot be used for a box")
    return shared


def box_plaintext(
    room: str,
    op: object,
    reader_private: bytes,
    owner_box_pub: bytes,
    *,
    eph_private: bytes | None = None,
    box_nonce: bytes | None = None,
) -> bytes:
    """eph(32) || nonce(12) || AES-GCM(content): what goes inside a COMMENT envelope.
    `eph_private` and `box_nonce` are for the vectors only."""
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    op_bytes = _json_object(op)
    reader = _ed_private(reader_private)
    reader_pub = reader.public_key().public_bytes_raw()
    content = reader.sign(reader_message(room, reader_pub, op_bytes)) + reader_pub + op_bytes
    eph = _x_private(eph_private if eph_private is not None else new_private())
    eph_pub = eph.public_key().public_bytes_raw()
    aes = AESGCM(box_key(_exchange(eph, owner_box_pub), room))
    if box_nonce is None:
        box_nonce = secrets.token_bytes(NONCE_SIZE)
    elif len(box_nonce) != NONCE_SIZE:
        raise PubLiveFormatError("A nonce is 12 bytes")
    return eph_pub + box_nonce + aes.encrypt(box_nonce, content, box_aad(room, eph_pub))


def box_op(
    key: bytes,
    room: str,
    op: object,
    reader_private: bytes,
    owner_box_pub: bytes,
    *,
    eph_private: bytes | None = None,
    box_nonce: bytes | None = None,
    nonce: bytes | None = None,
) -> bytes:
    """A reader's COMMENT envelope: `op` sealed to the owner's box key and signed by the
    reader. The backend never sends these; it is here for tests and the vectors."""
    plain = box_plaintext(
        room, op, reader_private, owner_box_pub, eph_private=eph_private, box_nonce=box_nonce
    )
    return seal(key, room, Kind.COMMENT, plain, nonce)


@dataclass(frozen=True)
class ReaderOp:
    rid: str
    reader_pub: bytes
    op: dict


def open_box(key: bytes, room: str, envelope: bytes, owner_box_private: bytes) -> ReaderOp:
    """Open a reader's COMMENT with the owner's box key and check the reader's signature.
    Raises PubLiveFormatError for any other kind, another owner's box, an altered frame or a
    signature that does not match the `reader_pub` inside."""
    from cryptography.exceptions import InvalidTag
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    kind, plain = unseal(key, room, envelope)
    if kind is not Kind.COMMENT:
        raise PubLiveFormatError("That is not a comment frame")
    if len(plain) < PUBLIC_SIZE + NONCE_SIZE + TAG_SIZE + SIG_SIZE + PUBLIC_SIZE:
        raise PubLiveFormatError("The comment frame is too short")
    eph_pub = plain[:PUBLIC_SIZE]
    box_nonce = plain[PUBLIC_SIZE : PUBLIC_SIZE + NONCE_SIZE]
    aes = AESGCM(box_key(_exchange(_x_private(owner_box_private), eph_pub), room))
    try:
        content = aes.decrypt(box_nonce, plain[PUBLIC_SIZE + NONCE_SIZE :], box_aad(room, eph_pub))
    except InvalidTag as e:
        raise PubLiveFormatError(
            "Could not open the comment: it is not for this owner, or it was altered"
        ) from e
    if len(content) < SIG_SIZE + PUBLIC_SIZE:
        raise PubLiveFormatError("The comment frame is too short")
    sig = content[:SIG_SIZE]
    reader_pub = content[SIG_SIZE : SIG_SIZE + PUBLIC_SIZE]
    op = content[SIG_SIZE + PUBLIC_SIZE :]
    if not _verify(reader_pub, sig, reader_message(room, reader_pub, op)):
        raise PubLiveFormatError("The comment is not signed by the key it carries")
    return ReaderOp(rid=rid_of(reader_pub), reader_pub=reader_pub, op=_parse_object(op))
