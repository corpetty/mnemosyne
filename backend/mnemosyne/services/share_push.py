"""What I did on meetings other people shared with me, sent back to their owners (phase 8,
task 16; C9, C10 in docs/plans/2026-10-23-phase-8-tasks.md).

Three kinds of work travel, as signed `Contribution` records (services/shares.py) POSTed to the
owner's `/api/share/contributions` with the token this person presents to them:

- comments (create, edit, delete, resolve, reopen) and action-item flags (done, follow): each
  local write on a meeting that is `shared_from` someone adds a row to the `share_outbox` table
  (`enqueue_comment`, `enqueue_item_flag`). The row's id is the record's id and its time the
  record's `at`, so a resend signs the very same record and the hub, which keeps the ids it has
  applied, answers "ok" again.
- typed notes: not stored again. The frames this person typed are in the notes log
  (`shared_notes_ops`, author `p:<me>`); everything after the meeting's cursor (meta
  `share_pushed_seq:<id>`) goes out in chunks, one `notes_frames` record per chunk, whose id is
  derived from its frames so a resend is the same record. The cursor moves only past a chunk the
  hub accepted.

`push` sends everything waiting for one owner in FIFO order, in batches under the hub's limits,
through the machine `SharePuller` would use. A transport failure leaves everything as it was; a
refusal settles the row as rejected with the hub's reason (shown by `GET
/api/sessions/{id}/share-status`). A local write schedules a push for that owner after a short
pause (`SharePusher`), and every pull round starts with one, so the copy it pulls already holds
what was accepted. The pulled copy replaces comments and the action items whole, so
`reapply_pending` puts what the owner has not accepted yet back on it.

Never fails a local write: scheduling swallows its errors, the row stays for the next round.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import hashlib
import logging
import secrets
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from .. import access
from ..models.shared_meeting import ContributionBatch, ContributionResponse
from . import contacts, shared_with_me

if TYPE_CHECKING:
    from ..api.context import AppContext

logger = logging.getLogger(__name__)

DEBOUNCE = 1.0  # seconds a write waits for the next before it is sent
CHUNK_BYTES = 512 * 1024  # one notes_frames record holds at most this many bytes of frames
CHUNK_FRAMES = 100
EXCERPT = 80


# ---- enqueueing (called after a local write on a meeting shared with me) --------------------


def _owner_of(app: AppContext, session_id: str) -> str:
    """The owner's person key when my write on this meeting has to travel, else "": the meeting
    is shared with me and this is not the app's own (internal) write."""
    if access.is_internal() or app.settings.team_mode:
        return ""
    return app.repo.shared_from(session_id)


def _announce(app: AppContext, session_id: str) -> None:
    waiting = waiting_for(app, session_id)
    app.bus.publish(
        {
            "type": "share_outbox",
            "session_id": session_id,
            "pending": waiting["comments"] + waiting["ticks"] + int(waiting["notes"]),
        }
    )


def _enqueue(app: AppContext, owner: str, session_id: str, kind: str, payload: dict) -> None:
    try:
        app.repo.outbox_add(
            secrets.token_hex(6), session_id, owner, kind, payload, datetime.now(UTC).isoformat()
        )
        _announce(app, session_id)
        schedule(app, owner)
    except Exception:  # noqa: BLE001 - never fails the local write
        logger.warning("Could not queue %s on %s for its owner", kind, session_id, exc_info=True)


def enqueue_comment(app: AppContext, session_id: str, op: str, payload: dict) -> bool:
    """A comment write on a meeting shared with me: `op` is create|edit|delete|resolve|reopen,
    `payload` has `comment_id` and the op's fields. False when nothing has to travel."""
    owner = _owner_of(app, session_id)
    if not owner:
        return False
    _enqueue(app, owner, session_id, "comment", {"op": op, **payload})
    return True


def enqueue_item_flag(
    app: AppContext, session_id: str, item_id: str, flag: str, value: bool
) -> bool:
    """Ticking (`done`) or starring (`follow`) an action item of a meeting shared with me."""
    owner = _owner_of(app, session_id)
    if not owner or not item_id:  # an item with no id cannot be named to the owner
        return False
    _enqueue(
        app,
        owner,
        session_id,
        "item_flag",
        {"item_id": item_id, "flag": flag, "value": bool(value)},
    )
    return True


def notes_typed(app: AppContext, session_id: str) -> None:
    """Frames were just logged on this meeting's notes. When it is shared with me (and they
    were mine, which `push` reads off the log), a push follows. Thread-safe, never raises."""
    try:
        owner = app.repo.shared_from(session_id)
        if owner and not app.settings.team_mode:
            _announce(app, session_id)
            schedule(app, owner)
    except Exception:  # noqa: BLE001
        logger.debug("Could not schedule the push of typed notes", exc_info=True)


def schedule(app: AppContext, person: str) -> None:
    pusher = getattr(app, "share_push", None)
    if pusher is not None:
        pusher.schedule(person)


# ---- reading what is waiting ------------------------------------------------------------


def _my_pid(app: AppContext) -> str:
    return app.person.pid


def _notes_ops(app: AppContext, session_id: str) -> list[tuple[int, bytes]]:
    """The frames I typed on this meeting that the owner has not accepted, when the notes are
    part of the share."""
    parts = shared_with_me.parts_of(app, session_id)
    if parts is None or not parts.notes:
        return []
    return app.repo.notes_ops_after(session_id, app.repo.pushed_seq(session_id), _my_pid(app))


def waiting_for(app: AppContext, session_id: str) -> dict[str, Any]:
    """{comments: n, ticks: n, notes: bool} still to send for this meeting."""
    counts = app.repo.outbox_counts(session_id)
    return {
        "comments": counts.get("comment", 0),
        "ticks": counts.get("item_flag", 0),
        "notes": bool(_notes_ops(app, session_id)),
    }


# ---- keeping my unaccepted work on the pulled copy -----------------------------------------


def _local_time(iso: str) -> str:
    """A record's UTC time as the naive local time comments are stored with."""
    try:
        return datetime.fromisoformat(iso).astimezone().replace(tzinfo=None).isoformat()
    except ValueError:
        return datetime.now().isoformat()


def reapply_pending(app: AppContext, session_id: str) -> None:
    """The owner's copy of the meeting has just replaced mine (comments and action items whole):
    show again what I did that they have not accepted, in the order I did it. Once accepted it
    arrives from them (comments with the same id, C10). Blocking; never raises."""
    try:
        rows = app.repo.outbox_pending(session_id=session_id)
        if rows:
            _reapply(app, session_id, rows)
    except Exception:  # noqa: BLE001
        logger.warning("Could not show my unsent work on %s again", session_id, exc_info=True)


def _reapply(app: AppContext, session_id: str, rows: list[dict]) -> None:
    from ..models.comment import CommentCreate
    from . import comments

    repo = app.repo
    session = repo.get(session_id)
    if session is None:
        return
    me = contacts.my_principal(app)
    existing = {c.id: c for c in repo.list_comments(session_id)}
    changed_comments = False
    for row in rows:
        p = row["payload"]
        if row["kind"] == "item_flag":
            items = session.summary_data.action_items if session.summary_data else []
            idx = next((i for i, a in enumerate(items) if a.id == p.get("item_id")), None)
            if idx is None:
                continue
            if p.get("flag") == "done":
                repo.set_action_item_done(session_id, idx, bool(p.get("value")))
            elif p.get("flag") == "follow":
                repo.set_action_item_follow(session_id, idx, bool(p.get("value")))
            continue
        op, cid = p.get("op"), p.get("comment_id")
        if not isinstance(cid, str):
            continue
        c = existing.get(cid)
        if op == "create":
            if c is not None:
                continue
            fields = {k: v for k, v in p.items() if k not in ("op", "comment_id")}
            try:
                req = CommentCreate.model_validate(fields)
                text = req.text.strip()
                extra: dict = {}
                if req.parent_id:
                    parent = existing.get(req.parent_id)
                    if parent is None:
                        continue  # its thread is gone at the owner's
                    extra["parent_id"] = parent.id
                else:
                    target, target_id, anchor, quote = comments._anchor(session, req, cid)
                    extra.update(target=target, target_id=target_id, anchor=anchor, quote=quote)
                when = _local_time(row["created_at"])
                repo.add_comment(
                    session_id,
                    id=cid,
                    text=text,
                    author_id=me.user_id,
                    author=me.name,
                    mentions=comments.parse_mentions(app, text),
                    created_at=when,
                    updated_at=when,
                    **extra,
                )
            except Exception:  # noqa: BLE001 - one that cannot be placed now is not shown
                logger.debug("Could not show comment %s again", cid, exc_info=True)
                continue
            existing[cid] = repo.get_comment(cid)
            changed_comments = True
        elif c is None:
            continue
        elif op == "edit" and not c.deleted and isinstance(p.get("text"), str):
            text = p["text"].strip()
            if text and repo.update_comment_text(cid, text, comments.parse_mentions(app, text)):
                changed_comments = True
        elif op == "delete" and not c.deleted:
            if repo.delete_comment(cid):
                existing.pop(cid, None)
                changed_comments = True
        elif op == "resolve":
            repo.set_comment_resolved(cid, datetime.now(), me.name)
            changed_comments = True
        elif op == "reopen":
            repo.set_comment_resolved(cid, None)
            changed_comments = True
    if changed_comments:
        app.bus.publish({"type": "comments", "session_id": session_id, "mentions": []})


# ---- building and sending a push -----------------------------------------------------------


@dataclass
class _Item:
    record: Any  # Contribution
    frames: dict[str, str] = field(default_factory=dict)  # sha256 -> base64
    size: int = 0  # decoded bytes of the frames
    outbox_id: str = ""  # the outbox row it settles
    notes: tuple[str, int] | None = None  # (meeting, last seq) of a notes chunk


@dataclass
class PushSummary:
    """What a push did."""

    state: str = "nothing"  # nothing | ok | unreachable | skew | error
    error: str | None = None
    sent: int = 0
    accepted: int = 0
    rejected: int = 0


def _chunk_id(session_id: str, hashes: list[str]) -> str:
    digest = hashlib.sha256(("notes|" + session_id + "|" + ",".join(hashes)).encode()).hexdigest()
    return digest[:12]


def _notes_item(app: AppContext, session_id: str, chunk: list[tuple[int, bytes]]) -> _Item:
    """One notes_frames record for these frames (in log order) of one meeting."""
    from .shares import frame_hash, make_contribution

    hashes = [frame_hash(f) for _, f in chunk]
    record = make_contribution(
        app.person,
        "notes_frames",
        session_id,
        {"frame_hashes": hashes},
        record_id=_chunk_id(session_id, hashes),
    )
    unique = {h: f for h, (_, f) in zip(hashes, chunk, strict=True)}
    return _Item(
        record=record,
        frames={h: base64.b64encode(f).decode() for h, f in unique.items()},
        size=sum(len(f) for f in unique.values()),
        notes=(session_id, chunk[-1][0]),
    )


def _build(app: AppContext, row: dict) -> list[_Item]:
    """The records waiting for this contact, signed, in the order they were made: comments and
    flags first, then each meeting's typed notes in chunks. Blocking."""
    from .shares import make_contribution

    person = row["person"]
    items: list[_Item] = []
    for r in app.repo.outbox_pending(person=person):
        record = make_contribution(
            app.person,
            r["kind"],
            r["session_id"],
            r["payload"],
            record_id=r["id"],
            at=r["created_at"],
        )
        items.append(_Item(record=record, outbox_id=r["id"]))
    for session_id in app.repo.shared_from_ids(person):
        chunk: list[tuple[int, bytes]] = []
        size = 0
        for seq, frame in _notes_ops(app, session_id):
            if chunk and (size + len(frame) > CHUNK_BYTES or len(chunk) >= CHUNK_FRAMES):
                items.append(_notes_item(app, session_id, chunk))
                chunk, size = [], 0
            chunk.append((seq, frame))
            size += len(frame)
        if chunk:
            items.append(_notes_item(app, session_id, chunk))
    return items


def _batches(items: list[_Item]) -> list[list[_Item]]:
    """Batches under the hub's limits (records and bytes of frames), the order kept."""
    from .shares import MAX_FRAME_BYTES, MAX_RECORDS

    out: list[list[_Item]] = []
    current: list[_Item] = []
    size = 0
    for item in items:
        if current and (len(current) >= MAX_RECORDS or size + item.size > MAX_FRAME_BYTES - 4096):
            out.append(current)
            current, size = [], 0
        current.append(item)
        size += item.size
    if current:
        out.append(current)
    return out


def _settle(app: AppContext, batch: list[_Item], response: ContributionResponse) -> tuple[int, int]:
    """Blocking: record the hub's answers. (accepted, rejected) counts."""
    by_id = {r.id: r for r in response.results}
    outbox: list[tuple[str, bool, str]] = []
    blocked: set[str] = set()  # meetings whose notes cursor must not pass a refused chunk
    accepted = rejected = 0
    touched: set[str] = set()
    for item in batch:
        result = by_id.get(item.record.id)
        if result is None:
            continue  # no answer: still waiting
        touched.add(item.record.session_id)
        accepted += int(result.ok)
        rejected += int(not result.ok)
        if item.notes is not None:
            sid, last = item.notes
            if not result.ok:
                blocked.add(sid)
                app.repo.set_notes_rejection(sid, result.reason or "Refused")
            elif sid not in blocked:
                app.repo.set_pushed_seq(sid, max(last, app.repo.pushed_seq(sid)))
                app.repo.set_notes_rejection(sid, "")
        else:
            outbox.append((item.outbox_id, result.ok, result.reason))
    if outbox:
        app.repo.outbox_mark(outbox)
    for sid in touched:
        _announce(app, sid)
    return accepted, rejected


async def push(app: AppContext, row: dict, *, repull: bool = False) -> PushSummary:
    """Send everything waiting for this contact (its row from `repo.contact`). `repull`: ask
    for a pull round when something was accepted (a push the pull round did not start itself).
    One push per contact at a time; the second waits and then sends what is left."""
    summary = PushSummary()
    if app.settings.team_mode or app.share_pull is None:
        return summary
    person = row["person"]
    name = contacts.my_or_card_name(row) or "This contact"
    pusher = app.share_push
    lock = pusher.lock_for(person) if pusher is not None else asyncio.Lock()
    reset = access.current.set(None)  # this computer's own work, whoever's write started it
    try:
        async with lock:
            items = await asyncio.to_thread(_build, app, row)
            if not items:
                return summary
            summary.state = "ok"

            async def send(client, endpoint: str) -> None:
                for batch in _batches(items):
                    body = ContributionBatch(
                        records=[i.record for i in batch],
                        frames={h: b for i in batch for h, b in i.frames.items()},
                    )
                    r = await client.post(
                        "/api/share/contributions", json=body.model_dump(mode="json")
                    )
                    r.raise_for_status()
                    response = ContributionResponse.model_validate(r.json())
                    summary.sent += len(batch)
                    ok, bad = await asyncio.to_thread(_settle, app, batch, response)
                    summary.accepted += ok
                    summary.rejected += bad

            try:
                await app.share_pull.with_machine(row, send)
            except Exception as e:  # noqa: BLE001 - everything unsettled stays waiting
                from .share_pull import describe_failure

                summary.state, summary.error = describe_failure(e, name)
    finally:
        access.current.reset(reset)
    if repull and summary.accepted:
        app.share_pull.poke(person)
    return summary


# ---- when ------------------------------------------------------------------------------


class SharePusher:
    """Schedules pushes after local writes: one per contact a moment after the last write."""

    def __init__(self, ctx: AppContext):
        self.ctx = ctx
        # Seconds to wait for the next write; None: never on its own (tests drive the pushes).
        self.debounce: float | None = DEBOUNCE
        self._loop: asyncio.AbstractEventLoop | None = None
        self._timers: dict[str, asyncio.TimerHandle] = {}
        self._locks: dict[str, asyncio.Lock] = {}
        self._tasks: set[asyncio.Task] = set()

    def start(self) -> None:
        self._loop = asyncio.get_running_loop()
        self._locks.clear()

    async def stop(self) -> None:
        for handle in self._timers.values():
            handle.cancel()
        self._timers.clear()
        tasks = list(self._tasks)
        self._loop = None
        for task in tasks:
            task.cancel()
        for task in tasks:
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        self._tasks.clear()

    def lock_for(self, person: str) -> asyncio.Lock:
        return self._locks.setdefault(person, asyncio.Lock())

    async def drain(self) -> None:
        """Wait for the timers and pushes started so far (tests)."""
        while self._timers or any(not t.done() for t in self._tasks):
            if self._timers:
                await asyncio.sleep((self.debounce or 0) + 0.01)
            else:
                await asyncio.gather(
                    *[t for t in self._tasks if not t.done()], return_exceptions=True
                )

    def schedule(self, person: str) -> None:
        """A push for this contact soon. From any thread; nothing happens before `start`."""
        loop = self._loop
        if loop is None or self.debounce is None or loop.is_closed():
            return
        with contextlib.suppress(RuntimeError):  # the loop closed meanwhile
            loop.call_soon_threadsafe(self._arm, person)

    def _arm(self, person: str) -> None:
        assert self._loop is not None
        old = self._timers.pop(person, None)
        if old is not None:
            old.cancel()
        self._timers[person] = self._loop.call_later(self.debounce or 0, self._fire, person)

    def _fire(self, person: str) -> None:
        self._timers.pop(person, None)
        task = asyncio.create_task(self._run(person))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _run(self, person: str) -> None:
        row = self.ctx.repo.contact(person)
        if row is None or row["removed_at"]:
            return
        try:
            await push(self.ctx, row, repull=True)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            logger.exception("Share push to %s failed", contacts.my_or_card_name(row))


# ---- the status the UI shows ---------------------------------------------------------------


def _excerpt(app: AppContext, row: dict) -> str:
    p = row["payload"]
    text = ""
    if row["kind"] == "comment":
        text = p.get("text") or ""
        if not text:
            c = app.repo.get_comment(p.get("comment_id") or "")
            text = (c.text or c.quote) if c is not None else ""
    else:
        session = app.repo.get(row["session_id"])
        items = session.summary_data.action_items if session and session.summary_data else []
        text = next((a.text for a in items if a.id == p.get("item_id")), "")
    return " ".join(text.split())[:EXCERPT]


def rejected_for(app: AppContext, session_id: str) -> list[dict]:
    """[{kind, reason, excerpt, at}]: what the owner refused, newest first, my typed notes (when
    refused) among them."""
    out = []
    notes_reason = app.repo.notes_rejection(session_id)
    if notes_reason:
        out.append({"kind": "notes", "reason": notes_reason, "excerpt": "", "at": ""})
    for row in app.repo.outbox_rejected(session_id):
        out.append(
            {
                "kind": row["kind"],
                "reason": row["reason"],
                "excerpt": _excerpt(app, row),
                "at": row["pushed_at"],
            }
        )
    return out


__all__ = [
    "PushSummary",
    "SharePusher",
    "enqueue_comment",
    "enqueue_item_flag",
    "notes_typed",
    "push",
    "reapply_pending",
    "rejected_for",
    "waiting_for",
]
