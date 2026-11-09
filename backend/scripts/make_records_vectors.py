"""Write backend/tests/vectors/records_v2.json: a fixed meeting and the exact digests the record
format (storage/records.py) must produce from it.

The payload definitions are what every existing seal depends on: if one changes, old meetings
stop verifying. tests/test_records_manifest.py recomputes everything from the file's inputs and
fails when the code and the file disagree, so regenerate it ONLY on purpose (a payload change
needs a new `PART_VERSION` and a plan for old seals):

    cd backend && uv run python scripts/make_records_vectors.py
"""

from __future__ import annotations

import json
from pathlib import Path

from mnemosyne.models.session import Session
from mnemosyne.storage import records as r

OUT = Path(__file__).resolve().parent.parent / "tests" / "vectors" / "records_v2.json"

SESSION = {
    "id": "0123456789ab",
    "name": "Roadmap sync 🚀 – Café Zürich",
    "participants": ["Zoë Müller", "Corey"],
    "attendees": ["李雷", "Corey"],
    "meeting_type": "planning",
    "owner_id": "u-1",
    "agenda": [{"text": "Launch date", "covered": True}, {"text": "Budget", "covered": False}],
    "transcript": [
        {
            "speaker": "Zoë Müller",
            "start": 0.0,
            "end": 4.25,
            "text": "Let's fix the launch date. ✅",
            "words": [{"word": "Let's", "start": 0.0, "end": 0.3}],
        },
        {"speaker": "Corey", "start": 4.5, "end": 9, "text": "Friday works — 1e-07 is not a plan."},
    ],
    "summary": "## Summary\n\nShip on Friday 📦\n",
    "summary_data": {
        "title": "Roadmap sync",
        "style": "meeting",
        "decisions": ["Ship on Friday"],
        "decision_ids": ["d1"],
        "decision_at": [4.5],
        "action_items": [
            {"id": "a1", "text": "Book the venue", "owner": "Zoë", "due": "2026-11-20", "at": 7.5}
        ],
        "edited_at": "2026-11-09T10:30:00",
        "edited_by": "Corey",
    },
    "notes": "# Notes\n\n- [ ] [Zoë](mnemosyne:person/Zo%C3%AB) book it\n"
    "![diagram](mnemosyne:image/" + "ab" * 32 + ".webp@640x480)\n",
    "external_notes": [
        {
            "id": "z9",
            "source": "Zoom",
            "text": "Zoom AI says hi",
            "added_at": "2026-11-09T09:00:00",
        },
        {"id": "g1", "source": "Gemini", "text": "Géminï notes", "added_at": "2026-11-09T09:05:00"},
    ],
}

COMMENTS = [
    {
        "id": "c2",
        "session_id": "0123456789ab",
        "parent_id": "c1",
        "target": "segment",
        "text": "Reply 👍",
        "author_id": "u-2",
        "author": "Zoë",
        "mentions": ["u-1"],
        "created_at": "2026-11-09T11:05:00",
        "updated_at": "2026-11-09T11:05:00",
    },
    {
        "id": "c1",
        "session_id": "0123456789ab",
        "target": "segment",
        "anchor": {"part": 0, "start": 4.5, "end": 9.0},
        "quote": "Friday works",
        "text": "Is Friday realistic?",
        "author_id": "u-1",
        "author": "Corey",
        "created_at": "2026-11-09T11:00:00+00:00",
        "updated_at": "2026-11-09T11:00:00+00:00",
        "resolved_at": "2026-11-09T12:00:00+00:00",
        "resolved_by": "u-1",
        "approved_at": "2026-11-09T11:30:00",
    },
]

SHA_A = "a" * 64
SHA_B = "b" * 64
SHA_X = "c" * 64
SHA_X2 = "d" * 64


def build() -> dict:
    session = Session.model_validate(SESSION)
    payloads = r.part_payloads(session, COMMENTS)
    digests = r.part_digests(payloads)
    content_v1 = r.content_hash(r.content_of(session))

    manifest1 = r.build_manifest(
        session_id=session.id,
        rev=1234,
        content_v1=content_v1,
        parts=digests,
        audio={
            "mixed.ogg": r.audio_entry(SHA_A, 4004),
            "mic.ogg": r.audio_entry(SHA_B, 2002),
        },
    )
    hash1 = r.manifest_hash(manifest1)
    v1_chain = r.chain_hash("", content_v1, {"mixed.ogg.enc": "e" * 64}, "2026-11-08T09:00:00", "x")
    at1, reason1 = "2026-11-09T12:30:00", "changed: comments"
    chain1 = r.chain_hash_v2(v1_chain, hash1, at1, reason1)

    # An erasure seal: mic.ogg was deleted and no longer matched the last seal's digest.
    audio2, erased2 = r.erase_audio(
        manifest1["audio"],
        {},
        {"mic.ogg": r.audio_entry(SHA_X, 2002)},
        at="2026-12-01T08:00:00",
        by="admin",
        reason="older than 365 days",
        deletion=17,
    )
    manifest2 = r.build_manifest(
        session_id=session.id,
        rev=1240,
        content_v1=content_v1,
        parts=digests,
        audio=audio2,
        erased=erased2,
    )
    hash2 = r.manifest_hash(manifest2)
    at2, reason2 = "2026-12-01T08:00:01", "audio deleted"
    chain2 = r.chain_hash_v2(chain1, hash2, at2, reason2)

    # A meeting whose audio is on another computer.
    manifest_origin = r.build_manifest(
        session_id=session.id,
        rev=3,
        content_v1=content_v1,
        parts=digests,
        audio={"ignored.ogg": r.audio_entry(SHA_A, 1)},
        audio_here=False,
    )
    return {
        "session": SESSION,
        "comments": COMMENTS,
        "content_v1": content_v1,
        "payloads_json": {name: json.dumps(p, ensure_ascii=False) for name, p in payloads.items()},
        "payloads_canonical": {
            name: r.canonical({"part": name, "v": r.PART_VERSION, "data": p}).decode()
            for name, p in payloads.items()
        },
        "part_digests": digests,
        "manifest_v1": {
            "manifest": manifest1,
            "canonical": r.canonical(manifest1).decode(),
            "hash": hash1,
        },
        "chain": {
            "v1_seal": {
                "prev": "",
                "content": content_v1,
                "audio": {"mixed.ogg.enc": "e" * 64},
                "at": "2026-11-08T09:00:00",
                "reason": "x",
                "chain": v1_chain,
            },
            "seal_1": {"at": at1, "reason": reason1, "chain": chain1},
            "seal_2": {"at": at2, "reason": reason2, "chain": chain2},
        },
        "manifest_erasure": {
            "manifest": manifest2,
            "canonical": r.canonical(manifest2).decode(),
            "hash": hash2,
        },
        "manifest_origin": {
            "manifest": manifest_origin,
            "canonical": r.canonical(manifest_origin).decode(),
            "hash": r.manifest_hash(manifest_origin),
        },
    }


def main() -> None:
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(build(), indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"wrote {OUT}")


if __name__ == "__main__":
    main()
