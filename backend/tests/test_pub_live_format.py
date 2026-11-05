"""The live-publication wire format (services/pub_live_format.py) and its test vectors."""

import hashlib
import importlib.util
import json
from pathlib import Path

import pytest

from mnemosyne.services import guest_format as gf
from mnemosyne.services import pub_live_format as pl
from mnemosyne.services import publication_format as pf

VECTORS = Path(__file__).parent / "vectors" / "publication_live_v1.json"
SCRIPT = Path(__file__).parent.parent / "scripts" / "make_live_vectors.py"
KEY = bytes(range(32))
ROOM = "0123456789abcdef"
OTHER_ROOM = "fedcba9876543210"
OWNER = pl.OwnerKeys(sign=bytes(range(1, 33)), box=bytes(range(33, 65)))
OTHER_OWNER = pl.OwnerKeys(sign=bytes(range(65, 97)), box=bytes(range(97, 129)))
READER = bytes(range(129, 161))
READER_2 = bytes(range(161, 193))
OP = {"op": "create", "id": "a1b2c3d4e5f6", "text": "Hi 🙂", "name": "Ana", "anchor": {}}


@pytest.fixture(scope="module")
def vec():
    return json.loads(VECTORS.read_text(encoding="utf-8"))


# ---- namespace ----


def test_space_is_its_own_namespace():
    assert pl.frames_key(KEY) != gf.frames_key(KEY)
    assert pl.room_token(KEY) != gf.room_token(KEY)
    assert pl.aad(ROOM, 1) == f"mnemosyne/pub-live/1|{ROOM}|1".encode()
    # a guest frame never opens as a live frame, nor the reverse, under the same key and room
    guest = gf.seal_json(KEY, ROOM, gf.Kind.SNAPSHOT, {"a": 1})
    with pytest.raises(pl.PubLiveFormatError):
        pl.unseal(KEY, ROOM, guest)
    live = pl.sign_frame(KEY, ROOM, pl.Kind.THREADS, {"a": 1}, OWNER.sign)
    with pytest.raises(gf.GuestFormatError):
        gf.unseal(KEY, ROOM, live)
    with pytest.raises(pl.PubLiveFormatError):  # pub-live has no kind 5 or 6
        pl.seal(KEY, ROOM, 5, b"x")
    with pytest.raises(gf.GuestFormatError):  # and no media
        gf.media_name_key(KEY, space=pl.PUB_LIVE)


def test_every_kind_pads_to_256():
    overhead = 1 + 12 + 16
    for kind in pl.Kind:
        assert len(pl.seal(KEY, ROOM, kind, b"x" * 252)) == overhead + 256
        assert len(pl.seal(KEY, ROOM, kind, b"x" * 253)) == overhead + 512
        assert gf.block_for(kind, space=pl.PUB_LIVE) == 256


# ---- keys ----


def test_keys_and_rid():
    keys = pl.OwnerKeys.new()
    assert len(keys.sign) == len(keys.box) == 32 and keys.sign != keys.box
    live = keys.live()
    assert pl.public_keys(live) == (keys.sign_public, keys.box_public)
    assert keys.sign.hex() not in repr(keys) and keys.box.hex() not in repr(keys)
    pub = pl.sign_public(READER)
    assert pl.rid_of(pub) == hashlib.sha256(pub).hexdigest()[:16]
    assert pl.rid_of(pub) != pl.rid_of(pl.sign_public(READER_2))
    with pytest.raises(pl.PubLiveFormatError):
        pl.rid_of(pub[:31])
    with pytest.raises(pl.PubLiveFormatError):
        pl.OwnerKeys(sign=b"short", box=bytes(32))
    # X25519 agrees both ways
    eph = pl.new_private()
    assert pl.x25519(eph, OWNER.box_public) == pl.x25519(OWNER.box, pl.box_public(eph))


def test_live_keys_model():
    live = OWNER.live()
    assert len(live.sign) == len(live.box) == 64
    for bad in ["", "A" * 64, "a" * 63, "g" * 64, "a" * 65]:
        with pytest.raises(ValueError):
            pf.LiveKeys(sign=bad, box=live.box)
    with pytest.raises(ValueError):
        pf.LiveKeys(sign=live.sign, box=live.box, extra="x")


# ---- owner frames ----


@pytest.mark.parametrize("kind", [pl.Kind.VERSION, pl.Kind.THREADS, pl.Kind.META])
def test_signed_round_trip(kind):
    body = {"version": 3, "text": "Café 🙂", "at": "2026-11-05T10:00:00Z"}
    env = pl.sign_frame(KEY, ROOM, kind, body, OWNER.sign)
    assert env[0] == kind
    assert pl.open_signed(KEY, ROOM, env, OWNER.sign_public) == (kind, body)
    assert pl.open_signed(KEY, ROOM, env, OWNER.sign_public, kind) == (kind, body)
    _, plain = pl.unseal(KEY, ROOM, env)
    sig, raw = plain[:64], plain[64:]
    assert raw == pf.canonical_json(body)
    message = f"mnemosyne/pub-live/1|{ROOM}|{int(kind)}|".encode() + raw
    assert pl.signed_message(ROOM, kind, raw) == message
    assert sig == pl.ed25519_sign(OWNER.sign, message)  # Ed25519 is deterministic


def test_signed_frame_refusals():
    body = {"version": 3}
    env = pl.sign_frame(KEY, ROOM, pl.Kind.VERSION, body, OWNER.sign)
    with pytest.raises(pl.PubLiveFormatError, match="signed"):  # another owner's key
        pl.open_signed(KEY, ROOM, env, OTHER_OWNER.sign_public)
    with pytest.raises(pl.PubLiveFormatError):  # expected another kind
        pl.open_signed(KEY, ROOM, env, OWNER.sign_public, pl.Kind.META)
    with pytest.raises(pl.PubLiveFormatError):  # another room (AAD)
        pl.open_signed(KEY, OTHER_ROOM, env, OWNER.sign_public)
    # signed by a reader (who holds the frames key) or by nobody: refused
    forged = pl.sign_frame(KEY, ROOM, pl.Kind.THREADS, {"threads": []}, READER)
    with pytest.raises(pl.PubLiveFormatError, match="signed"):
        pl.open_signed(KEY, ROOM, forged, OWNER.sign_public)
    unsigned = pl.seal(
        KEY, ROOM, pl.Kind.META, pf.canonical_json({"comments": False} | {"x": "y" * 80})
    )
    with pytest.raises(pl.PubLiveFormatError, match="signed"):
        pl.open_signed(KEY, ROOM, unsigned, OWNER.sign_public)
    with pytest.raises(pl.PubLiveFormatError, match="short"):
        pl.open_signed(KEY, ROOM, pl.seal(KEY, ROOM, pl.Kind.META, b"{}"), OWNER.sign_public)
    # the signature binds room and kind: re-sealing the same plaintext elsewhere fails
    _, plain = pl.unseal(KEY, ROOM, env)
    moved = pl.seal(KEY, OTHER_ROOM, pl.Kind.VERSION, plain)
    with pytest.raises(pl.PubLiveFormatError, match="signed"):
        pl.open_signed(KEY, OTHER_ROOM, moved, OWNER.sign_public)
    as_meta = pl.seal(KEY, ROOM, pl.Kind.META, plain)
    with pytest.raises(pl.PubLiveFormatError, match="signed"):
        pl.open_signed(KEY, ROOM, as_meta, OWNER.sign_public)
    # a changed body under the old signature
    changed = pl.seal(KEY, ROOM, pl.Kind.VERSION, plain[:64] + b'{"version":4}')
    with pytest.raises(pl.PubLiveFormatError, match="signed"):
        pl.open_signed(KEY, ROOM, changed, OWNER.sign_public)
    # COMMENT is never an owner frame, and bodies are objects
    with pytest.raises(pl.PubLiveFormatError):
        pl.sign_frame(KEY, ROOM, pl.Kind.COMMENT, body, OWNER.sign)
    with pytest.raises(pl.PubLiveFormatError):
        pl.sign_frame(KEY, ROOM, pl.Kind.META, [1, 2], OWNER.sign)
    box = pl.box_op(KEY, ROOM, OP, READER, OWNER.box_public)
    with pytest.raises(pl.PubLiveFormatError):
        pl.open_signed(KEY, ROOM, box, OWNER.sign_public)


# ---- reader frames ----


def test_box_round_trip():
    env = pl.box_op(KEY, ROOM, OP, READER, OWNER.box_public)
    assert env[0] == pl.Kind.COMMENT
    got = pl.open_box(KEY, ROOM, env, OWNER.box)
    assert got.op == OP
    assert got.reader_pub == pl.sign_public(READER)
    assert got.rid == pl.rid_of(pl.sign_public(READER))
    # a fresh ephemeral key per frame
    assert (
        pl.box_op(KEY, ROOM, OP, READER, OWNER.box_public)[13:45]
        != (pl.unseal(KEY, ROOM, env)[1][:32])
    )
    # a replayed box opens again: idempotence is the bridge's
    assert pl.open_box(KEY, ROOM, env, OWNER.box) == got


def test_box_layout():
    eph = bytes(range(200, 232))
    nonce = bytes(range(12))
    plain = pl.box_plaintext(ROOM, OP, READER, OWNER.box_public, eph_private=eph, box_nonce=nonce)
    eph_pub = pl.box_public(eph)
    assert plain[:32] == eph_pub and plain[32:44] == nonce
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    shared = pl.x25519(OWNER.box, eph_pub)
    aes_key = pl.box_key(shared, ROOM)
    aad = f"mnemosyne/pub-live/1|{ROOM}|4|{eph_pub.hex()}".encode()
    assert pl.box_aad(ROOM, eph_pub) == aad
    content = AESGCM(aes_key).decrypt(nonce, plain[44:], aad)
    reader_pub = pl.sign_public(READER)
    op = pf.canonical_json(OP)
    assert content[64:96] == reader_pub and content[96:] == op
    message = f"mnemosyne/pub-live/1|{ROOM}|4|".encode() + reader_pub + op
    assert content[:64] == pl.ed25519_sign(READER, message)
    # no inner padding: the envelope pads
    assert len(plain) == 32 + 12 + 64 + 32 + len(op) + 16


def test_box_refusals():
    env = pl.box_op(KEY, ROOM, OP, READER, OWNER.box_public)
    with pytest.raises(pl.PubLiveFormatError, match="owner"):  # another owner's box key
        pl.open_box(KEY, ROOM, env, OTHER_OWNER.box)
    with pytest.raises(pl.PubLiveFormatError):  # another room
        pl.open_box(KEY, OTHER_ROOM, env, OWNER.box)
    with pytest.raises(pl.PubLiveFormatError):  # an owner frame is not a comment
        pl.open_box(KEY, ROOM, pl.sign_frame(KEY, ROOM, 1, {}, OWNER.sign), OWNER.box)
    # a box made for another owner, readable by nobody but that owner
    other = pl.box_op(KEY, ROOM, OP, READER, OTHER_OWNER.box_public)
    assert pl.open_box(KEY, ROOM, other, OTHER_OWNER.box).op == OP
    with pytest.raises(pl.PubLiveFormatError):
        pl.open_box(KEY, ROOM, other, OWNER.box)
    # a reader cannot open another reader's comment with their own keys
    for wrong in (READER, READER_2):
        with pytest.raises(pl.PubLiveFormatError):
            pl.open_box(KEY, ROOM, env, wrong)
    # the signature must match the reader_pub inside: swap in READER_2's public key
    eph = bytes(range(200, 232))
    nonce = bytes(range(12))
    eph_pub = pl.box_public(eph)
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    aes = AESGCM(pl.box_key(pl.x25519(eph, OWNER.box_public), ROOM))
    op = pf.canonical_json(OP)
    sig = pl.ed25519_sign(READER, pl.reader_message(ROOM, pl.sign_public(READER), op))
    lying = sig + pl.sign_public(READER_2) + op
    plain = eph_pub + nonce + aes.encrypt(nonce, lying, pl.box_aad(ROOM, eph_pub))
    with pytest.raises(pl.PubLiveFormatError, match="signed"):
        pl.open_box(KEY, ROOM, pl.seal(KEY, ROOM, pl.Kind.COMMENT, plain), OWNER.box)
    # a box signed for another room
    sig = pl.ed25519_sign(READER, pl.reader_message(OTHER_ROOM, pl.sign_public(READER), op))
    wrong_room = sig + pl.sign_public(READER) + op
    plain = eph_pub + nonce + aes.encrypt(nonce, wrong_room, pl.box_aad(ROOM, eph_pub))
    with pytest.raises(pl.PubLiveFormatError, match="signed"):
        pl.open_box(KEY, ROOM, pl.seal(KEY, ROOM, pl.Kind.COMMENT, plain), OWNER.box)
    # a changed ephemeral key (in the inner AAD and the key agreement)
    _, inner = pl.unseal(KEY, ROOM, env)
    swapped = pl.box_public(bytes(range(1, 33))) + inner[32:]
    with pytest.raises(pl.PubLiveFormatError):
        pl.open_box(KEY, ROOM, pl.seal(KEY, ROOM, pl.Kind.COMMENT, swapped), OWNER.box)
    # a low-order ephemeral point (zero shared secret) is refused, not used
    zero = bytes(32) + inner[32:]
    with pytest.raises(pl.PubLiveFormatError):
        pl.open_box(KEY, ROOM, pl.seal(KEY, ROOM, pl.Kind.COMMENT, zero), OWNER.box)
    with pytest.raises(pl.PubLiveFormatError, match="short"):
        pl.open_box(KEY, ROOM, pl.seal(KEY, ROOM, pl.Kind.COMMENT, inner[:100]), OWNER.box)
    with pytest.raises(pl.PubLiveFormatError):
        pl.box_op(KEY, ROOM, ["not", "an", "object"], READER, OWNER.box_public)


# ---- the manifest's `live` ----


def _manifest(**kw):
    base = {
        "id": "0123456789abcdef",
        "version": 1,
        "published_at": "2026-11-05T09:30:00Z",
        "title": "T",
        "date": "2026-11-05",
    }
    return pf.Manifest(**{**base, **kw})


def test_manifest_live_is_additive():
    plain = pf.manifest_bytes(_manifest())
    assert b"live" not in plain
    m = _manifest(live=OWNER.live())
    data = pf.manifest_bytes(m)
    assert json.loads(data)["live"] == {
        "sign": OWNER.sign_public.hex(),
        "box": OWNER.box_public.hex(),
    }
    assert pf.parse_manifest(data) == m
    assert pf.parse_manifest(plain).live is None
    key = pf.new_key()
    blob, _ = pf.seal_manifest(m, key)
    assert pf.open_manifest(m.id, 1, blob, key).live == OWNER.live()
    # an unknown field inside `live` is still refused
    raw = json.loads(data)
    raw["live"]["extra"] = 1
    with pytest.raises(pf.PublicationFormatError):
        pf.parse_manifest(pf.canonical_json(raw))


# ---- vectors ----


def test_vectors_open(vec):
    key = pf.decode_key(vec["key_b64url"])
    room = vec["room"]
    assert pl.frames_key(key).hex() == vec["frames_key_hex"]
    assert pl.room_token(key) == vec["room_token"]
    o = vec["owner"]
    owner = pl.OwnerKeys(
        sign=bytes.fromhex(o["sign_private_hex"]), box=bytes.fromhex(o["box_private_hex"])
    )
    assert owner.sign_public.hex() == o["sign_public_hex"]
    assert owner.box_public.hex() == o["box_public_hex"]
    r = vec["reader"]
    reader = bytes.fromhex(r["private_hex"])
    assert pl.sign_public(reader).hex() == r["public_hex"]
    assert pl.rid_of(bytes.fromhex(r["public_hex"])) == r["rid"]

    m = vec["manifest"]
    blob = pf.b64url_decode(m["blob_b64url"])
    assert pf.blob_hash(blob) == m["sha256"]
    assert pf.aad(vec["pub_id"], "manifest", m["version"]).decode() == m["aad"]
    manifest = pf.open_manifest(vec["pub_id"], m["version"], blob, key)
    assert pf.manifest_bytes(manifest) == m["json"].encode()
    assert manifest.live == owner.live()
    assert pf.seal_manifest(manifest, key, nonce=bytes.fromhex(m["nonce_hex"]))[0] == blob

    sign_pub, box_pub = pl.public_keys(manifest.live)
    assert set(vec["samples"]) == {"version", "threads", "meta", "comment"}
    for label in ("version", "threads", "meta"):
        s = vec["samples"][label]
        env = pf.b64url_decode(s["envelope_b64url"])
        assert env[0] == s["kind"] and env[1:13].hex() == s["nonce_hex"]
        assert pl.aad(room, s["kind"]).decode() == s["aad"]
        kind, body = pl.open_signed(key, room, env, sign_pub)
        assert kind == s["kind"] and pf.canonical_json(body).decode() == s["json"], label
        _, plain = pl.unseal(key, room, env)
        assert plain.hex() == s["plaintext_hex"]
        assert len(pf.pad(plain, 256)) == s["padded_length"]
        assert pl.signed_message(room, kind, s["json"].encode()).hex() == s["signed_message_hex"]
        assert plain[:64].hex() == s["sig_hex"]
        assert (
            pl.sign_frame(key, room, kind, body, owner.sign, bytes.fromhex(s["nonce_hex"])) == env
        )
        with pytest.raises(pl.PubLiveFormatError):  # not under the reader's key
            pl.open_signed(key, room, env, bytes.fromhex(r["public_hex"]))
    version = pl.open_signed(
        key, room, pf.b64url_decode(vec["samples"]["version"]["envelope_b64url"]), sign_pub
    )[1]
    assert version["sha256"] == m["sha256"] and version["version"] == m["version"]

    c = vec["samples"]["comment"]
    env = pf.b64url_decode(c["envelope_b64url"])
    assert env[0] == c["kind"] == 4 and env[1:13].hex() == c["nonce_hex"]
    got = pl.open_box(key, room, env, owner.box)
    assert got.rid == c["rid"] == r["rid"]
    assert pf.canonical_json(got.op).decode() == c["json"]
    eph = bytes.fromhex(c["eph_private_hex"])
    assert pl.box_public(eph).hex() == c["eph_public_hex"]
    assert pl.x25519(eph, box_pub).hex() == c["shared_hex"]
    assert pl.x25519(owner.box, bytes.fromhex(c["eph_public_hex"])).hex() == c["shared_hex"]
    assert pl.box_key(bytes.fromhex(c["shared_hex"]), room).hex() == c["box_key_hex"]
    assert pl.box_aad(room, bytes.fromhex(c["eph_public_hex"])).decode() == c["box_aad"]
    msg = pl.reader_message(room, got.reader_pub, c["json"].encode())
    assert msg.hex() == c["reader_message_hex"]
    assert pl.ed25519_sign(reader, msg).hex() == c["reader_sig_hex"]
    assert c["content_hex"] == c["reader_sig_hex"] + r["public_hex"] + c["json"].encode().hex()
    _, plain = pl.unseal(key, room, env)
    assert plain.hex() == c["plaintext_hex"]
    assert len(pf.pad(plain, 256)) == c["padded_length"]
    assert (
        pl.box_op(
            key,
            room,
            got.op,
            reader,
            box_pub,
            eph_private=eph,
            box_nonce=bytes.fromhex(c["box_nonce_hex"]),
            nonce=bytes.fromhex(c["nonce_hex"]),
        )
        == env
    )

    assert set(vec["refused"]) == {"threads_signed_by_reader", "version_unsigned"}
    for text in vec["refused"].values():
        bad = pf.b64url_decode(text)
        pl.unseal(key, room, bad)  # it is a well-sealed frame ...
        with pytest.raises(pl.PubLiveFormatError, match="signed"):  # ... that does not verify
            pl.open_signed(key, room, bad, sign_pub)


def test_vectors_reproduce(vec):
    # The committed file is exactly what the script writes now.
    spec = importlib.util.spec_from_file_location("make_live_vectors", SCRIPT)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    assert mod.build() == vec
    assert json.dumps(mod.build(), indent=2, ensure_ascii=False) + "\n" == VECTORS.read_text(
        encoding="utf-8"
    )
