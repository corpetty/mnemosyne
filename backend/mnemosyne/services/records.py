"""Meeting records a team can rely on, when Mnemosyne is where its meetings are kept.

- Versions: a replaced transcript or summary is kept (storage/sqlite.py keep_version).
- Seals: after a transcription, a summary, an edit or an event (EVENT_SEALS), a manifest of the
  meeting (storage/records.py, format 2: a digest of each part — transcript, summary, its
  structured parts, notes, comments, details, other assistants' notes — and of each audio file's
  plaintext), chained to the previous seal. Seals made before (format 1: the transcript and
  summary, and each audio file as stored) stay in the chain and verify as they did. `verify`
  recomputes the chain and compares the last seal with what is there now, so a change made
  outside the app (a row edited in the database, a file swapped) shows, while a change the app
  made after the seal (the meeting's `rev`, its `sync_seq`, moved on) is pending, not a problem.
  Someone able to rewrite the whole database could rewrite the chain too: the chain head goes
  into exports and backups, which live elsewhere.
- Retention: `records_retention_years` after a meeting, deleting it or its audio needs an admin
  and a reason; a legal hold refuses it to everyone. Every deletion is logged (`deletions`),
  and that log outlives the meeting.
- Export: meetings (one, or a date range) as a zip with audio, the notes' images, transcript,
  summary, versions, history, seals and SHA256SUMS.
"""

from __future__ import annotations

import hashlib
import json
import logging
import zipfile
from collections.abc import Mapping
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Literal

from .. import access
from ..models.base import ApiModel
from ..storage.records import (
    PARTS,
    audio_entry,
    audio_key,
    chain_hash,
    chain_hash_v2,
    compare_audio,
    compare_parts,
    compare_step,
    content_hash,
    content_of,
    manifest_for,
    manifest_hash,
    manifest_problems,
)
from . import notes_media

if TYPE_CHECKING:
    from ..api.context import AppContext
    from ..models.session import Session

logger = logging.getLogger(__name__)

_file_hashes: dict[tuple[str, int, float], str] = {}


def _file_sha256(path: Path) -> str:
    """SHA-256 of a file as stored (format 1 seals), cached by path, size and mtime."""
    st = path.stat()
    key = (str(path), st.st_size, st.st_mtime)
    if key not in _file_hashes:
        h = hashlib.sha256()
        with path.open("rb") as f:
            while chunk := f.read(1 << 20):
                h.update(chunk)
        _file_hashes[key] = h.hexdigest()
    return _file_hashes[key]


_plain_digests: dict[tuple[str, int, int], dict] = {}
_PLAIN_DIGESTS_MAX = 4096


def _plain_entry(path: Path, file_key: bytes | None) -> dict:
    """A file's manifest entry (`audio_entry`): the SHA-256 and length of its PLAINTEXT, an
    encrypted file decrypted chunk by chunk (never written out), cached in memory by path,
    size and mtime_ns. PermissionError when it is encrypted and the key is not available;
    a file that does not decrypt raises the cipher's error."""
    from ..storage.crypto import EncryptedFile, is_encrypted

    st = path.stat()
    key = (str(path), st.st_size, st.st_mtime_ns)
    cached = _plain_digests.get(key)
    if cached is not None:
        return dict(cached)
    h = hashlib.sha256()
    if is_encrypted(path):
        if file_key is None:
            raise PermissionError("Meetings are encrypted and the key is not available")
        sealed = EncryptedFile(path, file_key)
        step = sealed.chunk * 16
        for start in range(0, sealed.size, step):
            h.update(sealed.read(start, start + step))
        size = sealed.size
    else:
        size = 0
        with path.open("rb") as f:
            while chunk := f.read(1 << 20):
                h.update(chunk)
                size += len(chunk)
    entry = audio_entry(h.hexdigest(), size)
    if len(_plain_digests) >= _PLAIN_DIGESTS_MAX:
        _plain_digests.clear()
    _plain_digests[key] = entry
    return dict(entry)


def audio_files(session: Session) -> list[Path]:
    if session.origin:  # recorded on another computer: its audio is not here
        return []
    paths = [r.path for r in session.recordings] + [session.audio_file or ""]
    return [Path(p) for p in dict.fromkeys(paths) if p and Path(p).is_file()]


def audio_hashes(session: Session) -> dict[str, str]:
    """SHA-256 of each audio file as stored (encrypted ones as encrypted), by file name: what
    format 1 seals hold."""
    return {p.name: _file_sha256(p) for p in audio_files(session)}


LOCKED = "locked"  # the file is encrypted and the key is not available
UNREADABLE = "unreadable"  # the file does not decrypt (or cannot be read)


def _audio_now(
    app: AppContext, session: Session, last: dict | None = None
) -> tuple[dict[str, dict], dict[str, dict], dict[str, str]]:
    """The meeting's audio files as manifest entries, keyed by name without `.enc`; their file
    stats (`audio_stat`: the next seal may reuse a digest by them); and the names that could not
    be hashed, with why (LOCKED or UNREADABLE). With `last` (the previous format 2 seal), a file
    whose path, size and mtime_ns are what that seal recorded keeps that seal's digest instead
    of being read again (a seal is cheap; `verify` passes no `last` and rehashes, through the
    in-memory cache)."""
    entries: dict[str, dict] = {}
    stats: dict[str, dict] = {}
    failed: dict[str, str] = {}
    if session.origin:
        return entries, stats, failed
    sealed = ((last or {}).get("manifest") or {}).get("audio") or {}
    seen = (last or {}).get("audio_stat") or {}
    for path in audio_files(session):
        name = audio_key(path.name)
        try:
            st = path.stat()
        except OSError:  # gone since it was listed
            continue
        stat = {"file": str(path), "size": st.st_size, "mtime_ns": st.st_mtime_ns}
        stats[name] = stat
        if seen.get(name) == stat and name in sealed:
            entries[name] = dict(sealed[name])
            continue
        try:
            entries[name] = _plain_entry(path, app.file_key)
        except PermissionError:
            failed[name] = LOCKED
        except Exception:  # a cipher error, a bad header, an I/O error
            logger.warning("Could not hash audio %s", path, exc_info=True)
            failed[name] = UNREADABLE
    return entries, stats, failed


# Seals that record something that happened rather than a change of content: made even when the
# content and audio are what the last seal covered, so the chain shows when and in which order
# (services/publishing.py).
EVENT_SEALS = frozenset(
    {
        "published",
        "publication updated",
        "unpublished",
        "guest notes started",
        "guest notes ended",
        "guest notes stopped",
    }
)

# A meeting whose last seal is format 1 gets a format 2 seal when its record is opened (D5).
UPGRADE_REASON = "sealed in the new format"


def _sealable(session: Session, comments: list) -> bool:
    """A meeting with something to keep: a transcript, a summary, notes or comments (D12: a
    notes-only meeting is a record too). Just audio is not, as before."""
    return bool(session.transcript or session.summary or (session.notes or "").strip() or comments)


def seal(app: AppContext, session_id: str, reason: str) -> dict | None:
    """Chain a format 2 seal of the meeting as it is now (blocking: hashes audio; run in a
    thread). None when nothing changed since the last seal (unless an EVENT_SEALS reason), for a
    meeting with nothing to keep, someone else's shared meeting (`shared_from`), or when an audio
    file cannot be hashed (encryption locked, a file that does not decrypt). Never raises: a
    record that cannot be sealed must not fail what it records."""
    try:
        # `rev` first: an app write landing while the meeting is read leaves the seal's rev
        # behind the meeting's, so a difference shows as pending, never as tampering.
        rev = app.repo.sync_seq_of(session_id)
        session = app.repo.get(session_id)
        if session is None or rev is None or session.shared_from:
            return None
        comments = app.repo.list_comments(session_id)
        if not _sealable(session, comments):
            return None
        seals = app.repo.seals(session_id)
        last = seals[-1] if seals and seals[-1]["format"] == 2 and seals[-1]["manifest"] else None
        audio, stats, failed = _audio_now(app, session, last)
        if failed:
            logger.warning(
                "Not sealing session %s (%s): audio %s cannot be hashed",
                session_id,
                reason,
                ", ".join(f"{n} ({why})" for n, why in sorted(failed.items())),
            )
            return None
        erased = dict(last["manifest"].get("erased") or {}) if last else {}
        manifest = manifest_for(
            session,
            comments,
            rev=rev,
            audio={n: e for n, e in audio.items() if n not in erased},
            audio_here=not session.origin,
            erased=erased,
        )
        return app.repo.add_seal(
            session_id,
            reason,
            force=reason in EVENT_SEALS,
            manifest=manifest,
            audio_stat=stats,
        )
    except Exception:
        logger.warning("Could not seal session %s", session_id, exc_info=True)
        return None


def seal_later(app: AppContext, session_id: str, reason: str) -> None:
    """Seal in the background, so an edit does not wait for it (hashing is cached)."""
    import asyncio

    try:
        asyncio.get_running_loop().create_task(asyncio.to_thread(seal, app, session_id, reason))
    except RuntimeError:  # no event loop (a script): seal now
        seal(app, session_id, reason)


def protected_ids(app: AppContext) -> set[str]:
    """Meetings whose audio retention must not remove: on legal hold, or created within
    `records_retention_years`."""
    years = app.settings.records_retention_years
    since = ""
    if years > 0:
        today = date.today()
        since = today.replace(year=today.year - years, day=min(today.day, 28)).isoformat()
    return app.repo.protected_ids(since)


# ---- verification ---------------------------------------------------------------------------


class PartCheck(ApiModel):
    """One part of the record compared with the last seal: a part (storage/records.py PARTS) or
    an audio file ("audio/<name without .enc>")."""

    name: str
    # pending: changed in the app, not sealed yet; unavailable: cannot be compared here
    status: Literal["same", "differs", "pending", "unavailable"]
    detail: str  # why it is unavailable; "" otherwise


class Erasure(ApiModel):
    """Audio deleted on purpose (retention, an admin with a reason): still in the record by its
    plaintext SHA-256, so a copy kept elsewhere (an export) can be checked against it."""

    name: str
    sha256: str
    bytes: int
    at: str
    by: str
    reason: str
    matched: bool  # it still equalled the sealed file when it was deleted


class Verification(ApiModel):
    ok: bool  # false only for problems: pending changes are fine
    seals: int
    last_sealed_at: datetime | None
    chain_head: str  # the last seal's chain hash: write it down to prove the record later
    problems: list[str]
    pending: bool  # the app changed the meeting since the last seal; the next seal covers it
    format: int  # of the last seal: 1 or 2 (0 without seals)
    # Format 2: every part and audio file. Format 1: only audio files that cannot be compared
    # (re-encrypted or decrypted since that seal).
    parts: list[PartCheck]
    erased: list[Erasure]


SAME, DIFFERS, PENDING, UNAVAILABLE = "same", "differs", "pending", "unavailable"

CONTENT_DIFFERS = "The transcript or summary differs from the last seal"
_PART_PROBLEMS = {
    "transcript": CONTENT_DIFFERS,
    "summary": CONTENT_DIFFERS,
    "summary_data": "The summary's decisions, questions or action items differ from the last seal",
    "notes": "The notes differ from the last seal",
    "comments": "The comments differ from the last seal",
    "meta": "The meeting's name, people or agenda differ from the last seal",
    "external_notes": "Other assistants' notes differ from the last seal",
}
_AUDIO_PROBLEMS = {
    "missing": "Audio file {} is missing",
    "differs": "Audio file {} differs from the last seal",
    "added": "Audio file {} was added after the last seal",
}
REENCRYPTED = (
    "Encrypted or decrypted since the last seal, which hashed the file as stored; open the "
    "record once to seal it in the new format"
)
IMAGE_CHANGED = "An image in the notes was changed"


def _add(problems: list[str], problem: str) -> None:
    if problem not in problems:
        problems.append(problem)


def _check_chain(session_id: str, seals: list[dict], problems: list[str]) -> None:
    """Every seal follows the one before and still hashes to its link; a format 2 seal's manifest
    hashes to its `content_hash`, is this meeting's, and continues the previous format 2 one
    (`compare_step`: audio leaves only by erasure, erasures never change)."""
    prev = ""
    prev_manifest = None
    for i, s in enumerate(seals, 1):
        if s["prev"] != prev:
            problems.append(f"Seal {i} does not follow seal {i - 1}")
        changed = f"Seal {i} ({s['at'][:16]}) was changed after it was made"
        if s["format"] == 2:
            m = s["manifest"]
            if (
                m is None
                or manifest_problems(m)
                or manifest_hash(m) != s["content_hash"]
                or chain_hash_v2(s["prev"], s["content_hash"], s["at"], s["reason"]) != s["chain"]
            ):
                problems.append(changed)
                prev_manifest = None
            elif m["session"] != session_id:
                problems.append(f"Seal {i} ({s['at'][:16]}) is another meeting's")
                prev_manifest = None
            else:
                if prev_manifest is not None:
                    problems.extend(compare_step(prev_manifest, m, i))
                prev_manifest = m
        else:
            if (
                chain_hash(s["prev"], s["content_hash"], s["audio"], s["at"], s["reason"])
                != s["chain"]
            ):
                problems.append(changed)
            prev_manifest = None
        prev = s["chain"]


def _compare_v1(session: Session, last: dict, problems: list[str]) -> list[PartCheck]:
    """The meeting now against a format 1 seal: as before, except that a file sealed as `x` and
    now `x.enc` (or the other way) cannot be compared (ciphertext), which is not a problem."""
    checks: list[PartCheck] = []
    if content_hash(content_of(session)) != last["content_hash"]:
        problems.append(CONTENT_DIFFERS)
    if session.origin:  # the audio files are on the computer that recorded it
        return checks
    now = audio_hashes(session)
    twins: set[str] = set()
    for name, digest in last["audio"].items():
        if name in now:
            if now[name] != digest:
                problems.append(_AUDIO_PROBLEMS["differs"].format(name))
            continue
        twin = name.removesuffix(".enc") if name.endswith(".enc") else name + ".enc"
        if twin in now and twin not in last["audio"]:
            twins.add(twin)
            checks.append(
                PartCheck(name=f"audio/{audio_key(name)}", status=UNAVAILABLE, detail=REENCRYPTED)
            )
        else:
            problems.append(_AUDIO_PROBLEMS["missing"].format(name))
    for name in sorted(now.keys() - last["audio"].keys() - twins):
        problems.append(_AUDIO_PROBLEMS["added"].format(name))
    return checks


def _compare_v2(
    app: AppContext, session: Session, sealed: dict, problems: list[str]
) -> tuple[bool, list[PartCheck], list[Erasure]]:
    """The meeting now against a format 2 manifest, part by part and file by file. A difference
    while the meeting's `rev` is past the seal's is pending (the app changed it); with the same
    `rev` it is a problem. Returns (pending, checks, erasures)."""
    comments = app.repo.list_comments(session.id)
    audio, _, failed = _audio_now(app, session)
    # `rev` last: an app write after the meeting was read can only make a difference pending.
    rev_now = app.repo.sync_seq_of(session.id) or 0
    in_app = rev_now > sealed["rev"]
    pending = False
    checks: list[PartCheck] = []

    def differ(name: str, problem: str) -> None:
        nonlocal pending
        if in_app:
            pending = True
            checks.append(PartCheck(name=name, status=PENDING, detail=""))
        else:
            checks.append(PartCheck(name=name, status=DIFFERS, detail=""))
            _add(problems, problem)

    current = manifest_for(session, comments, rev=sealed["rev"], audio_here=False)
    diffs = {d.name for d in compare_parts(sealed["parts"], current["parts"])}
    for name in PARTS:
        if name in diffs:
            differ(name, _PART_PROBLEMS[name])
        else:
            checks.append(PartCheck(name=name, status=SAME, detail=""))

    erased = sealed["erased"]
    if sealed["audio_here"] and not session.origin:
        now = dict(audio)
        for name, why in failed.items():
            if why == LOCKED:  # not comparable: taken as sealed, shown as unavailable
                if name in sealed["audio"]:
                    now[name] = sealed["audio"][name]
            else:
                now[name] = {"sha256": "", "bytes": -1}  # differs from anything sealed
        kinds = {d.name: d.kind for d in compare_audio(sealed["audio"], erased, now)}
        for name in sorted((sealed["audio"].keys() | now.keys() | failed.keys()) - erased.keys()):
            label = f"audio/{name}"
            if failed.get(name) == LOCKED:
                checks.append(
                    PartCheck(name=label, status=UNAVAILABLE, detail="Encryption is locked")
                )
            elif failed.get(name) == UNREADABLE:
                checks.append(PartCheck(name=label, status=DIFFERS, detail=""))
                _add(problems, _AUDIO_PROBLEMS["differs"].format(name) + " (it does not decrypt)")
            elif name in kinds:
                differ(label, _AUDIO_PROBLEMS[kinds[name]].format(name))
            else:
                checks.append(PartCheck(name=label, status=SAME, detail=""))
        for name in sorted(kinds.keys() & erased.keys()):  # a different file where one was erased
            checks.append(PartCheck(name=f"audio/{name}", status=DIFFERS, detail=""))
            _add(problems, f"Audio file {name} was deleted and a different file is in its place")

    erasures = []
    for name, e in sorted(erased.items()):
        erasures.append(
            Erasure(
                name=name,
                sha256=e["sha256"],
                bytes=e["bytes"],
                at=str(e.get("at") or ""),
                by=str(e.get("by") or ""),
                reason=str(e.get("reason") or ""),
                matched=bool(e.get("matched")),
            )
        )
        if not e.get("matched"):
            _add(problems, f"Audio file {name} no longer matched the record when it was deleted")
    return pending, checks, erasures


def _check_images(app: AppContext, session: Session, problems: list[str]) -> None:
    """Each image the notes name whose bytes are here must hash to its name: the notes' digest
    commits to the sha, so a swapped blob is a changed record."""
    for value in notes_media.values_in(session.notes):
        sha = value.split(".", 1)[0]
        file = notes_media.path(app, session.id, sha)
        if file is None:
            continue
        try:
            digest = _plain_entry(file, app.file_key)["sha256"]
        except PermissionError:  # encryption locked: not comparable
            continue
        except Exception:
            digest = ""
        if digest != sha:
            _add(problems, IMAGE_CHANGED)


def verify(app: AppContext, session_id: str) -> Verification:
    """Recompute the chain of seals and compare the last one with the meeting as it is now."""
    session = app.repo.get(session_id)
    seals = app.repo.seals(session_id)
    problems: list[str] = []
    _check_chain(session_id, seals, problems)
    pending = False
    parts: list[PartCheck] = []
    erased: list[Erasure] = []
    last = seals[-1] if seals else None
    if session is not None and last is not None:
        if last["format"] == 1:
            parts = _compare_v1(session, last, problems)
        elif isinstance(last["manifest"], Mapping) and not manifest_problems(last["manifest"]):
            pending, parts, erased = _compare_v2(app, session, last["manifest"], problems)
        # A malformed last manifest is already a problem from the chain check.
        _check_images(app, session, problems)
    return Verification(
        ok=not problems,
        seals=len(seals),
        last_sealed_at=last["at"] if last else None,
        chain_head=last["chain"] if last else "",
        problems=problems,
        pending=pending,
        format=last["format"] if last else 0,
        parts=parts,
        erased=erased,
    )


def open_record(app: AppContext, session_id: str) -> Verification:
    """`verify` for the Record card (blocking). A meeting whose last seal is format 1 and still
    matches is sealed in format 2 first (D5: upgraded lazily, never in a pass over the library)."""
    check = verify(app, session_id)
    if check.format == 1 and check.ok and seal(app, session_id, UPGRADE_REASON) is not None:
        check = verify(app, session_id)
    return check


# ---- retention and legal holds --------------------------------------------------------------


def kept_until(app: AppContext, session: Session) -> date | None:
    years = app.settings.records_retention_years
    if years <= 0:
        return None
    created = session.created_at.date()
    try:
        return created.replace(year=created.year + years)
    except ValueError:  # 29 February
        return created.replace(year=created.year + years, day=28)


def check_delete(app: AppContext, session: Session, what: str, reason: str = "") -> None:
    """May the caller delete this meeting (what="meeting") or its audio ("audio")? Raises
    access.Forbidden when not, ValueError when a reason is needed; logs the deletion when it
    goes ahead. (Deletion by age is `automatic_deletion_allowed`.)"""
    if session.legal_hold:
        raise access.Forbidden(f"On legal hold ({session.legal_hold}): nothing can be deleted")
    until = kept_until(app, session)
    if until is not None and date.today() < until:
        if not access.is_admin():
            raise access.Forbidden(f"Kept as a record until {until.isoformat()}")
        if not reason.strip():
            raise ValueError(f"Kept as a record until {until.isoformat()}: give a reason")
    who = access.principal()
    app.repo.log_deletion(
        session, what, who.name if who else "", who.role if who else "", reason.strip()
    )


def automatic_deletion_allowed(app: AppContext, session: Session) -> bool:
    """For retention jobs: no audio deleted on hold or inside the records period."""
    if session.legal_hold:
        return False
    until = kept_until(app, session)
    return until is None or date.today() >= until


# ---- export -------------------------------------------------------------------------------

README = """Mnemosyne meeting records
=========================

Exported {at} by {by} from {host}.

One folder per meeting:
  meeting.json       name, dates, owner, who it is shared with, participants, attendees, legal
                     hold, retention
  transcript.txt     who said what, when
  transcript.json    the same, with word times when available
  summary.md         the summary as shown in the app
  summary.json       its structured parts (decisions, action items, client facts, ...)
  versions.json      earlier transcripts and summaries, with when, why and by whom they changed
  history.json       what happened to the meeting: recordings, consent, transcriptions,
                     summaries, who opened, played or exported it
  seals.json         the chain of seals and its verification when exported
  supervision.json   lines flagged for review phrases and the reviewers' sign-offs
                     (only for meetings that have any)
  publications.json  links made to share the meeting (only for meetings that have any): when
                     and by whom each version was published, what it showed, where it was
                     put, when it expired or was withdrawn. Never the links themselves
  guest_notes.json   the meeting's notes opened to guests (only for meetings that had it): the
                     host, the room's id, how guests reached it (a publish host, or iroh to
                     this computer with its relay and a mirror host), when and by whom it was
                     started, ended and stopped. Never the link, its key or the host tokens
  audio/             the recordings (each channel and the mix), unencrypted
  media/             the images in the meeting's notes, as <sha256>.<webp|jpeg>, unencrypted
                     (only for meetings that have any; the seals cover the notes' text, not them)

deletions.json lists meetings and audio deleted in the period, with who, when and why.

To check that nothing in this export changed since it was made: `sha256sum -c SHA256SUMS`.
To check a meeting's record against the server later: its seals.json ends with the chain head
("chain_head"); the same meeting verified on the server gives the same value while nothing was
changed there.
"""


class ExportRequest(ApiModel):
    session_ids: list[str] = []
    start: date | None = None  # meetings created on start..end, inclusive
    end: date | None = None


def _meetings(app: AppContext, request: ExportRequest) -> list[Session]:
    if request.session_ids:
        found = [app.repo.get(i) for i in dict.fromkeys(request.session_ids)]
        return [s for s in found if s is not None]
    start = request.start or date(1970, 1, 1)
    end = request.end or date.today()
    return app.repo.sessions_between(start, end)


def _transcript_text(session: Session) -> str:
    def ts(t: float) -> str:
        return f"{int(t // 3600):02d}:{int(t % 3600 // 60):02d}:{int(t % 60):02d}"

    return "".join(f"[{ts(s.start)}] {s.speaker}: {s.text}\n" for s in session.transcript)


def export_zip(app: AppContext, request: ExportRequest, dest: Path, progress=None) -> dict:
    """Write the export to `dest` (blocking). Returns what went in."""
    from ..storage.crypto import plaintext

    who = access.principal()
    meetings = _meetings(app, request)
    owners = {u.id: u.name for u in app.users.list()}
    sums: list[tuple[str, str]] = []
    dest.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(dest, "w", zipfile.ZIP_DEFLATED) as z:

        def put(name: str, data: bytes) -> None:
            z.writestr(name, data)
            sums.append((hashlib.sha256(data).hexdigest(), name))

        for n, session in enumerate(meetings, 1):
            folder = f"{session.created_at:%Y-%m-%d} {session.name[:60]} ({session.id})".replace(
                "/", "-"
            )
            until = kept_until(app, session)
            meta = {
                "id": session.id,
                "name": session.name,
                "created_at": session.created_at.isoformat(),
                "owner": owners.get(session.owner_id, session.owner_id),
                "participants": session.participants,
                "attendees": session.attendees,
                "meeting_type": session.meeting_type,
                "legal_hold": session.legal_hold,
                "kept_until": until.isoformat() if until else None,
                "shared_with": [
                    {
                        **r,
                        "name": "everyone" if r["user_id"] == "*" else owners.get(r["user_id"], ""),
                    }
                    for r in app.repo.shares(session.id)
                ],
            }
            put(f"{folder}/meeting.json", json.dumps(meta, indent=2, default=str).encode())
            put(f"{folder}/transcript.txt", _transcript_text(session).encode())
            put(
                f"{folder}/transcript.json",
                json.dumps(
                    [s.model_dump() for s in session.transcript], indent=1, default=str
                ).encode(),
            )
            put(f"{folder}/summary.md", (session.summary or "").encode())
            data = session.summary_data.model_dump(mode="json") if session.summary_data else {}
            put(f"{folder}/summary.json", json.dumps(data, indent=2, default=str).encode())
            versions = app.repo.versions(session.id, with_content=True)
            put(f"{folder}/versions.json", json.dumps(versions, indent=1, default=str).encode())
            put(
                f"{folder}/history.json",
                json.dumps(app.repo.events(session.id), indent=1, default=str).encode(),
            )
            check = verify(app, session.id)
            seals = {"seals": app.repo.seals(session.id), "verified": check.model_dump(mode="json")}
            put(f"{folder}/seals.json", json.dumps(seals, indent=1, default=str).encode())
            flags, reviews = app.repo.flags(session.id), app.repo.reviews(session.id)
            if flags or reviews:
                supervised = {"flags": flags, "reviews": reviews}
                put(f"{folder}/supervision.json", json.dumps(supervised, indent=1).encode())
            published = publications_record(app, session)
            if published:
                put(
                    f"{folder}/publications.json",
                    json.dumps(published, indent=1, default=str).encode(),
                )
            guests = guest_notes_record(app, session)
            if guests:
                put(
                    f"{folder}/guest_notes.json", json.dumps(guests, indent=1, default=str).encode()
                )
            for path in audio_files(session):
                with plaintext(path, app.file_key) as plain:
                    put(
                        f"{folder}/audio/{path.name.removesuffix('.enc')}", Path(plain).read_bytes()
                    )
            for row in app.repo.media_rows(session.id):  # the notes' images
                try:
                    data = notes_media.read(app, session.id, row["sha256"])
                except FileNotFoundError:  # a row whose bytes are not on this computer
                    continue
                put(f"{folder}/media/{row['sha256']}.{row['ext']}", data)
            if progress:
                progress(n / max(len(meetings), 1))
        start = (request.start or date(1970, 1, 1)).isoformat()
        end = (request.end or date.today()) + timedelta(days=1)
        # The deletion log goes to reviewers and admins, in date-range exports.
        team_wide = who is None or who.role in (access.REVIEWER, access.ADMIN)
        wanted = team_wide and not request.session_ids
        deletions = app.repo.deletions(start, end.isoformat()) if wanted else []
        put("deletions.json", json.dumps(deletions, indent=1, default=str).encode())
        put(
            "README.txt",
            README.format(
                at=datetime.now().isoformat(timespec="minutes"),
                by=who.name if who else "an administrator",
                host=_host(),
            ).encode(),
        )
        z.writestr("SHA256SUMS", "".join(f"{h}  {name}\n" for h, name in sums))
    return {"meetings": len(meetings), "bytes": dest.stat().st_size}


def guest_notes_record(app: AppContext, session: Session) -> dict | None:
    """The meeting's guest notes room for the records export (services/guest_notes.py): where it
    was, when and by whom it was started, ended and stopped, and when it expires. Never the
    link, its key, the room token or the host token."""
    row = app.repo.guest_room(session.id)
    if row is None:
        return None
    return {
        "host": row["host_url"],
        "room": row["room"],
        "created_at": row["created_at"],
        "started_by": row["started_by"],
        "ended_at": row["ended_at"] or None,
        "deleted_at": row["deleted_at"] or None,
        "expires_at": row["expires_at"] or None,
        "transport": row["transport"],
        "endpoint_id": row["endpoint_id"],
        "relay": row["relay"],
        "public_url": row["public_url"],
        "mirror_host": row["mirror_host_url"],
    }


def publications_record(app: AppContext, session: Session) -> list[dict]:
    """The meeting's publications for the records export (services/publishing.py): each one's
    id, where it was put, its versions with their times and who made them (read from the
    meeting's history), what it showed, its expiry and withdrawal. Never the link's key or the
    host token."""
    rows = app.repo.session_publications(session.id)
    events = [
        e
        for e in app.repo.events(session.id)
        if e["kind"]
        in (
            "published",
            "publication_updated",
            "publication_auto_updated",
            "unpublished",
            "publication_expired",
        )
    ]
    out = []
    for row in rows:
        versions, ended = [], {}
        for e in events:
            d = e["detail"]
            if d.get("publication") != row["id"]:
                continue
            if e["kind"] in ("published", "publication_updated", "publication_auto_updated"):
                versions.append(
                    {
                        "auto": e["kind"] == "publication_auto_updated",
                        "version": d.get("version"),
                        "at": e["at"],
                        "by": d.get("by", ""),
                        "where": d.get("where", ""),
                        "parts": d.get("parts", []),
                        "quotes": d.get("quotes", 0),
                        "clips": d.get("clips", 0),
                        "names": d.get("names", ""),
                        "hide_identifiers": d.get("hide_identifiers"),
                    }
                )
            else:
                ended = {"kind": e["kind"], "at": e["at"], "by": d.get("by", "")}
        out.append(
            {
                "id": row["id"],
                "where": row["place"],
                "host": row["host_url"],
                "created_at": row["created_at"],
                "current_version": row["version"],
                "versions": versions,
                "selection": row["selection"],
                "expires_at": row["expires_at"],
                "unpublished_at": row["unpublished_at"],
                "unpublished_by": ended.get("by", "") if row["unpublished_at"] else "",
                "published_by": row["published_by"],
            }
        )
    return out


def _host() -> str:
    import socket

    return socket.gethostname()


def exports_dir(app: AppContext) -> Path:
    return app.settings.data_dir / "exports"
