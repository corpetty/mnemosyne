"""Write backend/tests/vectors/publication_v1.json: fixed inputs and the exact bytes the
publication format (services/publication_format.py) must produce from them.

Deterministic: fixed key, nonces and salt (only here; production always draws random nonces).
Both test suites read the file, the Python one (tests/test_publication_format.py) and the
viewer's (viewer/src/format.test.ts), so the two implementations cannot drift apart.

    cd backend && uv run python scripts/make_publication_vectors.py
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from mnemosyne.services import publication_format as pf

OUT = Path(__file__).resolve().parent.parent / "tests" / "vectors" / "publication_v1.json"

ID = "0123456789abcdef"
KEY = bytes(range(32))
MANIFEST_NONCE = bytes(range(0xA0, 0xAC))
CLIP_NONCE = bytes(range(0xB0, 0xBC))
PASSWORD_NONCE = bytes(range(0xC0, 0xCC))
IMAGE_NONCE = bytes(range(0xD0, 0xDC))
IMAGES_MANIFEST_NONCE = bytes(range(0xE0, 0xEC))
PASSWORD = "pässwörd 🔑 ключ"
SALT = bytes(range(0x10, 0x20))
CLIP_FIRST_VERSION = 1
MANIFEST_VERSION = 2
IMAGE_FIRST_VERSION = 1
IMAGES_MANIFEST_VERSION = 3
# Not a decodable image: the format only seals bytes and checks their SHA-256 (`ImageRef.id`).
IMAGE_BYTES = (
    b"RIFF\x24\x00\x00\x00WEBPVP8 fake webp bytes for the test vectors \xf0\x9f\x96\xbc" * 2
)
IMAGE_WIDTH, IMAGE_HEIGHT = 640, 480
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
    image_id = hashlib.sha256(IMAGE_BYTES).hexdigest()
    image_blob, image_sha = pf.seal_image(
        ID, IMAGE_FIRST_VERSION, IMAGE_BYTES, KEY, nonce=IMAGE_NONCE
    )
    # A later version of the meeting: the notes show the image (kept) and another (left out), the
    # image is reused from version 1, and there are no quotes or clips.
    url = f"mnemosyne:image/{image_id}.webp@{IMAGE_WIDTH}x{IMAGE_HEIGHT}"
    kept_line = f"![Whiteboard 🧠 \\[draft\\]]({url})"
    images_manifest = pf.Manifest(
        id=ID,
        version=IMAGES_MANIFEST_VERSION,
        published_at="2026-10-24T09:30:00Z",
        title="Roadmap sync — with a picture",
        date="2026-10-22",
        notes=f"Before the picture\n{kept_line}\n[image: Left out]\nAfter it\n",
        images=[
            pf.ImageRef(
                id=image_id,
                sha256=image_sha,
                size=len(IMAGE_BYTES),
                mime="image/webp",
                v=IMAGE_FIRST_VERSION,
                width=IMAGE_WIDTH,
                height=IMAGE_HEIGHT,
            )
        ],
    )
    images_plaintext = pf.manifest_bytes(images_manifest)
    images_blob, images_sha = pf.seal_manifest(images_manifest, KEY, nonce=IMAGES_MANIFEST_NONCE)
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
        "image": {
            "first_version": IMAGE_FIRST_VERSION,
            "aad": pf.aad(ID, "image", IMAGE_FIRST_VERSION).decode(),
            "nonce_hex": IMAGE_NONCE.hex(),
            "plaintext_hex": IMAGE_BYTES.hex(),
            "plaintext_sha256": image_id,  # the ImageRef `id`
            "mime": "image/webp",
            "width": IMAGE_WIDTH,
            "height": IMAGE_HEIGHT,
            "padded_length": len(pf.pad(IMAGE_BYTES, pf.IMAGE_BLOCK)),
            "blob_b64url": pf.b64url_encode(image_blob),
            "sha256": image_sha,  # the blob's address (`ImageRef.sha256`)
        },
        # A manifest with `images`: its notes hold one kept image line (the ref's `id` is the
        # hash in its URL) and one left-out `[image: ...]`; the ref reuses the `image` blob above.
        "images_manifest": {
            "version": IMAGES_MANIFEST_VERSION,
            "aad": pf.aad(ID, "manifest", IMAGES_MANIFEST_VERSION).decode(),
            "nonce_hex": IMAGES_MANIFEST_NONCE.hex(),
            "json": images_plaintext.decode(),
            "json_hex": images_plaintext.hex(),
            "padded_length": len(pf.pad(images_plaintext, pf.MANIFEST_BLOCK)),
            "blob_b64url": pf.b64url_encode(images_blob),
            "sha256": images_sha,
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
