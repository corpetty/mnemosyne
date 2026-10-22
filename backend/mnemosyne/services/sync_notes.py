"""Library sync of a meeting's live notes document (phase 6, T7; rules in
docs/plans/2026-10-14-library-sync-rules.md section 6).

The document (services/shared_notes.py) travels in `SyncSession.notes_doc`, the base64 of its
canonical snapshot, folded when served. When a meeting arrives (services/sync_service.py `take`):

- both sides have a document and they differ: `union` merges them through the sidecar
  (`notes doc` `merge`: the op set union, no base needed), `store_union` stores the merged
  snapshot as the fold, logs what the local one lacked as author "sync (<peer>)", broadcasts it
  to the editors connected here and sets `Session.notes` to the merged render, all before the
  rest of the meeting is written;
- only the arriving side has one: `adopt` stores it where there was none (that clears
  `needs_doc_from_sync`) and tells the editors of an open room to reload. The meeting's text
  was merged as text by the caller; whatever differs from the adopted document's render catches
  up into it later (`shared_notes.notes_drift`);
- only we have one: nothing here, the text merge and the catch-up cover it.

All of it runs in a thread with the meeting's fold lock held (the caller takes it), so no fold
or catch-up races a merge.
"""

from __future__ import annotations

import base64
import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING

from . import shared_notes
from .shared_notes import NotesDocError

if TYPE_CHECKING:
    from ..api.context import AppContext

logger = logging.getLogger(__name__)

RELOAD_CLOSE = 1000


@dataclass
class Union:
    """The merged document and what the local snapshot lacked of it."""

    snapshot: bytes
    markdown: str
    frames: list[bytes]
    changed: bool  # the union differs from the local snapshot
    plain: str | None = None  # its plain text and comment ranges (shared_notes.marks_of)
    comments: dict | None = None


def author(peer_name: str) -> str:
    return f"sync ({peer_name})"


def local_snapshot(ctx: AppContext, session_id: str) -> bytes | None:
    doc = ctx.repo.notes_doc(session_id)
    return bytes(doc["snapshot"]) if doc and doc["snapshot"] is not None else None


def union(ctx: AppContext, session_id: str, theirs: str) -> Union:
    """Merge their snapshot (base64) into the stored one through the sidecar. Raises
    NotesDocError (no sidecar, or it failed or answered something unreadable)."""
    binary = shared_notes._binary()
    mine = local_snapshot(ctx, session_id)
    if binary is None or mine is None:
        raise NotesDocError("no sidecar or no local document to merge into")
    reply = shared_notes._run_doc(
        binary,
        {
            "op": "merge",
            "notes": shared_notes.notes_origin(session_id),
            "snapshot": base64.b64encode(mine).decode("ascii"),
            "other": theirs,
        },
    )
    try:
        merged = shared_notes._unb64(reply["snapshot"])
        frames = [shared_notes._unb64(f) for f in reply["frames"]]
        markdown = str(reply["markdown"])
    except (KeyError, TypeError, ValueError) as e:
        raise NotesDocError(f"unreadable merge reply: {e}") from e
    plain, comments = shared_notes.marks_of(reply)
    return Union(merged, markdown, frames, changed=merged != mine, plain=plain, comments=comments)


def store_union(ctx: AppContext, session_id: str, u: Union, peer_name: str) -> None:
    """Store a union (no new version of the meeting: the caller's write of the rest counts it)
    and send the editors connected here what the merge brought."""
    if not u.changed:
        return
    seqs = ctx.repo.store_synced_notes_doc(
        session_id,
        u.snapshot,
        u.markdown,
        u.frames,
        author(peer_name),
        notes=u.markdown,
        plain=u.plain,
        comments=u.comments,
    )
    if seqs:
        shared_notes._on_loop(
            ctx,
            shared_notes._deliver,
            ctx,
            session_id,
            list(zip(seqs, u.frames, strict=True)),
            None,
        )
    shared_notes._on_loop(ctx, ctx.bus.publish, {"type": "notes", "session_id": session_id})


def adopt(ctx: AppContext, session_id: str, theirs: str, peer_name: str) -> bool:
    """Take their document where we have none stored. False when it cannot be read (the
    sidecar says so), then the meeting simply stays without one."""
    repo = ctx.repo
    if local_snapshot(ctx, session_id) is not None:
        return False
    try:
        snapshot = shared_notes._unb64(theirs)
    except ValueError:
        logger.warning("Sync: the notes document of %s is not base64; ignored", session_id)
        return False
    markdown: str | None = None
    plain: str | None = None
    comments: dict | None = None
    binary = shared_notes._binary()
    if binary is not None:
        try:
            reply = shared_notes._run_doc(
                binary,
                {
                    "op": "fold",
                    "notes": shared_notes.notes_origin(session_id),
                    "snapshot": theirs,
                    "frames": [],
                },
            )
            markdown = str(reply["markdown"])
            plain, comments = shared_notes.marks_of(reply)
        except (NotesDocError, KeyError) as e:
            logger.warning("Sync: the notes document of %s cannot be read: %s", session_id, e)
            return False
    if markdown is None:  # no sidecar to render it: the text that came with it
        source = repo.notes_source(session_id)
        markdown = source[0] if source else ""
    # Frames typed here and never folded (no sidecar then) stay in the log, unfolded: the next
    # fold applies them on top of the adopted document.
    repo.store_synced_notes_doc(
        session_id, snapshot, markdown, [], author(peer_name), plain=plain, comments=comments
    )
    shared_notes._on_loop(ctx, reload_room, ctx, session_id)
    return True


def reload_room(ctx: AppContext, session_id: str) -> None:
    """On the event loop: the meeting's open editors hold a document (or none) that is not
    the one stored now. They are told `{"type": "reload"}` and the socket is closed normally
    (1000); a client rejoins and gets the stored document."""
    room = shared_notes.hub(ctx).rooms.get(session_id)
    if room is None:
        return
    for member in list(room.members):
        member.send("json", {"type": "reload"})
        member.send("close", RELOAD_CLOSE)
