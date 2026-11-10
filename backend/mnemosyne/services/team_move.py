"""Meetings this computer recorded for the team it is in, moved to the team's computer (docs/
plans/2026-11-10-team-members-in-the-app.md 1.3 (b), "After the move", M11).

A team recording whose work runs here is an ordinary local meeting marked for the team: its id is
in `<data_dir>/team_moves.json` (`pending`). "Put in the team" adds any local meeting this
computer's user owns the same way. A pending meeting moves once nothing works on it (no
recording, no job: finish, transcribe, summarize, copilot...), it is transcribed (or has no
audio) and the team is reachable: hand-off v2 (api/routes/handoff.py on the host) with the
meeting as library sync's document (`sync_state.export_session`), one Ogg per recording with its
part, read through `storage.crypto.plaintext` (sealed files too), and the notes' images as
`media:<sha>` parts. `handoff_id` is the local meeting id, so a lost answer never makes two.

After the move the host's copy is the only one: the local meeting is deleted once the host
answered with its id, through the same checks as a delete from the window (records rules,
guest notes stopped, files and images removed, a tombstone for library sync), and `moved` maps
the local id to the host's for the window. Refused by the records rules here: the meeting stays,
the error says why, and a retry asks the host again (it answers the same meeting).

Never queued: a meeting whose audio is elsewhere (`origin`), one shared with this person
(`shared_from`), one this computer's user does not own, or any while not in a team. Only queued
meetings are ever sent. Retries follow satellite.py's BACKOFF; waiting for the work to end or for
the team to be joined again is no attempt.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import logging
import os
import time
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

import httpx

from .satellite import BACKOFF

if TYPE_CHECKING:
    from ..api.context import AppContext
    from ..models.session import Session

logger = logging.getLogger(__name__)

IDLE_WAKE = 30.0  # waiting meetings are looked at again this often (a transcription ends)
MOVED_KEPT = 50  # moved meetings remembered for the window
SAMPLE_RATE = 48000  # what the hand-off is told for an Ogg (informational: it is decoded there)
SOURCES = ("mic", "system")


class MoveError(Exception):
    """A move that did not happen. `status`: the host's HTTP status, or ours (404 the meeting is
    gone here, 409 refused here), None when the team was not reached. `waiting`: nothing went
    wrong, the meeting or the team is not ready yet (no attempt counted)."""

    def __init__(self, status: int | None, detail: str, waiting: bool = False):
        super().__init__(detail)
        self.status = status
        self.detail = detail
        self.waiting = waiting


@dataclass
class PendingMove:
    session_id: str
    added_at: str
    attempts: int = 0
    last_error: str = ""
    next_at: float = 0.0  # epoch seconds of the next try (0: now)
    host_session_id: str = ""  # the host answered; the local copy is not deleted yet


@dataclass
class MovedMeeting:
    session_id: str  # the local id it had
    host_session_id: str
    name: str
    at: str


def _now_iso() -> str:
    return datetime.fromtimestamp(time.time(), UTC).isoformat()


class MoveStore:
    """`team_moves.json`, written atomically. No secrets in it."""

    def __init__(self, path: Path):
        self.path = path
        self.pending: dict[str, PendingMove] = {}
        self.moved: list[MovedMeeting] = []
        self._load()

    def _load(self) -> None:
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return
        except (OSError, ValueError):
            logger.warning("Could not read %s: no meetings wait to move", self.path, exc_info=True)
            return
        fields = set(PendingMove.__dataclass_fields__)
        for item in raw.get("pending") or []:
            try:
                p = PendingMove(**{k: v for k, v in item.items() if k in fields})
            except TypeError:
                continue
            self.pending[p.session_id] = p
        moved_fields = set(MovedMeeting.__dataclass_fields__)
        for item in raw.get("moved") or []:
            with contextlib.suppress(TypeError):
                self.moved.append(
                    MovedMeeting(**{k: v for k, v in item.items() if k in moved_fields})
                )

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        body = {
            "pending": [asdict(p) for p in self.pending.values()],
            "moved": [asdict(m) for m in self.moved[-MOVED_KEPT:]],
        }
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(body, f, indent=2)
            f.flush()
            os.fsync(f.fileno())
        tmp.replace(self.path)


class TeamMoves:
    def __init__(self, app: AppContext):
        self.app = app
        self.store = MoveStore(app.settings.data_dir / "team_moves.json")
        self._moving: set[str] = set()
        self._worker: asyncio.Task | None = None
        self._wake: asyncio.Event | None = None

    # -- rules

    def _team(self):
        tm = self.app.team_member
        return tm if tm is not None and tm.joined else None

    def not_joined(self) -> str | None:
        """Why nothing can be put in a team now (not in one, signed out), or None."""
        tm = self._team()
        if tm is None:
            return "This computer is not in a team"
        if tm.signed_out:
            return f"You were signed out of {tm.membership.host_name}; ask for a new invite"
        return None

    def refusal(self, session: Session) -> str | None:
        """Why this meeting may never go to the team from here, or None (M11)."""
        if session.shared_from:
            return "This meeting was shared with you: it is not yours to put in the team"
        if session.origin:
            return "This meeting was recorded on another computer: its audio is not here"
        if session.owner_id not in ("", self.app.settings.team_owner_id or ""):
            return "This meeting is not yours"
        return None

    def waiting_for(self, session: Session) -> str | None:
        """What the meeting waits for before it can move, or None when it is ready."""
        sid = session.id
        if sid in self.app.active_recordings or sid in self.app.starting:
            return "Waiting for the recording to stop"
        jobs = self.app.jobs.list(session_id=sid, active_only=True)
        if jobs:
            return f"Waiting for {jobs[0].kind} to finish"
        if self._has_audio(session) and not session.transcript:
            return "Waiting for the transcription"
        return None

    @staticmethod
    def _has_audio(session: Session) -> bool:
        paths = [r.path for r in session.recordings] + [session.audio_file or ""]
        return any(p and Path(p).exists() for p in paths)

    def _session(self, session_id: str) -> Session | None:
        return self.app.sessions.get_session(session_id)

    # -- the list

    def queue(self, session_id: str) -> PendingMove:
        """Mark a meeting for the team. MoveError 404 (no such meeting) or 409 (refused)."""
        reason = self.not_joined()
        if reason:
            raise MoveError(409, reason)
        session = self._session(session_id)
        if session is None:
            raise MoveError(404, "Session not found")
        reason = self.refusal(session)
        if reason:
            raise MoveError(409, reason)
        p = self.store.pending.get(session_id)
        if p is None:
            p = PendingMove(session_id=session_id, added_at=_now_iso())
            self.store.pending[session_id] = p
            self.store.save()
            logger.info("Meeting %s will move to the team", session_id)
        self.kick()
        return p

    def unqueue(self, session_id: str) -> bool:
        if session_id in self._moving:
            raise MoveError(409, "The meeting is being moved right now")
        if self.store.pending.pop(session_id, None) is None:
            return False
        self.store.save()
        logger.info("Meeting %s no longer moves to the team", session_id)
        return True

    def consent(self, session_id: str, consent: str) -> PendingMove:
        """How the people in a waiting meeting agreed to be recorded, logged here as a recording
        start logs it (the host asks for it), then tried again at once."""
        from ..api.routes.audio import log_consent

        p = self.store.pending.get(session_id)
        if p is None:
            raise MoveError(404, "This meeting is not waiting to move to the team")
        if self._session(session_id) is None:
            raise MoveError(404, "Session not found")
        log_consent(self.app, session_id, consent)
        p.attempts, p.next_at, p.last_error = 0, 0.0, ""
        self.store.save()
        self.kick()
        return p

    def pending(self) -> list[tuple[PendingMove, Session | None]]:
        return [
            (p, self._session(p.session_id))
            for p in sorted(self.store.pending.values(), key=lambda p: p.added_at)
        ]

    def moved(self) -> list[MovedMeeting]:
        return list(reversed(self.store.moved[-MOVED_KEPT:]))

    # -- one move

    async def move(self, session_id: str) -> str:
        """Move one queued meeting now; the host's meeting id. MoveError says why not."""
        if session_id not in self.store.pending:
            raise MoveError(404, "This meeting is not waiting to move to the team")
        if session_id in self._moving:
            raise MoveError(409, "The meeting is being moved right now", waiting=True)
        self._moving.add(session_id)
        try:
            return await self._move(session_id)
        finally:
            self._moving.discard(session_id)

    async def _move(self, session_id: str) -> str:
        from .sync_state import export_session

        app = self.app
        reason = self.not_joined()
        if reason:
            raise MoveError(409, reason, waiting=True)
        session = self._session(session_id)
        if session is None:
            raise MoveError(404, "Session not found")
        reason = self.refusal(session)
        if reason:
            raise MoveError(409, reason)
        wait = self.waiting_for(session)
        if wait:
            raise MoveError(409, wait, waiting=True)
        tm = self._team()
        name = tm.membership.host_name
        if not await tm.reachable():
            raise MoveError(None, f"{name} is not reachable")
        dest = tm.destination()
        if dest is None:
            raise MoveError(None, f"{name} is not reachable")
        doc = export_session(
            app.repo, session_id, app.sync_peers.self_info().id, app.settings.team_owner_id or ""
        )
        if doc is None:
            raise MoveError(404, "Session not found")
        # Publications (their link keys and tokens) and shares with this person's contacts are
        # theirs, not the team's: they never leave (the host would drop them anyway).
        doc = doc.model_copy(update={"publications": [], "peer_shares": []})
        sources = self._sources(session)
        images = self._images(session_id, doc)
        meta = {
            "handoff_id": session_id,
            "title": session.name,
            "started_at": session.created_at.astimezone().isoformat(),
            "ended_at": None,
            "notes": "",
            "sources": [
                {
                    "source": source,
                    "part": part,
                    "offset": offset,
                    "sample_rate": SAMPLE_RATE,
                    "file": f"{source}{part}",
                    "label": label[:80],
                }
                for source, part, offset, _, label in sources
            ],
            "transcribed": True,
            "session": json.loads(doc.model_dump_json()),
        }
        answer = await self._send(tm, dest, meta, sources, images)
        host_id = str(answer.get("session_id") or "")
        if not host_id:
            raise MoveError(502, f"{name} did not say which meeting it made")
        p = self.store.pending.get(session_id)
        if p is not None and p.host_session_id != host_id:
            p.host_session_id = host_id
            self.store.save()
        await self._delete_local(session, name)
        self.store.pending.pop(session_id, None)
        self.store.moved.append(
            MovedMeeting(
                session_id=session_id, host_session_id=host_id, name=session.name, at=_now_iso()
            )
        )
        self.store.save()
        logger.info("Meeting %s moved to %s as %s; deleted here", session_id, name, host_id)
        return host_id

    def _sources(self, session: Session) -> list[tuple[str, int, float, str, str]]:
        """(source, part, offset, path, label) per file to send. Each recording as it is when the
        meeting has only microphone and call audio, one of each per part (the host joins them
        again into the same timeline); else (an import, two microphones in one part) the joined
        audio as one part. Files that are gone (audio deleted) are left out."""
        rows = [r for r in session.recordings if r.path and Path(r.path).exists()]
        keys = [(r.source, r.part) for r in rows]
        if rows and all(r.source in SOURCES for r in rows) and len(set(keys)) == len(keys):
            return [(r.source, r.part, r.offset, r.path, r.device_name) for r in rows]
        if session.audio_file and Path(session.audio_file).exists():
            if len({r.part for r in session.recordings}) > 1:
                logger.warning("Meeting %s: its parts go to the team as one recording", session.id)
            return [("mic", 0, 0.0, session.audio_file, "Meeting audio")]
        return []

    def _images(self, session_id: str, doc) -> dict[str, bytes]:
        """The notes' images the document lists, checked here as the host checks them; one that
        is missing or unreadable is left out (its row still goes, as library sync's would)."""
        from . import notes_media

        out: dict[str, bytes] = {}
        for m in doc.media or []:
            try:
                data = notes_media.read(self.app, session_id, m.sha256)
            except Exception:  # noqa: BLE001 - gone, sealed with a lost key, not a file
                logger.info("Meeting %s: image %s not sent", session_id, m.sha256[:12])
                continue
            if hashlib.sha256(data).hexdigest() != m.sha256 or notes_media.sniff(data) != m.ext:
                logger.warning("Meeting %s: image %s is damaged; not sent", session_id, m.sha256)
                continue
            out[m.sha256] = data
        return out

    async def _send(self, tm, dest, meta: dict, sources, images: dict[str, bytes]) -> dict:
        from ..storage.crypto import plaintext_async

        # The host answers once the audio is saved (encoded, mixed, joined): minutes for hours.
        timeout = httpx.Timeout(30.0, read=1800.0, write=600.0)
        async with contextlib.AsyncExitStack() as stack:
            files: list[tuple[str, tuple]] = []
            for source, part, _, path, _ in sources:
                plain = await stack.enter_async_context(plaintext_async(path, self.app.file_key))
                handle = stack.enter_context(open(plain, "rb"))  # noqa: SIM115
                files.append((f"{source}{part}", (f"{source}{part}.ogg", handle, "audio/ogg")))
            for sha, data in images.items():
                files.append((f"media:{sha}", (sha, data, "application/octet-stream")))
            try:
                r = await tm.client().post(
                    f"{dest.url}/api/connections/handoff",
                    headers={"Authorization": f"Bearer {dest.token}"},
                    data={"meta": json.dumps(meta)},
                    files=files,
                    timeout=timeout,
                )
            except httpx.HTTPError as e:
                tm.seen(False)
                raise MoveError(None, f"{dest.name} is not reachable ({type(e).__name__})") from e
        tm.seen(True)
        if r.status_code == 401:
            if await tm.check_signed_out():
                raise MoveError(401, f"You were signed out of {dest.name}", waiting=True)
            raise MoveError(502, f"{dest.name} refused the request")
        if r.status_code >= 400:
            try:
                detail = str(r.json().get("detail") or r.text)
            except ValueError:
                detail = r.text[:300]
            raise MoveError(r.status_code, detail or f"HTTP {r.status_code}")
        try:
            return r.json()
        except ValueError as e:
            raise MoveError(502, f"{dest.name} answered something else") from e

    async def _delete_local(self, session: Session, team: str) -> None:
        """The local copy goes the way a delete from the window goes (records rules, guest notes
        stopped, files and images removed, a tombstone for library sync). A refusal keeps it:
        MoveError 409 with the reason; the next try asks the host again (the same meeting)."""
        from .. import access
        from . import guest_notes
        from .records import check_delete

        sid = session.id
        if sid in self.app.busy_sessions() or sid in self.app.starting:
            raise MoveError(409, f"Moved to {team}; kept here while it is busy", waiting=True)
        try:
            check_delete(self.app, session, "meeting", f"Moved to {team}")
        except (ValueError, access.Forbidden) as e:
            raise MoveError(409, f"Moved to {team}, but kept here: {e}") from e
        try:
            await guest_notes.stop(self.app, sid, by="")
        except Exception:
            logger.warning("Could not stop the guest notes of %s before deleting it", sid)
        self.app.sessions.delete_session(sid)

    # -- the worker

    def start(self) -> None:
        """At startup: only when something waits."""
        if self.store.pending:
            self.kick()

    def kick(self) -> None:
        """Try every waiting meeting now (the team is back, a meeting was added)."""
        for p in self.store.pending.values():
            p.next_at = 0.0
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return  # no loop (a sync test): the next start picks them up
        if self._worker is None or self._worker.done():
            self._wake = asyncio.Event()
            self._worker = asyncio.create_task(self._run())
        if self._wake is not None:
            self._wake.set()

    async def stop(self) -> None:
        worker, self._worker = self._worker, None
        if worker is not None:
            worker.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await worker

    async def _run(self) -> None:
        while True:
            try:
                now = time.time()
                due = sorted(
                    (p for p in self.store.pending.values() if p.next_at <= now),
                    key=lambda p: p.added_at,
                )
                for p in due:
                    await self.attempt(p.session_id)
                if not self.store.pending:
                    return
                if due:
                    continue
                later = [p.next_at - now for p in self.store.pending.values()]
                self._wake.clear()
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(
                        self._wake.wait(), timeout=max(min([IDLE_WAKE, *later]), 0.01)
                    )
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Team moves: the worker failed; trying again")
                await asyncio.sleep(5)

    async def attempt(self, session_id: str) -> str | None:
        """One try for a waiting meeting, its outcome kept on it. The host's id when it moved."""
        p = self.store.pending.get(session_id)
        if p is None:
            return None
        try:
            return await self.move(session_id)
        except MoveError as e:
            if e.status == 404 and self._session(session_id) is None:
                self.store.pending.pop(session_id, None)  # deleted here meanwhile
                self.store.save()
                return None
            p.last_error = e.detail
            if e.waiting:
                p.next_at = time.time() + IDLE_WAKE
            else:
                p.attempts += 1
                p.next_at = time.time() + BACKOFF[min(p.attempts, len(BACKOFF)) - 1]
                logger.info("Meeting %s not moved to the team: %s", session_id, e.detail)
            self.store.save()
            return None
        except Exception as e:  # unreadable audio, a locked key: keep it, say why
            logger.exception("Meeting %s could not be moved to the team", session_id)
            p.last_error = str(e) or type(e).__name__
            p.attempts += 1
            p.next_at = time.time() + BACKOFF[min(p.attempts, len(BACKOFF)) - 1]
            self.store.save()
            return None
