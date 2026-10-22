"""Live shared notes (phase 6 of docs/plans/2026-10-12-hashseq-collaboration.md).

Every client runs the CRDT (hashseq's wasm build); the backend cannot decode it. It is an opaque
relay and an append log: a client's binary frame (0xDE delta or 0xAF artifact) is checked by its
tag and size, appended to `shared_notes_ops` with who sent it, and relayed to the meeting's other
connections (never echoed). The stateless sidecar (`mnemosyne-link notes doc`) folds the log into
a snapshot and renders it: `Session.notes` = the markdown, written through the repository's
normal update path (library sync's `_touch`, the search index), with a `notes` bus event.

Folds: 2 s after the last frame, every 30 s while frames keep coming, when the last connection
leaves, and on demand (`fold_now` / `fold_blocking`: before a summary or an Obsidian export).

The other writers (the notes route, combining meetings) go through `set_markdown`: the sidecar's
`set_text` diffs the document against the new markdown and gives frames, which are logged and
broadcast like a client's. A meeting with notes but no document is seeded that way (author
"seed") on its first join or write, only on the machine that recorded it.

Without a sidecar (`available()` False) nothing here runs: joins hear `unavailable`, and the
notes route writes `Session.notes` as before.

The WebSocket itself is api/notes_ws.py; the protocol is in docs/api-reference.md.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import logging
import os
import subprocess
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime
from typing import TYPE_CHECKING, Any

from .. import access

if TYPE_CHECKING:
    from ..api.context import AppContext

logger = logging.getLogger(__name__)

DELTA, ARTIFACT = 0xDE, 0xAF
MAX_FRAME = 2 * 1024 * 1024  # ~40 B per typed character: a 6,500-character paste is 256 KB
FOLD_DELAY = 2.0  # seconds after the last frame
FOLD_EVERY = 30.0  # at the latest, while frames keep coming
LAST_EDIT_EVERY = 15.0  # a `last_edit` message at most this often for the same person
DOC_TIMEOUT = 60.0
SEED_AUTHOR, API_AUTHOR = "seed", "api"

# Close codes for a refused frame (a read-only connection: 4403, as in HTTP; a deleted
# meeting: 4404).
CLOSE_READ_ONLY, CLOSE_BAD_FRAME, CLOSE_TOO_BIG, CLOSE_GONE = 4403, 4400, 1009, 4404


class NotesDocError(Exception):
    """The sidecar is missing, failed or answered something unreadable."""


class NotesWriteFailed(Exception):
    """The meeting has a notes document and writing to it failed. The caller must not write
    `Session.notes` around it: the next fold would put the document's text back."""


def notes_origin(session_id: str) -> str:
    """The id of the meeting's notes object, the same on every client and machine."""
    return hashlib.sha256(f"mnemosyne/notes/{session_id}".encode()).hexdigest()


# ---- the sidecar ---------------------------------------------------------------------------


def _b64(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


def _unb64(text: str) -> bytes:
    return base64.b64decode(text, validate=True)


def _run_doc(binary: str, request: dict) -> dict:
    """One `<binary> notes doc` run. Raises NotesDocError."""
    try:
        done = subprocess.run(
            [binary, "notes", "doc"],
            input=json.dumps(request),
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=DOC_TIMEOUT,
        )
    except (OSError, subprocess.SubprocessError) as e:
        raise NotesDocError(f"could not run {binary}: {e}") from e
    if done.returncode != 0:
        raise NotesDocError(f"{binary} notes doc exited {done.returncode}: {done.stderr.strip()}")
    try:
        reply: Any = json.loads(done.stdout.strip().splitlines()[-1])
        if not isinstance(reply, dict) or not isinstance(reply.get("snapshot"), str):
            raise ValueError("no snapshot")
        return reply
    except (IndexError, ValueError) as e:
        raise NotesDocError(f"unreadable reply from {binary} notes doc: {e}") from e


def _binary() -> str | None:
    from .link import find_binary

    return find_binary()


_probed: dict[tuple[str, float], bool] = {}
_warned: set[str] = set()


def available() -> bool:
    """A sidecar that answers `notes doc` (an older mnemosyne-link has no such command). Runs
    it once per binary (by path and mtime), so call it off the event loop the first time."""
    binary = _binary()
    if binary is None:
        return False
    try:
        key = (binary, os.stat(binary).st_mtime)
    except OSError:
        return False
    if key not in _probed:
        try:
            _run_doc(binary, {"op": "fold", "notes": "0" * 64, "snapshot": None, "frames": []})
            _probed[key] = True
        except NotesDocError as e:
            logger.warning("Live shared notes are off: %s", e)
            _probed[key] = False
    return _probed[key]


def _warn_once(key: str, message: str, *args) -> None:
    if key not in _warned:
        _warned.add(key)
        logger.warning(message, *args)


# ---- rooms ---------------------------------------------------------------------------------


@dataclass(eq=False)
class Member:
    """One connection to a meeting's notes. Everything it is sent goes through `out`, in order:
    the hello and catch-up first, live traffic after."""

    ws: Any
    id: str
    name: str
    can_edit: bool
    typing: bool = False
    seen_seq: int = 0  # the frames up to this one were in its catch-up
    out: asyncio.Queue = field(default_factory=asyncio.Queue)
    sender: asyncio.Task | None = None

    def send(self, kind: str, data: Any) -> None:
        self.out.put_nowait((kind, data))


@dataclass
class Room:
    session_id: str
    members: list[Member] = field(default_factory=list)
    fold_timer: asyncio.TimerHandle | None = None
    dirty_since: float | None = None  # loop time of the first frame since the last fold
    last_edit: tuple[str, float] = ("", 0.0)  # who, and when it was last announced

    def broadcast(self, kind: str, data: Any, skip: Member | None = None) -> None:
        for m in self.members:
            if m is not skip:
                m.send(kind, data)


class Hub:
    """The open meetings' rooms (on the event loop) and a lock per meeting for folds and
    writes (in threads)."""

    def __init__(self) -> None:
        self.rooms: dict[str, Room] = {}
        self.loop: asyncio.AbstractEventLoop | None = None
        self.tasks: set[asyncio.Task] = set()
        self.closed = False  # the backend is stopping: no new folds
        self._locks: dict[str, threading.RLock] = {}
        self._guard = threading.Lock()

    def lock(self, session_id: str) -> threading.RLock:
        with self._guard:
            return self._locks.setdefault(session_id, threading.RLock())


_hub_guard = threading.Lock()


def hub(app: AppContext) -> Hub:
    if app.notes_hub is None:
        with _hub_guard:
            if app.notes_hub is None:
                app.notes_hub = Hub()
    return app.notes_hub


def _remember_loop(app: AppContext) -> None:
    hub(app).loop = asyncio.get_running_loop()


def _on_loop(app: AppContext, fn, *args) -> None:
    """Run `fn(*args)` on the event loop: now when this is it, else soon (from a thread)."""
    loop = hub(app).loop
    try:
        running = asyncio.get_running_loop()
    except RuntimeError:
        running = None
    if loop is None or running is loop or loop.is_closed():
        fn(*args)
    else:
        loop.call_soon_threadsafe(fn, *args)


@contextmanager
def unrestricted():
    """Internal writes: who may edit was decided when the frame was accepted."""
    token = access.current.set(None)
    try:
        yield
    finally:
        access.current.reset(token)


def may_edit(app: AppContext, session_id: str) -> bool:
    """The caller may change this (readable) meeting's notes (access.can_edit_notes)."""
    owner = app.repo.owner_of(session_id) or ""
    shared = {s["user_id"] for s in app.repo.shares(session_id)}
    return access.can_edit_notes(owner, shared)


# ---- folding and writing (blocking: threads) -----------------------------------------------


def _state(app: AppContext, session_id: str) -> tuple[str | None, list[tuple[int, bytes]], int]:
    """(snapshot as base64 or None, unfolded frames, the last sequence number known)."""
    doc, ops = app.repo.notes_catchup(session_id)
    snapshot = doc["snapshot"] if doc else None
    last = ops[-1][0] if ops else (doc["folded_seq"] if doc else 0)
    return (_b64(snapshot) if snapshot is not None else None), ops, last


def marks_of(reply: dict) -> tuple[str | None, dict | None]:
    """(plain text, comment ranges) of a sidecar answer, as stored beside the snapshot: each
    `comment:<id>` mark's [start, end) in code points of `plain`, None once its text is gone.
    (None, None) from a sidecar that does not report them."""
    plain, comments = reply.get("plain"), reply.get("comments")
    if not isinstance(plain, str) or not isinstance(comments, dict):
        return None, None
    ranges: dict[str, list[int] | None] = {}
    for key, value in comments.items():
        ok = (
            isinstance(value, list)
            and len(value) == 2
            and all(isinstance(x, int) and not isinstance(x, bool) for x in value)
        )
        ranges[str(key)] = [value[0], value[1]] if ok else None
    return plain, ranges


def _write_notes(app: AppContext, session_id: str, markdown: str) -> bool:
    """Session.notes = the document's markdown, unless it is that already."""
    with unrestricted():
        source = app.repo.notes_source(session_id)
        if source is None or source[0] == markdown:
            return False
        app.repo.update_fields(session_id, notes=markdown)
    _on_loop(app, app.bus.publish, {"type": "notes", "session_id": session_id})
    return True


def fold_blocking(app: AppContext, session_id: str) -> bool:
    """Fold the unfolded frames into the snapshot and write `Session.notes` when its markdown
    changed. First, text written around the document catches up (`_catch_up_locked`). Cheap
    when there is nothing to do. True when the notes were written. Never raises: a failing
    sidecar is logged and the frames stay unfolded."""
    drift = app.repo.notes_drift(session_id) is not None
    if not drift and not app.repo.has_unfolded_notes(session_id):
        return False
    binary = _binary()
    if binary is None or (drift and not available()):
        return False
    with hub(app).lock(session_id):
        try:
            caught, wrote = _catch_up_locked(app, session_id, binary)
        except NotesDocError as e:
            _warn_once(f"catch:{session_id}", "Notes of %s cannot catch up: %s", session_id, e)
            caught, wrote = [], False
        if caught:
            _on_loop(app, _deliver, app, session_id, caught, None)
            _on_loop(app, _announce_edit, app, session_id, API_AUTHOR, None)
        return _fold_locked(app, session_id, binary) or wrote


def _fold_locked(app: AppContext, session_id: str, binary: str) -> bool:
    """The fold itself (the meeting's lock held). Never raises for a failing sidecar."""
    snapshot, ops, _ = _state(app, session_id)
    if not ops:
        return False
    request = {
        "op": "fold",
        "notes": notes_origin(session_id),
        "snapshot": snapshot,
        "frames": [_b64(f) for _, f in ops],
    }
    try:
        reply = _run_doc(binary, request)
        folded = _unb64(reply["snapshot"])
        markdown = str(reply["markdown"])
    except (NotesDocError, KeyError, ValueError) as e:
        _warn_once(f"fold:{session_id}", "Could not fold the notes of %s: %s", session_id, e)
        return False
    try:
        app.repo.store_notes_fold(session_id, folded, ops[-1][0], markdown, *marks_of(reply))
    except Exception:  # sqlite3's or sqlcipher3's IntegrityError: deleted meanwhile?
        if app.repo.notes_source(session_id) is not None:
            raise
        return False
    return _write_notes(app, session_id, markdown)


def _catch_up_locked(
    app: AppContext, session_id: str, binary: str
) -> tuple[list[tuple[int, bytes]], bool]:
    """`Session.notes` was written around the document (while the sidecar was missing, or by
    library sync's text merge): it differs from the markdown of the last fold. The document
    takes that text first, as an edit by "api" against the folded state, so frames not
    folded yet merge with it instead of being undone; then everything is folded, so the fold
    renders the newer text rather than overwriting it. Returns (appended frames, notes
    written). Raises NotesDocError."""
    text = app.repo.notes_drift(session_id)
    if text is None:
        return [], False
    doc = app.repo.notes_doc(session_id)
    snapshot = doc["snapshot"] if doc else None
    reply = _run_doc(
        binary,
        {
            "op": "set_text",
            "notes": notes_origin(session_id),
            "snapshot": _b64(snapshot) if snapshot is not None else None,
            "frames": [],
            "markdown": text,
        },
    )
    try:
        frames = [_unb64(f) for f in reply["frames"]]
        rendered = str(reply["markdown"])
    except (KeyError, TypeError, ValueError) as e:
        raise NotesDocError(f"unreadable set_text reply: {e}") from e
    if not frames:  # the same document, spelled otherwise: the notes take its spelling
        app.repo.set_notes_markdown(session_id, rendered)
        return [], _write_notes(app, session_id, rendered)
    seqs = app.repo.append_notes_ops(session_id, frames, "", API_AUTHOR, True)
    # Caught up even if the fold below fails: a second catch-up would insert the text again.
    app.repo.set_notes_markdown(session_id, text)
    wrote = _fold_locked(app, session_id, binary)
    return list(zip(seqs, frames, strict=True)), wrote


def _set_text_blocking(
    app: AppContext, session_id: str, markdown: str, author_id: str, author: str, last_edit: bool
) -> list[tuple[int, bytes]]:
    """Make the document say `markdown`: the sidecar's frames, appended to the log, folded.
    Returns the appended (seq, frame)s for broadcasting. Raises NotesDocError."""
    binary = _binary()
    if binary is None:
        raise NotesDocError("mnemosyne-link not found")
    with hub(app).lock(session_id):
        snapshot, ops, last = _state(app, session_id)
        reply = _run_doc(
            binary,
            {
                "op": "set_text",
                "notes": notes_origin(session_id),
                "snapshot": snapshot,
                "frames": [_b64(f) for _, f in ops],
                "markdown": markdown,
            },
        )
        try:
            frames = [_unb64(f) for f in reply["frames"]]
            folded = _unb64(reply["snapshot"])
            rendered = str(reply["markdown"])
        except (KeyError, TypeError, ValueError) as e:
            raise NotesDocError(f"unreadable set_text reply: {e}") from e
        seqs = app.repo.append_notes_ops(session_id, frames, author_id, author, last_edit)
        if not seqs or seqs[0] == last + 1:
            # Nothing came in meanwhile: the reply's snapshot is the whole log.
            app.repo.store_notes_fold(
                session_id, folded, seqs[-1] if seqs else last, rendered, *marks_of(reply)
            )
            _write_notes(app, session_id, rendered)
        else:
            _fold_locked(app, session_id, binary)
        return list(zip(seqs, frames, strict=True))


def mark_comment_blocking(
    app: AppContext,
    session_id: str,
    comment_id: str,
    first: str,
    last_id: str,
    author_id: str,
    author: str,
) -> list[tuple[int, bytes]]:
    """Put the mark `comment:<comment_id>` on the notes from element `first` to element `last_id`
    (hex, the editor's `seqIdAt` ids), by the sidecar: for a reader, whose editor cannot author
    it. The frames it returns are appended to the log as `author`, folded (ranges stored), and
    returned for broadcasting. Raises NotesDocError (an unknown or deleted element, no sidecar,
    a failing run): the comment stays, detached."""
    binary = _binary()
    if binary is None:
        raise NotesDocError("mnemosyne-link not found")
    with hub(app).lock(session_id):
        snapshot, ops, last = _state(app, session_id)
        reply = _run_doc(
            binary,
            {
                "op": "mark_comment",
                "notes": notes_origin(session_id),
                "snapshot": snapshot,
                "frames": [_b64(f) for _, f in ops],
                "first": first,
                "last": last_id,
                "comment": comment_id,
            },
        )
        try:
            frames = [_unb64(f) for f in reply["frames"]]
            folded = _unb64(reply["snapshot"])
            rendered = str(reply["markdown"])
        except (KeyError, TypeError, ValueError) as e:
            raise NotesDocError(f"unreadable mark_comment reply: {e}") from e
        seqs = app.repo.append_notes_ops(session_id, frames, author_id, author, False)
        if not seqs:
            return []
        if seqs[0] == last + 1:
            # Nothing came in meanwhile: the reply's snapshot is the whole log.
            app.repo.store_notes_fold(session_id, folded, seqs[-1], rendered, *marks_of(reply))
            _write_notes(app, session_id, rendered)
        else:
            _fold_locked(app, session_id, binary)
        return list(zip(seqs, frames, strict=True))


def seed_blocking(app: AppContext, session_id: str) -> list[tuple[int, bytes]]:
    """A meeting with notes but no document gets one, from its notes (author "seed"), when it
    was recorded on this machine. Returns the appended frames. Raises NotesDocError."""
    with hub(app).lock(session_id):
        if app.repo.has_notes_doc(session_id):
            return []
        source = app.repo.notes_source(session_id)
        if source is None or not source[0].strip():
            return []
        if source[1]:
            # Recorded on another machine (`origin`): its document arrives with the meeting by
            # library sync (SyncSession.notes_doc), which stores it with store_notes_fold.
            # Seeding here too would give two documents with the same text, doubled on merge.
            return []
        return _set_text_blocking(app, session_id, source[0], "", SEED_AUTHOR, False)


def prepare_blocking(app: AppContext, session_id: str) -> list[tuple[int, bytes]]:
    """Before a join: a meeting without a document is seeded, one with text written around
    its document catches up. Returns the appended frames. Raises NotesDocError."""
    binary = _binary()
    if binary is None:
        raise NotesDocError("mnemosyne-link not found")
    with hub(app).lock(session_id):
        seeded = seed_blocking(app, session_id)
        if seeded:
            return seeded
        return _catch_up_locked(app, session_id, binary)[0]


def needs_doc_from_sync(app: AppContext, session_id: str) -> bool:
    """Notes text, no document, and recorded elsewhere: wait for library sync to bring it."""
    source = app.repo.notes_source(session_id)
    return bool(
        source and source[1] and source[0].strip() and not app.repo.has_notes_doc(session_id)
    )


def set_markdown_blocking(
    app: AppContext, session_id: str, markdown: str, author_id: str = "", author: str = API_AUTHOR
) -> bool:
    """The notes written whole (the notes route, combining meetings): through the document,
    broadcast to the meeting's connections. Callable from any thread.

    False when there is no document to go through (no sidecar, a meeting recorded elsewhere
    whose document has not come yet, or none could be made): the caller writes `Session.notes`
    itself, as before. Raises NotesWriteFailed when the meeting has a document and the write
    failed: then nothing may be written around it."""
    if not available() or needs_doc_from_sync(app, session_id):
        return False
    try:
        seeded = seed_blocking(app, session_id)
        frames = _set_text_blocking(app, session_id, markdown, author_id, author, True)
    except NotesDocError as e:
        logger.warning("Could not write the notes of %s: %s", session_id, e)
        if app.repo.has_notes_doc(session_id):
            raise NotesWriteFailed(str(e)) from e
        return False
    if seeded or frames:
        _on_loop(app, _deliver, app, session_id, seeded + frames, None)
    if frames:
        _on_loop(app, _announce_edit, app, session_id, author, None)
    return True


# ---- on the event loop ---------------------------------------------------------------------


async def fold_now(app: AppContext, session_id: str) -> bool:
    """Fold now (before a summary, an export...): a no-op when nothing is unfolded and the
    notes were not written around the document."""
    _remember_loop(app)
    room = app.notes_hub.rooms.get(session_id) if app.notes_hub else None
    if room is not None and room.fold_timer is not None:
        room.fold_timer.cancel()
        room.fold_timer, room.dirty_since = None, None
    if not app.repo.has_unfolded_notes(session_id) and app.repo.notes_drift(session_id) is None:
        return False
    return await asyncio.to_thread(fold_blocking, app, session_id)


async def set_markdown(
    app: AppContext, session_id: str, markdown: str, author_id: str = "", author: str = API_AUTHOR
) -> bool:
    """set_markdown_blocking, off the event loop."""
    _remember_loop(app)
    return await asyncio.to_thread(
        set_markdown_blocking, app, session_id, markdown, author_id, author
    )


async def mark_comment(
    app: AppContext,
    session_id: str,
    comment_id: str,
    first: str,
    last: str,
    author_id: str = "",
    author: str = "",
) -> bool:
    """mark_comment_blocking, off the event loop, and the new frames to everyone connected (the
    commenter too: the mark arrives like any remote edit). False when it could not be marked
    (logged): the comment is then detached."""
    _remember_loop(app)
    try:
        frames = await asyncio.to_thread(
            mark_comment_blocking, app, session_id, comment_id, first, last, author_id, author
        )
    except NotesDocError as e:
        logger.info("Could not mark comment %s in the notes of %s: %s", comment_id, session_id, e)
        return False
    if frames:
        _deliver(app, session_id, frames, None)
    return bool(frames)


def _deliver(
    app: AppContext, session_id: str, frames: list[tuple[int, bytes]], skip: Member | None
) -> None:
    """Frames already in the log to the room's members that did not get them in their
    catch-up."""
    room = hub(app).rooms.get(session_id)
    if room is None:
        return
    for m in room.members:
        if m is skip:
            continue
        for seq, frame in frames:
            if seq > m.seen_seq:
                m.send("bytes", frame)


def _announce_edit(app: AppContext, session_id: str, by: str, skip: Member | None) -> None:
    room = hub(app).rooms.get(session_id)
    if room is None:
        return
    now = time.monotonic()
    if by == room.last_edit[0] and now - room.last_edit[1] < LAST_EDIT_EVERY:
        return
    room.last_edit = (by, now)
    room.broadcast(
        "json", {"type": "last_edit", "by": by, "at": datetime.now().isoformat()}, skip=skip
    )


def _presence(room: Room) -> dict:
    return {
        "type": "presence",
        "present": list(dict.fromkeys(m.name for m in room.members)),
        "typing": list(dict.fromkeys(m.name for m in room.members if m.typing)),
    }


def _schedule_fold(app: AppContext, room: Room) -> None:
    loop = asyncio.get_running_loop()
    now = loop.time()
    if room.dirty_since is None:
        room.dirty_since = now
    delay = max(0.0, min(FOLD_DELAY, room.dirty_since + FOLD_EVERY - now))
    if room.fold_timer is not None:
        room.fold_timer.cancel()
    room.fold_timer = loop.call_later(delay, _start_fold, app, room.session_id)


def _start_fold(app: AppContext, session_id: str) -> None:
    room = hub(app).rooms.get(session_id)
    if room is not None:
        room.fold_timer, room.dirty_since = None, None
    _spawn(app, fold_now(app, session_id))


def _spawn(app: AppContext, coro) -> None:
    h = hub(app)
    if h.closed:
        coro.close()
        return
    task = asyncio.get_running_loop().create_task(coro)
    h.tasks.add(task)
    task.add_done_callback(h.tasks.discard)


async def shutdown(app: AppContext) -> None:
    """The backend stops: folds under way finish, open meetings are folded one last time
    (the frames are stored anyway; this keeps Session.notes current), no new ones start."""
    h = app.notes_hub
    if h is None:
        return
    h.closed = True
    for room in h.rooms.values():
        if room.fold_timer is not None:
            room.fold_timer.cancel()
            room.fold_timer = None
    if h.tasks:
        await asyncio.wait(list(h.tasks), timeout=DOC_TIMEOUT)
    for session_id in list(h.rooms):
        try:
            await asyncio.to_thread(fold_blocking, app, session_id)
        except Exception:
            logger.warning("Could not fold the notes of %s at shutdown", session_id, exc_info=True)


async def _sender(member: Member) -> None:
    try:
        while True:
            kind, data = await member.out.get()
            if kind == "close":
                await member.ws.close(code=data)
                return
            if kind == "json":
                await member.ws.send_json(data)
            else:
                await member.ws.send_bytes(data)
    except asyncio.CancelledError:
        raise
    except Exception:
        logger.debug("Notes socket send failed", exc_info=True)


async def join(
    app: AppContext, session_id: str, ws: Any, you_id: str, you_name: str, can_edit: bool
) -> Member | None:
    """Join a (readable) meeting's notes: None when live notes are unavailable. Otherwise the
    member, with the hello, its snapshot and the unfolded frames queued for it, in that order."""
    _remember_loop(app)
    if not await asyncio.to_thread(available):
        return None
    h = hub(app)
    waiting = await asyncio.to_thread(needs_doc_from_sync, app, session_id)
    if not waiting:
        try:
            added = await asyncio.to_thread(prepare_blocking, app, session_id)
        except NotesDocError as e:
            _warn_once(f"prep:{session_id}", "Could not open the notes of %s: %s", session_id, e)
            return None
        if added:
            _deliver(app, session_id, added, None)
    # Catch-up and joining the room in one step (no await in between): a frame appended now is
    # either in the backlog or relayed to the member, never neither or both.
    doc, ops = app.repo.notes_catchup(session_id)
    snapshot = doc["snapshot"] if doc else None
    room = h.rooms.setdefault(session_id, Room(session_id))
    member = Member(ws=ws, id=you_id, name=you_name, can_edit=can_edit and not waiting)
    member.seen_seq = max(ops[-1][0] if ops else 0, doc["folded_seq"] if doc else 0)
    room.members.append(member)
    last = (
        {"by": doc["last_by"], "at": doc["last_at"]}
        if doc and doc["last_by"] and doc["last_at"]
        else None
    )
    hello = {
        "type": "hello",
        "notes": notes_origin(session_id),
        "you": {"id": you_id, "name": you_name},
        "read_only": not member.can_edit,
        "present": _presence(room)["present"],
        "last_edit": last,
        "snapshot": snapshot is not None,
        "backlog": len(ops),
    }
    if waiting:
        hello["waiting_for_sync"] = True
    member.send("json", hello)
    if snapshot is not None:
        member.send("bytes", bytes(snapshot))
    for _, frame in ops:
        member.send("bytes", frame)
    member.sender = asyncio.get_running_loop().create_task(_sender(member))
    room.broadcast("json", _presence(room), skip=member)
    return member


def receive_frame(app: AppContext, session_id: str, member: Member, frame: bytes) -> int | None:
    """A frame from a member: logged and relayed. None, or the close code it is refused with."""
    if not member.can_edit:
        return CLOSE_READ_ONLY
    if not frame or frame[0] not in (DELTA, ARTIFACT):
        return CLOSE_BAD_FRAME
    if len(frame) > MAX_FRAME:
        return CLOSE_TOO_BIG
    room = hub(app).rooms.get(session_id)
    if room is None:
        return CLOSE_BAD_FRAME
    edit = frame[0] == DELTA
    try:
        app.repo.append_notes_ops(session_id, [frame], member.id, member.name, last_edit=edit)
    except Exception:  # sqlite3's or sqlcipher3's IntegrityError: the meeting was deleted?
        if app.repo.notes_source(session_id) is not None:
            raise
        return CLOSE_GONE
    room.broadcast("bytes", frame, skip=member)
    if edit:
        _announce_edit(app, session_id, member.name, member)
    _schedule_fold(app, room)
    return None


def receive_text(app: AppContext, session_id: str, member: Member, text: str) -> None:
    """A JSON message from a member: `{"type":"presence","typing":bool}` or a ping."""
    try:
        msg = json.loads(text)
    except ValueError:
        return
    if not isinstance(msg, dict):
        return
    if msg.get("type") == "ping":
        member.send("json", {"type": "pong"})
        return
    if msg.get("type") != "presence" or not member.can_edit:
        return
    typing = bool(msg.get("typing"))
    room = hub(app).rooms.get(session_id)
    if room is None or member.typing == typing:
        return
    member.typing = typing
    room.broadcast("json", _presence(room), skip=member)


async def leave(
    app: AppContext, session_id: str, member: Member, close_code: int | None = None
) -> None:
    """A member is gone (or is sent away with `close_code`, after what it was sent). The last
    one out folds the notes."""
    if member.sender is not None:
        if close_code is not None:
            member.send("close", close_code)
            try:
                await asyncio.wait_for(member.sender, timeout=5)
            except (TimeoutError, asyncio.CancelledError, Exception):
                member.sender.cancel()
        else:
            member.sender.cancel()
    room = hub(app).rooms.get(session_id)
    if room is None or member not in room.members:
        return
    room.members.remove(member)
    if room.members:
        room.broadcast("json", _presence(room))
        return
    if room.fold_timer is not None:
        room.fold_timer.cancel()
    del hub(app).rooms[session_id]
    _spawn(app, fold_now(app, session_id))
