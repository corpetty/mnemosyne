"""The guest-notes wire format (services/guest_format.py) and its test vectors."""

import importlib.util
import json
import re
from pathlib import Path

import pytest

from mnemosyne.services import guest_format as gf
from mnemosyne.services import publication_format as pf

VECTORS = Path(__file__).parent / "vectors" / "guest_v1.json"
SCRIPT = Path(__file__).parent.parent / "scripts" / "make_guest_vectors.py"
ROOM = "0123456789abcdef"
OTHER_ROOM = "fedcba9876543210"
KEY = bytes(range(32))
DELTA = b"\xde\x01\x02hello"


@pytest.fixture(scope="module")
def vec():
    return json.loads(VECTORS.read_text(encoding="utf-8"))


# ---- ids, keys, derivation ----


def test_room_and_key_helpers():
    assert re.fullmatch(r"[0-9a-f]{16}", gf.new_room())
    assert gf.new_room() != gf.new_room()
    key = gf.new_key()
    assert len(key) == 32 and key != gf.new_key()
    text = gf.key_to_fragment(key)
    assert len(text) == 43 and "=" not in text
    assert gf.key_from_fragment(text) == key


@pytest.mark.parametrize("bad", ["", "short", "A" * 44, "!" * 43, "A" * 42 + "B"])
def test_bad_fragment_refused(bad):
    with pytest.raises(gf.GuestFormatError):
        gf.key_from_fragment(bad)


@pytest.mark.parametrize("bad", ["", "ABCDEF0123456789", "0123456789abcde", "0123456789abcdeg"])
def test_bad_room_refused(bad):
    with pytest.raises(gf.GuestFormatError):
        gf.seal(KEY, bad, gf.Kind.NOTES, b"x")


def test_derived_keys_differ_and_depend_on_key():
    assert gf.frames_key(KEY) != KEY and len(gf.frames_key(KEY)) == 32
    assert pf.b64url_decode(gf.room_token(KEY)) != gf.frames_key(KEY)
    assert gf.room_token(KEY) != gf.room_token(bytes(range(1, 33)))
    with pytest.raises(gf.GuestFormatError):
        gf.frames_key(b"short")


# ---- round trips ----


def test_notes_round_trip():
    env = gf.seal_notes(KEY, ROOM, "g1", "Ana", DELTA)
    assert env[0] == gf.Kind.NOTES
    assert gf.unseal_notes(KEY, ROOM, env) == ("g1", "Ana", DELTA)
    kind, plain = gf.unseal(KEY, ROOM, env)
    assert kind is gf.Kind.NOTES and gf.parse_notes(plain) == ("g1", "Ana", DELTA)


@pytest.mark.parametrize(
    ("kind", "obj"),
    [
        (gf.Kind.SNAPSHOT, {"format": 1, "notes": "", "frames": [], "threads": [], "meta": {}}),
        (gf.Kind.COMMENT, {"op": "create", "id": "x", "text": "héllo 🙂", "notes_ids": ["a", "b"]}),
        (gf.Kind.META, {"title": "T", "agenda": [{"text": "a", "covered": False}], "ended": False}),
        (gf.Kind.PRESENCE, {"gid": "g", "name": "Ana", "typing": True}),
    ],
)
def test_json_round_trip(kind, obj):
    env = gf.seal_json(KEY, ROOM, kind, obj)
    assert env[0] == kind
    assert gf.unseal_json(KEY, ROOM, env) == (kind, obj)


def test_json_is_canonical():
    a = gf.seal_json(KEY, ROOM, gf.Kind.META, {"b": 1, "a": "é"}, nonce=bytes(12))
    b = gf.seal_json(KEY, ROOM, gf.Kind.META, {"a": "é", "b": 1}, nonce=bytes(12))
    assert a == b
    _, plain = gf.unseal(KEY, ROOM, a)
    assert plain == '{"a":"é","b":1}'.encode()


def test_json_kinds_only():
    with pytest.raises(gf.GuestFormatError):
        gf.seal_json(KEY, ROOM, gf.Kind.NOTES, {})
    notes = gf.seal_notes(KEY, ROOM, "", "", DELTA)
    with pytest.raises(gf.GuestFormatError):
        gf.unseal_json(KEY, ROOM, notes)
    with pytest.raises(gf.GuestFormatError):
        gf.unseal_notes(KEY, ROOM, gf.seal_json(KEY, ROOM, gf.Kind.META, {}))
    with pytest.raises(gf.GuestFormatError):
        gf.seal_json(KEY, ROOM, gf.Kind.META, {"x": float("nan")})
    garbage = gf.seal(KEY, ROOM, gf.Kind.META, b"not json")
    with pytest.raises(gf.GuestFormatError):
        gf.unseal_json(KEY, ROOM, garbage)


def test_nonces_are_random_and_explicit_nonce_is_used():
    assert gf.seal(KEY, ROOM, 1, b"x") != gf.seal(KEY, ROOM, 1, b"x")
    nonce = bytes(range(12))
    assert gf.seal(KEY, ROOM, 1, b"x", nonce)[1:13] == nonce
    with pytest.raises(gf.GuestFormatError):
        gf.seal(KEY, ROOM, 1, b"x", b"short")


# ---- refusals ----


def test_wrong_key_room_and_tampering():
    env = gf.seal(KEY, ROOM, gf.Kind.NOTES, b"payload")
    with pytest.raises(gf.GuestFormatError):
        gf.unseal(bytes(range(1, 33)), ROOM, env)
    with pytest.raises(gf.GuestFormatError):
        gf.unseal(KEY, OTHER_ROOM, env)
    for kind_byte in (2, 3, 4, 5):  # another valid kind: the AAD no longer matches
        with pytest.raises(gf.GuestFormatError):
            gf.unseal(KEY, ROOM, bytes([kind_byte]) + env[1:])
    for bad_kind in (0, 6, 255):
        with pytest.raises(gf.GuestFormatError):
            gf.unseal(KEY, ROOM, bytes([bad_kind]) + env[1:])
    flipped = bytearray(env)
    flipped[-1] ^= 1
    with pytest.raises(gf.GuestFormatError):
        gf.unseal(KEY, ROOM, bytes(flipped))
    flipped = bytearray(env)
    flipped[5] ^= 1  # nonce
    with pytest.raises(gf.GuestFormatError):
        gf.unseal(KEY, ROOM, bytes(flipped))


@pytest.mark.parametrize("cut", [0, 1, 12, 13, 28, 29])
def test_short_or_truncated_envelope(cut):
    env = gf.seal(KEY, ROOM, gf.Kind.NOTES, b"payload")
    with pytest.raises(gf.GuestFormatError):
        gf.unseal(KEY, ROOM, env[:cut])
    with pytest.raises(gf.GuestFormatError):
        gf.unseal(KEY, ROOM, env[:-1])
    with pytest.raises(gf.GuestFormatError):
        gf.unseal(KEY, ROOM, env + b"\0")


def test_bad_padding_is_refused():
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    nonce = bytes(12)
    padded = b"\0\0\0\x05abc" + b"\0" * 249  # 256 bytes; the length says 5
    sealed = AESGCM(gf.frames_key(KEY)).encrypt(nonce, padded, gf.aad(ROOM, gf.Kind.NOTES))
    # 4+5 = 9 <= 256, and the padding is zeros: this one is valid (reads "abc\0\0")
    assert gf.unseal(KEY, ROOM, bytes([1]) + nonce + sealed)[1] == b"abc\0\0"
    nonzero = b"\0\0\0\x01a" + b"\0" * 250 + b"\x01"
    sealed = AESGCM(gf.frames_key(KEY)).encrypt(nonce, nonzero, gf.aad(ROOM, gf.Kind.NOTES))
    with pytest.raises(gf.GuestFormatError):
        gf.unseal(KEY, ROOM, bytes([1]) + nonce + sealed)
    wrong_block = b"\0\0\0\x01a" + b"\0" * 100  # not a multiple of 256
    sealed = AESGCM(gf.frames_key(KEY)).encrypt(nonce, wrong_block, gf.aad(ROOM, gf.Kind.NOTES))
    with pytest.raises(gf.GuestFormatError):
        gf.unseal(KEY, ROOM, bytes([1]) + nonce + sealed)


# ---- padding ----


def test_padding_hides_lengths_within_a_block():
    overhead = 1 + 12 + 16
    sizes = {len(gf.seal(KEY, ROOM, gf.Kind.PRESENCE, b"x" * n)) for n in range(0, 253)}
    assert sizes == {overhead + 256}
    assert len(gf.seal(KEY, ROOM, gf.Kind.PRESENCE, b"x" * 253)) == overhead + 512
    # snapshots pad to 4096
    snaps = {len(gf.seal(KEY, ROOM, gf.Kind.SNAPSHOT, b"x" * n)) for n in (0, 100, 4000, 4092)}
    assert snaps == {overhead + 4096}
    assert len(gf.seal(KEY, ROOM, gf.Kind.SNAPSHOT, b"x" * 4093)) == overhead + 8192
    assert gf.block_for(gf.Kind.SNAPSHOT) == 4096 and gf.block_for(gf.Kind.COMMENT) == 256


def test_empty_plaintext_and_large_frame():
    assert gf.unseal(KEY, ROOM, gf.seal(KEY, ROOM, gf.Kind.META, b"")) == (gf.Kind.META, b"")
    big = b"\xde" + b"z" * (2 * 1024 * 1024)
    assert gf.unseal_notes(KEY, ROOM, gf.seal_notes(KEY, ROOM, "g", "n", big))[2] == big


# ---- notes plaintext ----


def test_notes_plaintext_layout():
    plain = gf.notes_plaintext("ab", "Zoë", b"\xaf\x01")
    assert plain == b"\x02ab" + bytes([4]) + "Zoë".encode() + b"\xaf\x01"
    assert gf.parse_notes(plain) == ("ab", "Zoë", b"\xaf\x01")


def test_notes_plaintext_empty_gid_and_name():
    plain = gf.notes_plaintext("", "", DELTA)
    assert plain == b"\x00\x00" + DELTA
    assert gf.parse_notes(plain) == ("", "", DELTA)


def test_notes_name_truncated_at_character_boundary():
    name = "é" * 200  # 400 bytes
    _, got, _ = gf.parse_notes(gf.notes_plaintext("g", name, DELTA))
    assert got == "é" * 127 and len(got.encode()) == 254
    emoji = "a" + "🎉" * 100  # 1 + 4 * 63 = 253 fits; the 64th would make 257
    _, got, _ = gf.parse_notes(gf.notes_plaintext("g", emoji, DELTA))
    assert got == "a" + "🎉" * 63 and len(got.encode()) == 253
    exact = "x" * 255
    assert gf.parse_notes(gf.notes_plaintext("g", exact, DELTA))[1] == exact
    assert gf.parse_notes(gf.notes_plaintext("g", "x" * 256, DELTA))[1] == exact


def test_notes_gid_too_long_refused():
    assert gf.parse_notes(gf.notes_plaintext("g" * 255, "", DELTA))[0] == "g" * 255
    with pytest.raises(gf.GuestFormatError):
        gf.notes_plaintext("g" * 256, "", DELTA)


@pytest.mark.parametrize("frame", [b"", b"\x00abc", b"\xdd", b"{}", b"\xdf"])
def test_frame_must_start_with_de_or_af(frame):
    with pytest.raises(gf.GuestFormatError):
        gf.notes_plaintext("g", "n", frame)
    with pytest.raises(gf.GuestFormatError):
        gf.parse_notes(b"\x01g\x01n" + frame)
    # and a sealed one that smuggles a bad frame is refused on reading
    env = gf.seal(KEY, ROOM, gf.Kind.NOTES, b"\x01g\x01n" + frame)
    with pytest.raises(gf.GuestFormatError):
        gf.unseal_notes(KEY, ROOM, env)


@pytest.mark.parametrize(
    "plain", [b"", b"\x05ab", b"\x01g", b"\x01g\x05n", b"\x01\xff\x00" + DELTA]
)
def test_malformed_notes_plaintext(plain):
    with pytest.raises(gf.GuestFormatError):
        gf.parse_notes(plain)


# ---- vectors ----


def test_vectors_open(vec):
    key = gf.key_from_fragment(vec["key_b64url"])
    room = vec["room"]
    assert gf.frames_key(key).hex() == vec["frames_key_hex"]
    assert gf.room_token(key) == vec["room_token"]
    assert set(vec["samples"]) == {
        "notes",
        "notes_artifact",
        "notes_owner",
        "snapshot",
        "comment",
        "meta",
        "presence",
    }
    for label, s in vec["samples"].items():
        env = pf.b64url_decode(s["envelope_b64url"])
        assert env[0] == s["kind"] and env[1:13].hex() == s["nonce_hex"]
        assert gf.aad(room, s["kind"]).decode() == s["aad"]
        kind, plain = gf.unseal(key, room, env)
        assert kind == s["kind"] and plain.hex() == s["plaintext_hex"], label
        assert len(pf.pad(plain, gf.block_for(kind))) == s["padded_length"]
        if "frame_hex" in s:
            assert gf.parse_notes(plain) == (s["gid"], s["name"], bytes.fromhex(s["frame_hex"]))
            assert gf.seal_notes(
                key, room, s["gid"], s["name"], bytes.fromhex(s["frame_hex"]),
                bytes.fromhex(s["nonce_hex"]),
            ) == env  # fmt: skip
        else:
            assert plain.decode() == s["json"]
            assert gf.seal(key, room, kind, plain, bytes.fromhex(s["nonce_hex"])) == env
            assert gf.canonical_json(json.loads(s["json"])) == plain


def test_vectors_reproduce(vec):
    # The committed file is exactly what the script writes now.
    spec = importlib.util.spec_from_file_location("make_guest_vectors", SCRIPT)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    assert mod.build() == vec
    assert json.dumps(mod.build(), indent=2, ensure_ascii=False) + "\n" == VECTORS.read_text(
        encoding="utf-8"
    )
