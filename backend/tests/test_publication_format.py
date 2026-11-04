"""The publication bundle format (services/publication_format.py) and its test vectors."""

import hashlib
import importlib.util
import json
import re
from pathlib import Path

import pytest

from mnemosyne.services import publication_format as pf

VECTORS = Path(__file__).parent / "vectors" / "publication_v1.json"
SCRIPT = Path(__file__).parent.parent / "scripts" / "make_publication_vectors.py"
ID = "0123456789abcdef"


@pytest.fixture(scope="module")
def vec():
    return json.loads(VECTORS.read_text(encoding="utf-8"))


def manifest(**kw):
    base = {
        "id": ID,
        "version": 1,
        "published_at": "2026-10-23T09:30:00Z",
        "title": "T",
        "date": "2026-10-23",
    }
    return pf.Manifest(**{**base, **kw})


# ---- ids and keys ----


def test_ids_keys_and_tokens():
    assert re.fullmatch(r"[0-9a-f]{16}", pf.new_id())
    assert pf.new_id() != pf.new_id()
    key = pf.new_key()
    text = pf.encode_key(key)
    assert len(text) == 43 and "=" not in text
    assert pf.decode_key(text) == key
    assert len(pf.b64url_decode(pf.new_token())) == 32


@pytest.mark.parametrize("bad", ["", "short", "A" * 44, "A" * 42, "!" * 43, "A" * 42 + "B"])
def test_bad_keys_are_refused(bad):
    # "A"*42+"B" has non-zero trailing bits: a second spelling of a value.
    with pytest.raises(pf.PublicationFormatError):
        pf.decode_key(bad)


def test_check_id():
    assert pf.check_id(ID) == ID
    for bad in ["", "0123456789ABCDEF", "0123456789abcde", "0123456789abcdeg", ID + "0"]:
        with pytest.raises(pf.PublicationFormatError):
            pf.check_id(bad)


# ---- padding ----


@pytest.mark.parametrize("block", [pf.MANIFEST_BLOCK, pf.CLIP_BLOCK])
@pytest.mark.parametrize("n", [0, 1, 4091, 4092, 4093, 65532, 65533, 70000])
def test_padding_is_exact(block, n):
    data = bytes([7]) * n
    padded = pf.pad(data, block)
    assert len(padded) % block == 0 and len(padded) == pf.padded_size(n, block)
    assert padded[:4] == n.to_bytes(4, "big")
    assert len(padded) - (4 + n) < block
    assert not any(padded[4 + n :])
    assert pf.unpad(padded, block) == data


def test_unpad_refuses_bad_padding():
    good = pf.pad(b"abc", 4096)
    for bad in [
        good[:-1],  # not a multiple of the block
        good + b"\0" * 4096,  # padding longer than needed
        good[:100] + b"\1" + good[101:],  # junk in the padding
        (5000).to_bytes(4, "big") + good[4:],  # length beyond the data
        b"",
    ]:
        with pytest.raises(pf.PublicationFormatError):
            pf.unpad(bad, 4096)


# ---- canonical JSON and the manifest ----


def test_canonical_json_is_stable():
    a = pf.canonical_json({"b": 1, "a": [1, {"z": "é🙂", "y": None}]})
    b = pf.canonical_json({"a": [1, {"y": None, "z": "é🙂"}], "b": 1})
    assert a == b == '{"a":[1,{"y":null,"z":"é🙂"}],"b":1}'.encode()
    assert pf.manifest_bytes(manifest()) == pf.manifest_bytes(manifest())


def test_manifest_has_no_transcript_and_refuses_extras():
    assert not {"segments", "transcript", "words"} & set(pf.Manifest.model_fields)
    parsed = json.loads(pf.manifest_bytes(manifest()))
    assert "segments" not in parsed and "transcript" not in parsed
    with pytest.raises(pf.PublicationFormatError):
        pf.parse_manifest(pf.canonical_json({**parsed, "segments": []}))
    with pytest.raises(pf.PublicationFormatError):
        pf.parse_manifest(pf.canonical_json({**parsed, "format": 2}))
    with pytest.raises(pf.PublicationFormatError):
        pf.parse_manifest(b"\xff not json")


# ---- seal / open ----


def test_round_trips():
    key = pf.new_key()
    m = manifest(title="Café 🙂", summary="x" * 10000)
    blob, sha = pf.seal_manifest(m, key)
    assert sha == pf.blob_hash(blob)
    assert len(blob) == pf.NONCE_SIZE + pf.padded_size(len(pf.manifest_bytes(m)), 4096) + 16
    assert pf.open_manifest(ID, 1, blob, key) == m
    ogg = b"OggS" + bytes(range(256)) * 10
    cblob, csha = pf.seal_clip(ID, 3, ogg, key)
    assert (len(cblob) - 28) % pf.CLIP_BLOCK == 0
    clip = pf.ClipRef(sha256=csha, size=len(ogg), seconds=1.0, mime="audio/ogg", v=3)
    assert pf.open_clip(ID, clip, cblob, key) == ogg


def test_nonces_are_random():
    key = pf.new_key()
    a, _ = pf.seal("manifest", ID, 1, b"same", key)
    b, _ = pf.seal("manifest", ID, 1, b"same", key)
    assert a[:12] != b[:12] and a != b


def test_nonce_parameter_is_checked():
    with pytest.raises(pf.PublicationFormatError):
        pf.seal("manifest", ID, 1, b"x", pf.new_key(), nonce=b"short")


def test_wrong_key_aad_or_tamper_fail():
    key = pf.new_key()
    blob, _ = pf.seal("manifest", ID, 2, b"{}", key)
    assert pf.open_blob("manifest", ID, 2, blob, key) == b"{}"
    for args in [
        ("manifest", ID, 3, blob, key),  # other version
        ("clip", ID, 2, blob, key),  # other kind
        ("manifest", "fedcba9876543210", 2, blob, key),  # other publication
        ("manifest", ID, 2, blob, pf.new_key()),  # other key
    ]:
        with pytest.raises(pf.PublicationFormatError, match="Could not decrypt"):
            pf.open_blob(*args)
    flipped = bytearray(blob)
    flipped[20] ^= 1
    with pytest.raises(pf.PublicationFormatError, match="Could not decrypt"):
        pf.open_blob("manifest", ID, 2, bytes(flipped), key)
    with pytest.raises(pf.PublicationFormatError, match="too short"):
        pf.open_blob("manifest", ID, 2, blob[:20], key)
    with pytest.raises(pf.PublicationFormatError):
        pf.open_blob("manifest", ID, 2, blob, key[:16])
    with pytest.raises(pf.PublicationFormatError, match="kind"):
        pf.aad(ID, "transcript", 1)  # type: ignore[arg-type]


def test_manifest_must_match_its_blob_slot():
    key = pf.new_key()
    other = manifest(version=7)
    # Sealed under version 1's AAD but claiming version 7 inside.
    blob, _ = pf.seal("manifest", ID, 1, pf.manifest_bytes(other), key)
    with pytest.raises(pf.PublicationFormatError, match="another publication or version"):
        pf.open_manifest(ID, 1, blob, key)


def test_clip_address_is_checked():
    key = pf.new_key()
    blob, sha = pf.seal_clip(ID, 1, b"abc", key)
    ok = pf.ClipRef(sha256=sha, size=3, seconds=1, mime="audio/ogg", v=1)
    assert pf.open_clip(ID, ok, blob, key) == b"abc"
    with pytest.raises(pf.PublicationFormatError, match="address"):
        pf.open_clip(ID, ok.model_copy(update={"sha256": "0" * 64}), blob, key)
    with pytest.raises(pf.PublicationFormatError, match="Could not decrypt"):
        pf.open_clip(ID, ok.model_copy(update={"v": 2}), blob, key)


# ---- caps ----


def test_manifest_cap():
    key = pf.new_key()
    fits = b"x" * (pf.MAX_MANIFEST - 4)
    blob, _ = pf.seal("manifest", ID, 1, fits, key)
    assert pf.open_blob("manifest", ID, 1, blob, key) == fits
    with pytest.raises(pf.PublicationFormatError, match="at most 1 MiB"):
        pf.seal("manifest", ID, 1, fits + b"x", key)


def test_clip_cap():
    key = pf.new_key()
    fits = b"\1" * (pf.MAX_CLIP - 4)
    blob, _ = pf.seal("clip", ID, 1, fits, key)
    assert len(blob) == 12 + pf.MAX_CLIP + 16
    with pytest.raises(pf.PublicationFormatError, match="at most 20 MiB"):
        pf.seal("clip", ID, 1, fits + b"x", key)


def test_clips_total_cap():
    pf.check_clips_total([pf.MAX_CLIPS_TOTAL // 2, pf.MAX_CLIPS_TOTAL // 2])
    with pytest.raises(pf.PublicationFormatError, match="total at most 20 MiB"):
        pf.check_clips_total([pf.MAX_CLIPS_TOTAL // 2, pf.MAX_CLIPS_TOTAL // 2, pf.CLIP_BLOCK])


def test_image_cap_and_blocks():
    key = pf.new_key()
    fits = b"\2" * (pf.MAX_CLIP - 4)
    blob, _ = pf.seal("image", ID, 1, fits, key)
    assert len(blob) == 12 + pf.MAX_CLIP + 16
    with pytest.raises(pf.PublicationFormatError, match="An image may be at most 20 MiB"):
        pf.seal("image", ID, 1, fits + b"x", key)
    small, _ = pf.seal_image(ID, 1, b"RIFFxxxxWEBP", key)
    assert (len(small) - 28) % pf.IMAGE_BLOCK == 0 and pf.IMAGE_BLOCK == 65536


def test_images_and_clips_share_one_total():
    half = pf.MAX_CLIPS_TOTAL // 2
    pf.check_clips_total([half, half])  # e.g. one clip and one image
    with pytest.raises(pf.PublicationFormatError, match="clips and images may total at most"):
        pf.check_clips_total([half, half, pf.IMAGE_BLOCK])


def test_an_image_round_trips_and_is_bound_to_its_slot():
    key = pf.new_key()
    data = b"RIFF\x10\x00\x00\x00WEBP" + bytes(range(200))
    blob, sha = pf.seal_image(ID, 2, data, key)
    ref = pf.ImageRef(
        id=hashlib.sha256(data).hexdigest(),
        sha256=sha,
        size=len(data),
        mime="image/webp",
        v=2,
        width=3,
        height=4,
    )
    assert pf.open_image(ID, ref, blob, key) == data
    assert pf.aad(ID, "image", 2) == f"mnemosyne/pub/1|{ID}|image|2".encode()
    with pytest.raises(pf.PublicationFormatError):
        pf.open_image(ID, ref.model_copy(update={"v": 3}), blob, key)  # another version's AAD
    with pytest.raises(pf.PublicationFormatError):
        pf.open_blob("clip", ID, 2, blob, key)  # a clip slot does not open an image
    with pytest.raises(pf.PublicationFormatError, match="address"):
        pf.open_image(ID, ref.model_copy(update={"sha256": "0" * 64}), blob, key)
    with pytest.raises(pf.PublicationFormatError, match="hash"):
        pf.open_image(ID, ref.model_copy(update={"id": "1" * 64}), blob, key)


def test_the_manifest_lists_images_only_when_there_are_some():
    # Additive: without images the canonical JSON has no `images`, as before this field.
    assert b"images" not in pf.manifest_bytes(manifest())
    ref = {
        "id": "a" * 64,
        "sha256": "b" * 64,
        "size": 5,
        "mime": "image/jpeg",
        "v": 1,
        "width": 2,
        "height": 3,
    }
    m = manifest(images=[ref])
    parsed = json.loads(pf.manifest_bytes(m))
    assert parsed["images"] == [ref]
    assert pf.parse_manifest(pf.manifest_bytes(m)) == m
    # An old manifest (no `images`) parses; an unknown field in an image ref is still refused.
    assert pf.parse_manifest(pf.manifest_bytes(manifest())).images == []
    with pytest.raises(pf.PublicationFormatError):
        pf.parse_manifest(pf.canonical_json({**parsed, "images": [{**ref, "extra": 1}]}))


# ---- password ----


def test_password_key():
    salt = pf.new_salt()
    assert len(salt) == 16
    a = pf.derive_key("pässwörd", salt)
    assert len(a) == 32 and a == pf.derive_key("pässwörd", salt)
    assert a != pf.derive_key("other", salt)
    assert a != pf.derive_key("pässwörd", pf.new_salt())
    with pytest.raises(pf.PublicationFormatError):
        pf.derive_key("x", salt, 1000)
    with pytest.raises(pf.PublicationFormatError):
        pf.derive_key("x", b"short")


# ---- vectors ----


def test_vectors_open(vec):
    key = pf.decode_key(vec["key_b64url"])
    m = vec["manifest"]
    blob = pf.b64url_decode(m["blob_b64url"])
    assert pf.blob_hash(blob) == m["sha256"]
    assert pf.aad(vec["id"], "manifest", m["version"]).decode() == m["aad"]
    plain = pf.open_blob("manifest", vec["id"], m["version"], blob, key)
    assert plain.decode() == m["json"] and plain.hex() == m["json_hex"]
    parsed = pf.open_manifest(vec["id"], m["version"], blob, key)
    assert pf.manifest_bytes(parsed) == plain  # canonical form is stable
    assert "🚀" in parsed.title and "Zürich" in parsed.title

    c = vec["clip"]
    cblob = pf.b64url_decode(c["blob_b64url"])
    ref = parsed.quotes[0].clip
    assert ref is not None and ref.sha256 == c["sha256"] and ref.v == c["first_version"]
    assert ref.v < parsed.version  # reused from an earlier version
    assert pf.open_clip(vec["id"], ref, cblob, key).hex() == c["plaintext_hex"]

    # The image sample: one blob (version 1) that a later manifest (version 3) lists and reuses.
    img = vec["image"]
    iblob = pf.b64url_decode(img["blob_b64url"])
    assert pf.blob_hash(iblob) == img["sha256"]
    assert pf.aad(vec["id"], "image", img["first_version"]).decode() == img["aad"]
    idata = pf.open_blob("image", vec["id"], img["first_version"], iblob, key)
    assert idata.hex() == img["plaintext_hex"]
    assert hashlib.sha256(idata).hexdigest() == img["plaintext_sha256"]
    assert len(pf.pad(idata, pf.IMAGE_BLOCK)) == img["padded_length"]
    im = vec["images_manifest"]
    mblob = pf.b64url_decode(im["blob_b64url"])
    assert pf.blob_hash(mblob) == im["sha256"]
    iplain = pf.open_blob("manifest", vec["id"], im["version"], mblob, key)
    assert iplain.decode() == im["json"] and iplain.hex() == im["json_hex"]
    imanifest = pf.open_manifest(vec["id"], im["version"], mblob, key)
    assert pf.manifest_bytes(imanifest) == iplain
    (ref,) = imanifest.images
    assert (ref.id, ref.sha256, ref.v) == (
        img["plaintext_sha256"],
        img["sha256"],
        img["first_version"],
    )
    assert (ref.mime, ref.width, ref.height) == (img["mime"], img["width"], img["height"])
    assert ref.v < imanifest.version  # reused from an earlier version
    assert pf.open_image(vec["id"], ref, iblob, key) == idata
    assert f"mnemosyne:image/{ref.id}.webp@{ref.width}x{ref.height}" in imanifest.notes
    assert "[image: Left out]" in imanifest.notes

    p = vec["password"]
    pkey = pf.derive_key(p["password"], bytes.fromhex(p["salt_hex"]), p["iterations"])
    assert pf.encode_key(pkey) == p["key_b64url"]
    pblob = pf.b64url_decode(p["manifest_blob_b64url"])
    assert pf.open_manifest(vec["id"], m["version"], pblob, pkey) == parsed


def test_vectors_reproduce(vec):
    # The committed file is exactly what the script writes now.
    spec = importlib.util.spec_from_file_location("make_publication_vectors", SCRIPT)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    assert mod.build() == vec
