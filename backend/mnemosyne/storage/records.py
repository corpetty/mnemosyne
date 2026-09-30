"""What a meeting's record is made of, for versions and seals (services/records.py): its
transcript lines and summary, hashed the same way wherever they are checked."""

from __future__ import annotations

import hashlib
import json


def content_of(session) -> dict:
    """The parts of a meeting that make its record: who said what when, and the summary."""
    return {
        "transcript": [
            {"speaker": s.speaker, "start": s.start, "end": s.end, "text": s.text}
            for s in session.transcript
        ],
        "summary": session.summary or "",
    }


def canonical(value) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()


def content_hash(content: dict) -> str:
    return hashlib.sha256(canonical(content)).hexdigest()


def chain_hash(prev: str, content: str, audio: dict, at: str, reason: str) -> str:
    """A seal's link: everything it vouches for, and the seal before it."""
    return hashlib.sha256(canonical([prev, content, audio, at, reason])).hexdigest()
