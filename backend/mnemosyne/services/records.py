"""Meeting records a team can rely on, when Mnemosyne is where its meetings are kept.

- Versions: a replaced transcript or summary is kept (storage/sqlite.py keep_version).
- Seals: after a transcription, a summary or an edit, a SHA-256 of the meeting's content and of
  each audio file, chained to the previous seal. `verify` recomputes the chain and compares the
  last seal with what is there now, so a change made outside the app (a row edited in the
  database, a file swapped) shows. Someone able to rewrite the whole database could rewrite the
  chain too: the chain head goes into exports and backups, which live elsewhere.
- Retention: `records_retention_years` after a meeting, deleting it or its audio needs an admin
  and a reason; a legal hold refuses it to everyone. Every deletion is logged (`deletions`),
  and that log outlives the meeting.
- Export: meetings (one, or a date range) as a zip with audio, transcript, summary,
  versions, history, seals and SHA256SUMS.
"""

from __future__ import annotations

import hashlib
import json
import logging
import zipfile
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING

from .. import access
from ..models.base import ApiModel
from ..storage.records import chain_hash, content_hash, content_of

if TYPE_CHECKING:
    from ..api.context import AppContext
    from ..models.session import Session

logger = logging.getLogger(__name__)

_file_hashes: dict[tuple[str, int, float], str] = {}


def _file_sha256(path: Path) -> str:
    st = path.stat()
    key = (str(path), st.st_size, st.st_mtime)
    if key not in _file_hashes:
        h = hashlib.sha256()
        with path.open("rb") as f:
            while chunk := f.read(1 << 20):
                h.update(chunk)
        _file_hashes[key] = h.hexdigest()
    return _file_hashes[key]


def audio_files(session: Session) -> list[Path]:
    if session.origin:  # recorded on another computer: its audio is not here
        return []
    paths = [r.path for r in session.recordings] + [session.audio_file or ""]
    return [Path(p) for p in dict.fromkeys(paths) if p and Path(p).is_file()]


def audio_hashes(session: Session) -> dict[str, str]:
    """SHA-256 of each audio file as stored (encrypted ones as encrypted), by file name."""
    return {p.name: _file_sha256(p) for p in audio_files(session)}


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


def seal(app: AppContext, session_id: str, reason: str) -> dict | None:
    """Chain a seal of the meeting as it is now (blocking: hashes audio; run in a thread).
    Never raises: a record that cannot be sealed must not fail what it records."""
    try:
        session = app.repo.get(session_id)
        if session is None or not (session.transcript or session.summary):
            return None
        return app.repo.add_seal(
            session_id,
            reason,
            content_hash(content_of(session)),
            audio_hashes(session),
            force=reason in EVENT_SEALS,
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


class Verification(ApiModel):
    ok: bool
    seals: int
    last_sealed_at: datetime | None
    chain_head: str  # the last seal's chain hash: write it down to prove the record later
    problems: list[str]


def verify(app: AppContext, session_id: str) -> Verification:
    """Recompute the chain of seals and compare the last one with the meeting as it is now."""
    session = app.repo.get(session_id)
    seals = app.repo.seals(session_id)
    problems: list[str] = []
    prev = ""
    for i, s in enumerate(seals, 1):
        if s["prev"] != prev:
            problems.append(f"Seal {i} does not follow seal {i - 1}")
        if chain_hash(s["prev"], s["content_hash"], s["audio"], s["at"], s["reason"]) != s["chain"]:
            problems.append(f"Seal {i} ({s['at'][:16]}) was changed after it was made")
        prev = s["chain"]
    if session is not None and seals:
        last = seals[-1]
        if content_hash(content_of(session)) != last["content_hash"]:
            problems.append("The transcript or summary differs from the last seal")
        if session.origin:  # the audio files are on the computer that recorded it
            return Verification(
                ok=not problems,
                seals=len(seals),
                last_sealed_at=last["at"],
                chain_head=last["chain"],
                problems=problems,
            )
        now = audio_hashes(session)
        for name, digest in last["audio"].items():
            if name not in now:
                problems.append(f"Audio file {name} is missing")
            elif now[name] != digest:
                problems.append(f"Audio file {name} differs from the last seal")
        for name in now.keys() - last["audio"].keys():
            problems.append(f"Audio file {name} was added after the last seal")
    return Verification(
        ok=not problems,
        seals=len(seals),
        last_sealed_at=seals[-1]["at"] if seals else None,
        chain_head=seals[-1]["chain"] if seals else "",
        problems=problems,
    )


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
                     host, the room's id, when and by whom it was started, ended and stopped.
                     Never the link, its key or the host token
  audio/             the recordings (each channel and the mix), unencrypted

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
        if e["kind"] in ("published", "publication_updated", "unpublished", "publication_expired")
    ]
    out = []
    for row in rows:
        versions, ended = [], {}
        for e in events:
            d = e["detail"]
            if d.get("publication") != row["id"]:
                continue
            if e["kind"] in ("published", "publication_updated"):
                versions.append(
                    {
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
