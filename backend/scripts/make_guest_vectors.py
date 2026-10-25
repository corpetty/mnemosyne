"""Write backend/tests/vectors/guest_v1.json: fixed inputs and the exact bytes the guest-notes
format (services/guest_format.py) must produce from them.

Deterministic: fixed key, room and nonces (only here; production always draws random nonces).
Read by tests/test_guest_format.py and by the guest page's and the relay's tests, so the
implementations cannot drift apart.

    cd backend && uv run python scripts/make_guest_vectors.py
"""

from __future__ import annotations

import json
from pathlib import Path

from mnemosyne.services import guest_format as gf
from mnemosyne.services import publication_format as pf

OUT = Path(__file__).resolve().parent.parent / "tests" / "vectors" / "guest_v1.json"

KEY = bytes(range(0x80, 0xA0))
ROOM = "fedcba9876543210"
GID = "g-3f9a1c2e"
NAME = "Zoë Müller 🎉"
DELTA = bytes([0xDE]) + b"\x01\x02delta frame bytes for the test vectors \xf0\x9f\x93\x9d"
ARTIFACT = bytes([0xAF]) + b"artifact frame: https://example.org/a-long-link-over-14-bytes"

JSON_SAMPLES: dict[str, tuple[gf.Kind, dict]] = {
    "snapshot": (
        gf.Kind.SNAPSHOT,
        {
            "format": 1,
            "notes": pf.b64url_encode(b"\xde\x00snapshot bytes"),
            "frames": [pf.b64url_encode(DELTA)],
            "threads": [{"id": "c1", "text": "Check this 🙂", "replies": []}],
            "meta": {"title": "Roadmap sync", "ended": False},
        },
    ),
    "comment": (
        gf.Kind.COMMENT,
        {
            "op": "create",
            "id": "0a1b2c3d4e5f6071",
            "parent_id": None,
            "text": "Why Friday — not Thursday? 日本語",
            "quote": "Ship on Friday",
            "gid": GID,
            "name": NAME,
            "notes_ids": ["12:3", "12:17"],
        },
    ),
    "meta": (
        gf.Kind.META,
        {
            "title": "Roadmap sync 🚀 – Café Zürich",
            "date": "2026-10-24",
            "owner": "Corey",
            "agenda": [
                {"text": "Launch date", "covered": True},
                {"text": "Docs owner", "covered": False},
            ],
            "ended": True,
            "ended_at": "2026-10-24T11:00:00Z",
        },
    ),
    "presence": (gf.Kind.PRESENCE, {"gid": GID, "name": NAME, "typing": True}),
}
NONCES = {
    "notes": bytes(range(0x10, 0x1C)),
    "notes_artifact": bytes(range(0x20, 0x2C)),
    "notes_owner": bytes(range(0x30, 0x3C)),
    "snapshot": bytes(range(0x40, 0x4C)),
    "comment": bytes(range(0x50, 0x5C)),
    "meta": bytes(range(0x60, 0x6C)),
    "presence": bytes(range(0x70, 0x7C)),
}


def _entry(kind: gf.Kind, plaintext: bytes, nonce: bytes, envelope: bytes, **extra) -> dict:
    return {
        "kind": int(kind),
        "aad": gf.aad(ROOM, kind).decode(),
        "nonce_hex": nonce.hex(),
        **extra,
        "plaintext_hex": plaintext.hex(),
        "padded_length": len(pf.pad(plaintext, gf.block_for(kind))),
        "envelope_b64url": pf.b64url_encode(envelope),
    }


def build() -> dict:
    samples: dict[str, dict] = {}
    for label, gid, name, frame in [
        ("notes", GID, NAME, DELTA),
        ("notes_artifact", GID, NAME, ARTIFACT),
        ("notes_owner", "", "", DELTA),
    ]:
        plain = gf.notes_plaintext(gid, name, frame)
        env = gf.seal_notes(KEY, ROOM, gid, name, frame, NONCES[label])
        samples[label] = _entry(
            gf.Kind.NOTES,
            plain,
            NONCES[label],
            env,
            gid=gid,
            name=name,
            frame_hex=frame.hex(),
        )
    for label, (kind, obj) in JSON_SAMPLES.items():
        plain = gf.canonical_json(obj)
        env = gf.seal_json(KEY, ROOM, kind, obj, NONCES[label])
        samples[label] = _entry(kind, plain, NONCES[label], env, json=plain.decode())
    return {
        "format": gf.FORMAT,
        "key_b64url": gf.key_to_fragment(KEY),
        "room": ROOM,
        "frames_key_hex": gf.frames_key(KEY).hex(),
        "room_token": gf.room_token(KEY),
        "samples": samples,
    }


def main() -> None:
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(build(), indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"wrote {OUT}")


if __name__ == "__main__":
    main()
