"""Write backend/tests/vectors/publication_v1.json: fixed inputs and the exact bytes the
publication format (services/publication_format.py) must produce from them.

Deterministic: fixed key, nonces and salt (only here; production always draws random nonces).
Both test suites read the file, the Python one (tests/test_publication_format.py) and the
viewer's (viewer/src/format.test.ts), so the two implementations cannot drift apart.

    cd backend && uv run python scripts/make_publication_vectors.py
"""

from __future__ import annotations

import json
from pathlib import Path

from mnemosyne.services import publication_format as pf

OUT = Path(__file__).resolve().parent.parent / "tests" / "vectors" / "publication_v1.json"

ID = "0123456789abcdef"
KEY = bytes(range(32))
MANIFEST_NONCE = bytes(range(0xA0, 0xAC))
CLIP_NONCE = bytes(range(0xB0, 0xBC))
PASSWORD_NONCE = bytes(range(0xC0, 0xCC))
PASSWORD = "pässwörd 🔑 ключ"
SALT = bytes(range(0x10, 0x20))
CLIP_FIRST_VERSION = 1
MANIFEST_VERSION = 2
CLIP_BYTES = b"OggS\x00fake opus bytes for the test vectors \xf0\x9f\x8e\xb5" * 3


def build() -> dict:
    clip_blob, clip_sha = pf.seal_clip(ID, CLIP_FIRST_VERSION, CLIP_BYTES, KEY, nonce=CLIP_NONCE)
    manifest = pf.Manifest(
        id=ID,
        version=MANIFEST_VERSION,
        published_at="2026-10-23T09:30:00Z",
        expires_at="2027-01-21T09:30:00Z",
        title="Roadmap sync 🚀 – Café Zürich",
        date="2026-10-22",
        duration_seconds=1830.5,
        summary="## Summary\n\nWe agreed on the 🎯 launch date — naïve estimates were dropped.\n",
        decisions=[pf.Decision(id="d1", text="Ship on Friday 📦", at=412.25)],
        questions=[pf.Question(id="q1", text="Who owns the 日本語 docs?", at=None)],
        action_items=[
            pf.ActionItem(id="a1", text="Write the release notes", owner="Zoë", done=True),
            pf.ActionItem(id="a2", text="Book the venue", due="2026-11-01"),
        ],
        chapters=[pf.Chapter(title="Intro", start=0), pf.Chapter(title="Plan ✅", start=300.5)],
        attendees=["Zoë Müller", "Corey", "李雷"],
        topics=["launch", "docs"],
        notes="Notes with a table:\n\n| a | b |\n|---|---|\n| 1 | 🙂 |\n",
        quotes=[
            pf.Quote(
                id="u1",
                text="Let's ship it — « vite ! »",
                speaker="Zoë Müller",
                start=412.25,
                end=415.0,
                clip=pf.ClipRef(
                    sha256=clip_sha,
                    size=len(CLIP_BYTES),
                    seconds=2.75,
                    mime="audio/ogg; codecs=opus",
                    v=CLIP_FIRST_VERSION,
                ),
            ),
            pf.Quote(id="u2", text="No clip for this one", start=1.0, end=2.0),
        ],
    )
    plaintext = pf.manifest_bytes(manifest)
    blob, sha = pf.seal_manifest(manifest, KEY, nonce=MANIFEST_NONCE)
    password_key = pf.derive_key(PASSWORD, SALT, pf.PBKDF2_ITERATIONS)
    password_blob, password_sha = pf.seal_manifest(manifest, password_key, nonce=PASSWORD_NONCE)
    return {
        "format": pf.FORMAT,
        "id": ID,
        "key_b64url": pf.encode_key(KEY),
        "manifest": {
            "version": MANIFEST_VERSION,
            "aad": pf.aad(ID, "manifest", MANIFEST_VERSION).decode(),
            "nonce_hex": MANIFEST_NONCE.hex(),
            "json": plaintext.decode(),
            "json_hex": plaintext.hex(),
            "padded_length": len(pf.pad(plaintext, pf.MANIFEST_BLOCK)),
            "blob_b64url": pf.b64url_encode(blob),
            "sha256": sha,
        },
        "clip": {
            "first_version": CLIP_FIRST_VERSION,
            "aad": pf.aad(ID, "clip", CLIP_FIRST_VERSION).decode(),
            "nonce_hex": CLIP_NONCE.hex(),
            "plaintext_hex": CLIP_BYTES.hex(),
            "padded_length": len(pf.pad(CLIP_BYTES, pf.CLIP_BLOCK)),
            "blob_b64url": pf.b64url_encode(clip_blob),
            "sha256": clip_sha,
        },
        "password": {
            "password": PASSWORD,
            "salt_hex": SALT.hex(),
            "iterations": pf.PBKDF2_ITERATIONS,
            "key_b64url": pf.encode_key(password_key),
            "nonce_hex": PASSWORD_NONCE.hex(),
            "manifest_blob_b64url": pf.b64url_encode(password_blob),
            "manifest_sha256": password_sha,
        },
    }


def main() -> None:
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(build(), indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"wrote {OUT}")


if __name__ == "__main__":
    main()
