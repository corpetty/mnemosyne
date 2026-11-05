"""Write backend/tests/vectors/publication_live_v1.json: fixed inputs and the exact bytes the
live-publication format (services/pub_live_format.py) must produce from them.

Deterministic: fixed link key, room, owner and reader keys, ephemeral key and nonces (only
here; production always draws random ones). Ed25519 signatures are deterministic by
themselves. Read by tests/test_pub_live_format.py and by the viewer's tests
(src/lib/sealed/signed.test.ts, viewer/src/live.test.ts), so the implementations cannot drift
apart. Regenerate only on purpose:

    cd backend && uv run python scripts/make_live_vectors.py
"""

from __future__ import annotations

import json
from pathlib import Path

from mnemosyne.services import pub_live_format as pl
from mnemosyne.services import publication_format as pf

OUT = Path(__file__).resolve().parent.parent / "tests" / "vectors" / "publication_live_v1.json"

KEY = bytes(range(0x40, 0x60))  # the publication's link key
PUB_ID = "0123456789abcdef"
ROOM = "a1b2c3d4e5f60718"
VERSION = 4
OWNER = pl.OwnerKeys(sign=bytes(range(0x01, 0x21)), box=bytes(range(0x21, 0x41)))
READER = bytes(range(0x61, 0x81))  # the reader's Ed25519 seed
EPH = bytes(range(0x81, 0xA1))  # the COMMENT's ephemeral X25519 scalar
NONCES = {
    "manifest": bytes(range(0x70, 0x7C)),
    "version": bytes(range(0x10, 0x1C)),
    "threads": bytes(range(0x20, 0x2C)),
    "meta": bytes(range(0x30, 0x3C)),
    "comment": bytes(range(0x40, 0x4C)),
    "comment_box": bytes(range(0x50, 0x5C)),
    "threads_signed_by_reader": bytes(range(0x60, 0x6C)),
    "version_unsigned": bytes(range(0x90, 0x9C)),
}
PUBLISHED_AT = "2026-11-05T10:15:00Z"
EXPIRES_AT = "2027-02-03T10:15:00Z"


def _manifest() -> pf.Manifest:
    return pf.Manifest(
        id=PUB_ID,
        version=VERSION,
        published_at=PUBLISHED_AT,
        expires_at=EXPIRES_AT,
        title="Roadmap sync 🚀 – Café Zürich",
        date="2026-11-05",
        summary="## Summary\n\nWe agreed on the launch date — naïve estimates were dropped.\n",
        decisions=[pf.Decision(id="d1", text="Ship on Friday 📦", at=412.25)],
        action_items=[pf.ActionItem(id="a1", text="Write the release notes", owner="Zoë")],
        attendees=["Zoë Müller", "Corey"],
        live=OWNER.live(),
    )


def _owner_sample(kind: pl.Kind, body: dict, nonce: bytes, private: bytes) -> dict:
    body_bytes = pf.canonical_json(body)
    message = pl.signed_message(ROOM, kind, body_bytes)
    plain = pl.signed_plaintext(ROOM, kind, body, private)
    env = pl.sign_frame(KEY, ROOM, kind, body, private, nonce)
    return {
        "kind": int(kind),
        "aad": pl.aad(ROOM, kind).decode(),
        "nonce_hex": nonce.hex(),
        "json": body_bytes.decode(),
        "signed_message_hex": message.hex(),
        "sig_hex": plain[: pl.SIG_SIZE].hex(),
        "plaintext_hex": plain.hex(),
        "padded_length": len(pf.pad(plain, 256)),
        "envelope_b64url": pf.b64url_encode(env),
    }


def build() -> dict:
    reader_pub = pl.sign_public(READER)
    rid = pl.rid_of(reader_pub)

    manifest = _manifest()
    mjson = pf.manifest_bytes(manifest)
    mblob, msha = pf.seal_manifest(manifest, KEY, nonce=NONCES["manifest"])

    version = {
        "version": VERSION,
        "sha256": msha,
        "published_at": PUBLISHED_AT,
        "expires_at": EXPIRES_AT,
    }
    reader_comment = {
        "id": "5f3a9c1e7b20",
        "parent_id": None,
        "author": "Ana (reader)",
        "rid": rid,
        "text": "Why Friday — not Thursday? 日本語",
        "anchor": {"target": "decision", "target_id": "d1", "quote_id": None, "quote": None},
        "created_at": "2026-11-05T10:20:00Z",
        "updated_at": "2026-11-05T10:20:00Z",
        "resolved": False,
        "deleted": False,
        "detached": False,
        "changed": False,
    }
    owner_reply = {
        **reader_comment,
        "id": "0c4d8e2f6a13",
        "parent_id": "5f3a9c1e7b20",
        "author": "Corey",
        "rid": None,
        "text": "Thursday is the holiday 🙂",
        "anchor": {"target": "", "target_id": None, "quote_id": None, "quote": None},
        "created_at": "2026-11-05T10:22:30Z",
        "updated_at": "2026-11-05T10:22:30Z",
    }
    threads = {
        "threads": [{"root": reader_comment, "replies": [owner_reply]}],
        "at": "2026-11-05T10:22:31Z",
    }
    meta = {"comments": True, "follow": True, "at": "2026-11-05T10:15:01Z"}

    samples = {
        "version": _owner_sample(pl.Kind.VERSION, version, NONCES["version"], OWNER.sign),
        "threads": _owner_sample(pl.Kind.THREADS, threads, NONCES["threads"], OWNER.sign),
        "meta": _owner_sample(pl.Kind.META, meta, NONCES["meta"], OWNER.sign),
    }

    op = {
        "op": "create",
        "id": "5f3a9c1e7b20",
        "parent_id": None,
        "text": "Why Friday — not Thursday? 日本語",
        "name": "Ana",
        "at": "2026-11-05T10:20:00Z",
        "anchor": {
            "target": "decision",
            "target_id": "d1",
            "quote_id": None,
            "exact": None,
            "prefix": None,
            "suffix": None,
        },
    }
    op_bytes = pf.canonical_json(op)
    reader_message = pl.reader_message(ROOM, reader_pub, op_bytes)
    eph_pub = pl.box_public(EPH)
    shared = pl.x25519(EPH, OWNER.box_public)
    boxed = pl.box_plaintext(
        ROOM, op, READER, OWNER.box_public, eph_private=EPH, box_nonce=NONCES["comment_box"]
    )
    env = pl.box_op(
        KEY,
        ROOM,
        op,
        READER,
        OWNER.box_public,
        eph_private=EPH,
        box_nonce=NONCES["comment_box"],
        nonce=NONCES["comment"],
    )
    reader_sig = pl.ed25519_sign(READER, reader_message)
    samples["comment"] = {
        "kind": int(pl.Kind.COMMENT),
        "aad": pl.aad(ROOM, pl.Kind.COMMENT).decode(),
        "nonce_hex": NONCES["comment"].hex(),
        "json": op_bytes.decode(),
        "reader_message_hex": reader_message.hex(),
        "reader_sig_hex": reader_sig.hex(),
        "content_hex": (reader_sig + reader_pub + op_bytes).hex(),
        "eph_private_hex": EPH.hex(),
        "eph_public_hex": eph_pub.hex(),
        "shared_hex": shared.hex(),
        "box_key_hex": pl.box_key(shared, ROOM).hex(),
        "box_aad": pl.box_aad(ROOM, eph_pub).decode(),
        "box_nonce_hex": NONCES["comment_box"].hex(),
        "plaintext_hex": boxed.hex(),
        "padded_length": len(pf.pad(boxed, 256)),
        "envelope_b64url": pf.b64url_encode(env),
        "rid": rid,
    }

    # Frames a reader could make (they hold the frames key): both must be refused.
    threads_bytes = pf.canonical_json(threads)
    forged = pl.seal(
        KEY,
        ROOM,
        pl.Kind.THREADS,
        pl.ed25519_sign(READER, pl.signed_message(ROOM, pl.Kind.THREADS, threads_bytes))
        + threads_bytes,
        NONCES["threads_signed_by_reader"],
    )
    unsigned = pl.seal(
        KEY, ROOM, pl.Kind.VERSION, pf.canonical_json(version), NONCES["version_unsigned"]
    )
    refused = {
        "threads_signed_by_reader": pf.b64url_encode(forged),
        "version_unsigned": pf.b64url_encode(unsigned),
    }

    return {
        "format": pl.FORMAT,
        "key_b64url": pf.encode_key(KEY),
        "pub_id": PUB_ID,
        "room": ROOM,
        "frames_key_hex": pl.frames_key(KEY).hex(),
        "room_token": pl.room_token(KEY),
        "owner": {
            "sign_private_hex": OWNER.sign.hex(),
            "sign_public_hex": OWNER.sign_public.hex(),
            "box_private_hex": OWNER.box.hex(),
            "box_public_hex": OWNER.box_public.hex(),
        },
        "reader": {
            "private_hex": READER.hex(),
            "public_hex": reader_pub.hex(),
            "rid": rid,
        },
        "manifest": {
            "version": VERSION,
            "json": mjson.decode(),
            "aad": pf.aad(PUB_ID, "manifest", VERSION).decode(),
            "nonce_hex": NONCES["manifest"].hex(),
            "blob_b64url": pf.b64url_encode(mblob),
            "sha256": msha,
        },
        "samples": samples,
        "refused": refused,
    }


def main() -> None:
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(build(), indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"wrote {OUT}")


if __name__ == "__main__":
    main()
