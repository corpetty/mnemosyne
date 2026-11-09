"""What a meeting's record is made of, for versions and seals (services/records.py), hashed the
same way wherever it is checked: in the app, in an export, in the standalone checker.

Everything here is pure (no I/O, no database, no clock). Two formats of seal exist:

Format 1 (old rows, verified forever as they were): `content_hash(content_of(session))` is the
SHA-256 of the transcript lines and the summary markdown, `audio` the SHA-256 of each file as
stored, `chain_hash` the link.

Format 2 (records_by_frontier plan, docs/plans/2026-11-09-records-by-frontier.md): a seal vouches
for a MANIFEST of per-part digests (a Merkle-style root over the meeting):

    part_digest(name, payload) = sha256(canonical({"part": name, "v": 1, "data": payload}))
    manifest_hash              = sha256(canonical(manifest))
    chain_hash_v2 = sha256(canonical(["mnemosyne/seal/2", prev, manifest_hash, at, reason]))

Canonical encoding (`canonical`; export checkers and every stored seal depend on it, so none
of this may change without bumping the format and `PART_VERSION`):

- JSON with object keys sorted (by code point), separators `,` and `:` and no whitespace,
  `ensure_ascii=False`, encoded as UTF-8. Lists keep their order.
- Strings are hashed exactly as stored: no Unicode normalisation, no trimming. (A lone
  surrogate cannot be encoded and raises; SQLite cannot hold one either.)
- Numbers are Python's `json` output: `float` as `repr` (shortest round trip, `1.0` stays
  `1.0`, `1e-07`), `int` as digits. NaN and infinity are not valid and never occur. Transcript
  times are coerced with `float()` so a JSON round trip (int `5` vs float `5.0`) cannot change
  a digest.
- Times (`created_at` and the like): `datetime.isoformat()` of the value as the model holds it
  (naive stays naive: `2026-11-09T10:00:00`; aware gets its offset: `...+00:00`), `None` stays
  null, a string is taken verbatim. The one exception is `summary_data`, which is pydantic's
  `model_dump(mode="json")`: there an aware UTC time ends in `Z` and a `date` is `YYYY-MM-DD`.
  The vectors (tests/vectors/records_v2.json) pin both.
- `summary_data` is dumped with `exclude_defaults=True`, so a field added to the model in a
  later release (with its default) does not change the digest of a summary that does not use it.
  The converse also holds: changing an existing field's default, renaming or removing a field
  changes the digests of old meetings; the vectors fail CI when that happens.
- Objects given to the payload functions may be models (attribute access) or plain dicts
  with the same field names, so an export's JSON files hash the same as the live objects.

A part payload is a JSON value; the parts are `PARTS` (transcript, summary, summary_data,
notes, comments, meta, external_notes). The manifest also names the audio files by their
plaintext digest (`audio`, keyed by the file name without `.enc`), and the audio that was
deleted on purpose (`erased`, cumulative, so a later seal still verifies without the file).
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Mapping
from datetime import date, datetime
from typing import Any, NamedTuple

# ---- format 1 -------------------------------------------------------------------------------


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


# ---- format 2: part payloads ----------------------------------------------------------------

FORMAT = 2
PART_VERSION = 1  # the "v" in every part digest; bump with any payload definition change
SEAL_DOMAIN = "mnemosyne/seal/2"
PARTS = (
    "transcript",
    "summary",
    "summary_data",
    "notes",
    "comments",
    "meta",
    "external_notes",
)
ENCRYPTED_SUFFIX = ".enc"

COMMENT_TIMES = ("created_at", "updated_at", "resolved_at", "approved_at", "hidden_at")


def _get(obj, name: str, default=None):
    if isinstance(obj, Mapping):
        return obj.get(name, default)
    return getattr(obj, name, default)


def _ts(value) -> str | None:
    if value is None or isinstance(value, str):
        return value
    if isinstance(value, datetime | date):
        return value.isoformat()
    raise TypeError(f"not a time: {value!r}")


def transcript_payload(segments: Iterable) -> list[dict]:
    """The lines as format 1 has them: speaker, start, end, text, in order (not their words)."""
    return [
        {
            "speaker": _get(s, "speaker"),
            "start": float(_get(s, "start")),
            "end": float(_get(s, "end")),
            "text": _get(s, "text"),
        }
        for s in segments
    ]


def summary_payload(summary: str | None) -> str:
    return summary or ""


def summary_data_payload(summary_data) -> dict | None:
    """pydantic's JSON dump without the fields still at their default; None when there is none.
    A dict is taken to be a payload already (an export's summary.json) and returned as it is."""
    if summary_data is None:
        return None
    if isinstance(summary_data, Mapping):
        return dict(summary_data)
    return summary_data.model_dump(mode="json", exclude_defaults=True)


def notes_payload(notes: str | None) -> str:
    """The folded notes markdown, raw: image shas, chips and section refs included."""
    return notes or ""


def comments_payload(comments: Iterable) -> list[dict]:
    """Every comment of the meeting (deleted ones too), sorted by id, only the stored fields
    (not what is worked out when read: changed, detached, can_*, notes_range...)."""
    out = []
    for c in comments:
        out.append(
            {
                "id": _get(c, "id"),
                "parent_id": _get(c, "parent_id", ""),
                "target": _get(c, "target", ""),
                "target_id": _get(c, "target_id", ""),
                "anchor": dict(_get(c, "anchor", None) or {}),
                "quote": _get(c, "quote", ""),
                "text": _get(c, "text"),
                "author_id": _get(c, "author_id", ""),
                "author": _get(c, "author", ""),
                "mentions": list(_get(c, "mentions", None) or []),
                "created_at": _ts(_get(c, "created_at")),
                "updated_at": _ts(_get(c, "updated_at")),
                "resolved_at": _ts(_get(c, "resolved_at")),
                "resolved_by": _get(c, "resolved_by", ""),
                "deleted": bool(_get(c, "deleted", False)),
                "approved_at": _ts(_get(c, "approved_at")),
                "hidden_at": _ts(_get(c, "hidden_at")),
            }
        )
    return sorted(out, key=lambda c: c["id"])


def meta_payload(session) -> dict:
    """Name, participants, attendees, meeting type, owner and agenda (text and covered)."""
    return {
        "name": _get(session, "name", ""),
        "participants": list(_get(session, "participants", None) or []),
        "attendees": list(_get(session, "attendees", None) or []),
        "meeting_type": _get(session, "meeting_type", ""),
        "owner_id": _get(session, "owner_id", ""),
        "agenda": [
            {"text": _get(a, "text"), "covered": bool(_get(a, "covered", False))}
            for a in (_get(session, "agenda", None) or [])
        ],
    }


def external_notes_payload(notes: Iterable | None) -> list[dict]:
    """Other assistants' notes (id, source, text, added_at), sorted by id."""
    out = [
        {
            "id": _get(n, "id"),
            "source": _get(n, "source"),
            "text": _get(n, "text"),
            "added_at": _ts(_get(n, "added_at")),
        }
        for n in (notes or [])
    ]
    return sorted(out, key=lambda n: n["id"])


def part_payloads(session, comments: Iterable = ()) -> dict[str, Any]:
    """The payload of every part of a meeting. `comments` are the meeting's comments (they are
    not on the Session model); `session` may be a Session or a dict of the same fields."""
    return {
        "transcript": transcript_payload(_get(session, "transcript", None) or []),
        "summary": summary_payload(_get(session, "summary", "")),
        "summary_data": summary_data_payload(_get(session, "summary_data")),
        "notes": notes_payload(_get(session, "notes", "")),
        "comments": comments_payload(comments),
        "meta": meta_payload(session),
        "external_notes": external_notes_payload(_get(session, "external_notes", None)),
    }


def part_digest(name: str, payload) -> str:
    """SHA-256 of one part, named so two parts with equal payloads still differ."""
    return hashlib.sha256(canonical({"part": name, "v": PART_VERSION, "data": payload})).hexdigest()


def part_digests(payloads: Mapping[str, Any]) -> dict[str, str]:
    return {name: part_digest(name, payload) for name, payload in payloads.items()}


# ---- format 2: audio entries, the manifest, the chain ---------------------------------------


def audio_key(stored_name: str) -> str:
    """The manifest's name for an audio file: without `.enc`, so turning encryption on or off
    does not rename it."""
    if stored_name.endswith(ENCRYPTED_SUFFIX):
        return stored_name[: -len(ENCRYPTED_SUFFIX)]
    return stored_name


def audio_entry(sha256: str, size: int) -> dict:
    """An audio file in a manifest: the SHA-256 and length of its plaintext."""
    return {"sha256": sha256, "bytes": int(size)}


def build_manifest(
    *,
    session_id: str,
    rev: int,
    content_v1: str,
    parts: Mapping[str, str],
    audio: Mapping[str, Mapping] | None = None,
    audio_here: bool = True,
    erased: Mapping[str, Mapping] | None = None,
) -> dict:
    """The manifest a format 2 seal vouches for (see the plan, 3.2). `parts` must be exactly
    `PARTS`' digests; `audio` is empty when the audio is not on this computer (`audio_here`
    false); `erased` is every file ever erased, not just by the latest seal."""
    if set(parts) != set(PARTS):
        raise ValueError(f"parts must be exactly {sorted(PARTS)}, got {sorted(parts)}")
    return {
        "format": FORMAT,
        "session": session_id,
        "rev": int(rev),
        "content_v1": content_v1,
        "parts": dict(parts),
        "audio": {k: dict(v) for k, v in (audio or {}).items()} if audio_here else {},
        "audio_here": bool(audio_here),
        "erased": {k: dict(v) for k, v in (erased or {}).items()},
    }


def manifest_for(
    session,
    comments: Iterable = (),
    *,
    rev: int,
    audio: Mapping[str, Mapping] | None = None,
    audio_here: bool = True,
    erased: Mapping[str, Mapping] | None = None,
) -> dict:
    """The manifest of a meeting as it is now: payloads, digests and `content_v1`."""
    return build_manifest(
        session_id=_get(session, "id"),
        rev=rev,
        content_v1=content_hash(content_of(session)),
        parts=part_digests(part_payloads(session, comments)),
        audio=audio,
        audio_here=audio_here,
        erased=erased,
    )


def manifest_hash(manifest: Mapping) -> str:
    return hashlib.sha256(canonical(manifest)).hexdigest()


def chain_hash_v2(prev: str, manifest_digest: str, at: str, reason: str) -> str:
    """A format 2 seal's link. `prev` is the chain hash of whatever row came before, of either
    format; `manifest_digest` is `manifest_hash(manifest)`."""
    return hashlib.sha256(canonical([SEAL_DOMAIN, prev, manifest_digest, at, reason])).hexdigest()


def manifest_problems(manifest) -> list[str]:
    """Why a stored manifest cannot be a manifest (wrong shape), [] when it is well formed."""
    if not isinstance(manifest, Mapping):
        return ["The manifest is not an object"]
    problems = []
    if manifest.get("format") != FORMAT:
        problems.append(f"The manifest has format {manifest.get('format')!r}, not {FORMAT}")
    for key, kind in (("session", str), ("rev", int), ("content_v1", str), ("audio_here", bool)):
        if not isinstance(manifest.get(key), kind) or (
            kind is int and isinstance(manifest.get(key), bool)
        ):
            problems.append(f"The manifest's {key} is missing or of the wrong type")
    parts = manifest.get("parts")
    if not isinstance(parts, Mapping) or set(parts) != set(PARTS):
        problems.append("The manifest does not list exactly the parts of a record")
    for key in ("audio", "erased"):
        entries = manifest.get(key)
        if not isinstance(entries, Mapping):
            problems.append(f"The manifest's {key} is not an object")
            continue
        for name, entry in entries.items():
            if (
                not isinstance(entry, Mapping)
                or not isinstance(entry.get("sha256"), str)
                or not isinstance(entry.get("bytes"), int)
            ):
                problems.append(f"The manifest's {key} entry {name} is malformed")
    return problems


# ---- erasure ---------------------------------------------------------------------------------


def erase_audio(
    audio: Mapping[str, Mapping],
    erased: Mapping[str, Mapping],
    measured: Mapping[str, Mapping],
    *,
    at: str,
    by: str,
    reason: str,
    deletion: int | None,
) -> tuple[dict, dict]:
    """Move files from `audio` into `erased`, for the manifest of an erasure seal.

    `measured` is what the files are NOW (name -> `audio_entry`, hashed from their plaintext
    just before deletion). Each erased entry records those measured values plus `matched`: did
    the file still equal what the last seal said (false when it differs or was never sealed).
    Names in `audio` but not in `measured` stay in `audio`. A name already in `erased` keeps its
    first entry (an erasure never changes). Returns new `(audio, erased)`; the inputs are not
    modified."""
    new_audio = {k: dict(v) for k, v in audio.items()}
    new_erased = {k: dict(v) for k, v in erased.items()}
    for name, now in measured.items():
        if name in new_erased:
            continue
        before = new_audio.pop(name, None)
        new_erased[name] = {
            "sha256": now["sha256"],
            "bytes": int(now["bytes"]),
            "at": at,
            "by": by,
            "reason": reason,
            "deletion": deletion,
            "matched": before is not None and before.get("sha256") == now["sha256"],
        }
    return new_audio, new_erased


# ---- comparison ------------------------------------------------------------------------------


class PartDiff(NamedTuple):
    name: str
    kind: str  # "differs" | "added" (only now) | "removed" (only in the seal)


class AudioDiff(NamedTuple):
    name: str
    kind: str  # "missing" | "differs" | "added"


def compare_parts(sealed: Mapping[str, str], current: Mapping[str, str]) -> list[PartDiff]:
    """Parts whose digest differs between a manifest's `parts` and the meeting now, in
    `PARTS` order then alphabetically. Parts that are equal are not listed."""
    order = {name: i for i, name in enumerate(PARTS)}
    out = []
    for name in sorted(sealed.keys() | current.keys(), key=lambda n: (order.get(n, 99), n)):
        if name not in current:
            out.append(PartDiff(name, "removed"))
        elif name not in sealed:
            out.append(PartDiff(name, "added"))
        elif sealed[name] != current[name]:
            out.append(PartDiff(name, "differs"))
    return out


def compare_audio(
    sealed: Mapping[str, Mapping],
    erased: Mapping[str, Mapping],
    current: Mapping[str, Mapping],
) -> list[AudioDiff]:
    """Audio files that do not match a manifest's `audio`, sorted by name. An erased file is
    expected to be absent; if a file of that name is here, it is only a difference when its
    SHA-256 is not the erased one (a copy put back is not tampering)."""
    out = []
    for name in sorted(sealed.keys() | current.keys() | erased.keys()):
        here = current.get(name)
        if name in erased:
            if here is not None and here.get("sha256") != erased[name].get("sha256"):
                out.append(AudioDiff(name, "differs"))
        elif name not in sealed:
            out.append(AudioDiff(name, "added"))
        elif here is None:
            out.append(AudioDiff(name, "missing"))
        elif here.get("sha256") != sealed[name].get("sha256"):
            out.append(AudioDiff(name, "differs"))
    return out


def compare_step(prev: Mapping, nxt: Mapping, n: int | str = "") -> list[str]:
    """Continuity between two consecutive format 2 manifests (`nxt` is seal number `n`):
    - an audio file may leave `audio` only by moving into `erased` (not by vanishing);
    - an erased entry never disappears or changes;
    - an erased entry's `matched` is true exactly when its SHA-256 is the one the previous
      seal had for that file (an erasure of a file that no longer matched is recorded with
      its own SHA-256 and `matched` false, so the digests of two entries may differ).
    Audio is not compared across a seal on a computer that has none (`audio_here` false)."""
    at = f" at seal {n}" if n != "" else ""
    problems = []
    prev_erased = prev.get("erased", {})
    next_erased = nxt.get("erased", {})
    for name, entry in prev_erased.items():
        if name not in next_erased:
            problems.append(f"Erased audio file {name} left the record{at}")
        elif next_erased[name] != entry:
            problems.append(f"Erased audio file {name} was changed{at}")
    prev_audio = prev.get("audio", {})
    if prev.get("audio_here", True) and nxt.get("audio_here", True):
        for name in sorted(prev_audio):
            if name not in nxt.get("audio", {}) and name not in next_erased:
                problems.append(f"Audio file {name} left the record{at} without being erased")
    for name, entry in sorted(next_erased.items()):
        if name in prev_erased:
            continue
        before = prev_audio.get(name)
        same = before is not None and before.get("sha256") == entry.get("sha256")
        if bool(entry.get("matched")) != same:
            problems.append(f"Erased audio file {name}{at} is recorded as matching wrongly")
    return problems
