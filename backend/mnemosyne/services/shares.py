"""Sharing a meeting with a contact: the signed `SharedMeeting` one share projects
(docs/plans/2026-10-23-phase-8-tasks.md, C12 and C13; models/shared_meeting.py), and the hub
taking back what the contact did on it (`accept_contributions`, C1, C9, C10, C12).

Nothing outside the share's parts is ever read into the result: each section is copied only
when its part is on. "Hide identifiers" covers the summary, the decisions, questions and items,
chapters, topics, the title and the transcript, not the notes or the comments (C13).
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import logging
import re
import secrets
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from pydantic import ValidationError

from .. import access
from ..models.comment import CommentCreate
from ..models.session import ActionItem, Session, SummaryData
from ..models.shared_meeting import (
    Contribution,
    ContributionBatch,
    ContributionResult,
    SharedMeeting,
    SharedOwner,
    SharedSegment,
    ShareParts,
)
from ..models.sync_session import SyncComment
from ..summarization.privacy import redact_contacts, redact_identifiers, redact_transcript
from . import comments, contacts, item_flags, shared_notes
from .person import PersonKeys

if TYPE_CHECKING:
    from ..api.context import AppContext

logger = logging.getLogger(__name__)


class ShareNotFound(LookupError):
    """No live share of this meeting with that person (or no such meeting)."""


# Which part a comment's target belongs to ('' = the meeting as a whole: always included).
_COMMENT_PART = {
    "segment": "transcript",
    "notes": "notes",
    "body": "summary",
    "decision": "decisions",
    "question": "questions",
    "item": "action_items",
}


def _redact(text: str) -> str:
    return redact_contacts(redact_identifiers(text)) if text else text


def _summary_data(data: SummaryData | None, parts: ShareParts, text) -> SummaryData | None:
    """The chosen sections of the summary, with their stable ids, and nothing else: no model or
    provider, no source hash, no drafted follow-up, no client facts, no edit trail, no issue
    links on items."""
    if data is None:
        return None
    kept = SummaryData(style=data.style)
    if parts.summary:
        kept.title = text(data.title)
    if parts.topics:
        kept.topics = [text(t) for t in data.topics]
    if parts.decisions:
        kept.decisions = [text(d) for d in data.decisions]
        kept.decision_at = list(data.decision_at)
        kept.decision_ids = list(data.decision_ids)
        kept.decision_links = dict(data.decision_links)
    if parts.questions:
        kept.open_questions = [text(q) for q in data.open_questions]
        kept.question_at = list(data.question_at)
        kept.question_ids = list(data.question_ids)
    if parts.action_items:
        kept.action_items = [
            ActionItem.model_validate({**a.model_dump(), "text": text(a.text), "issue_url": None})
            for a in data.action_items
        ]
    if parts.chapters:
        kept.chapters = [c.model_copy(update={"title": text(c.title)}) for c in data.chapters]
    return kept


def _comments(
    app: AppContext, session_id: str, parts: ShareParts, owner_name: str
) -> list[SyncComment]:
    """Comments on the shared parts. A reply follows its thread's first comment (replies carry
    no target of their own); a thread whose first comment is gone is left out.

    The owner's own comments are stored without an author id (on the recipient, "no author id"
    means "you"): they go out as the owner's (`p:` id and name). Comments by contacts keep
    theirs. A resolution by the local user is the owner's name. Mentions keep only `p:` ids:
    local and team user ids mean nothing on another computer."""
    pid = app.person.pid
    local_name = (app.settings.local_speaker_name or "").strip() or "You"
    rows = app.repo.comment_rows(session_id)
    target_of = {r["id"]: r["target"] for r in rows if not r["parent_id"]}
    out = []
    for r in rows:
        if r["parent_id"]:
            if r["parent_id"] not in target_of:
                continue
            target = target_of[r["parent_id"]]
        else:
            target = r["target"]
        if target:
            part = _COMMENT_PART.get(target)
            if part is None or not getattr(parts, part):
                continue
        row = {k: r[k] for k in SyncComment.model_fields}
        if not row["author_id"]:
            row["author_id"], row["author"] = pid, owner_name
        if row["resolved_by"] == local_name:
            row["resolved_by"] = owner_name
        row["mentions"] = [m for m in row["mentions"] if str(m).startswith("p:")]
        out.append(SyncComment(**row))
    return out


def _shared_with(app: AppContext, session_id: str) -> list[str]:
    names = []
    for share in app.repo.peer_shares(session_id):
        row = app.repo.contact(share["person"])
        names.append((row or {}).get("name") or "p:" + share["person"][:16])
    return names


def _canonical(sm: SharedMeeting, exclude: set[str]) -> bytes:
    return PersonKeys.canonical(sm.model_dump(mode="json", exclude=exclude))


def digest_of(sm: SharedMeeting) -> str:
    return hashlib.sha256(_canonical(sm, {"digest", "sig"})).hexdigest()


def build_shared_meeting(app: AppContext, session_id: str, person: str) -> SharedMeeting:
    """What `person` (a contact's key) gets of the meeting, signed by this person's key.
    Raises ShareNotFound without a live share. Blocking: with notes shared it first folds the
    live notes document (as library sync does), so the markdown and the snapshot are current."""
    share = app.repo.peer_share(session_id, person)
    if share is None or share.get("revoked_at"):
        raise ShareNotFound(session_id)
    parts = ShareParts.from_stored(share["parts"])
    if parts.notes:
        # The document set up as for a join first (seeded from typed notes that never had
        # one), so the recipient gets a snapshot to type into, never an empty one.
        if shared_notes.available():
            try:
                shared_notes.ensure_doc_blocking(app, session_id)
            except shared_notes.NotesDocError as e:
                logger.warning("Notes of %s not set up for sharing: %s", session_id, e)
        shared_notes.fold_blocking(app, session_id)
    session: Session | None = app.repo.get(session_id)
    if session is None:
        raise ShareNotFound(session_id)

    def text(value: str) -> str:
        return _redact(value) if parts.hide_identifiers else value

    def text_contacts(value: str) -> str:
        # The transcript's identifiers went through redact_transcript, which reads each line
        # after the one before; e-mail addresses and phone numbers are left.
        return redact_contacts(value) if parts.hide_identifiers else value

    transcript: list[SharedSegment] = []
    if parts.transcript:
        lines = session.transcript
        if parts.hide_identifiers:
            lines = redact_transcript(lines)
        transcript = [
            SharedSegment(speaker=s.speaker, text=text_contacts(s.text), start=s.start, end=s.end)
            for s in lines
        ]
    notes, notes_doc = "", ""
    if parts.notes:
        notes = session.notes
        doc = app.repo.notes_doc(session_id)
        if doc and doc["snapshot"] is not None:
            notes_doc = base64.b64encode(bytes(doc["snapshot"])).decode("ascii")

    owner = SharedOwner(person=app.person.public_hex, name=contacts.display_name(app))
    sm = SharedMeeting(
        id=session.id,
        name=text(session.name),
        created_at=session.created_at,
        updated_at=session.updated_at,
        owner=owner,
        parts=parts,
        summary=text(session.summary) if parts.summary else "",
        summary_data=_summary_data(session.summary_data, parts, text),
        attendees=[text(a) for a in session.attendees] if parts.attendees else [],
        participants=[text(p) for p in session.participants] if parts.attendees else [],
        transcript=transcript,
        notes=notes,
        notes_doc=notes_doc,
        comments=_comments(app, session_id, parts, owner.name),
        shared_with=_shared_with(app, session_id),
    )
    sm.digest = digest_of(sm)
    sm.sig = app.person.sign(_canonical(sm, {"sig"}))
    return sm


def verify_shared_meeting(sm: SharedMeeting | dict[str, Any], owner_person: str) -> bool:
    """Whether `sm` was signed by `owner_person` and is unchanged since: the owner named in it
    is that person, the digest matches its content and the signature verifies. Never raises."""
    try:
        if not isinstance(sm, SharedMeeting):
            sm = SharedMeeting.model_validate(sm)
        if sm.owner.person != owner_person or sm.digest != digest_of(sm):
            return False
        return PersonKeys.verify(owner_person, sm.sig, _canonical(sm, {"sig"}))
    except Exception:
        return False


# ---- contributions: what a contact did on a meeting shared with them -----------------------

MAX_RECORDS = 200  # per batch
MAX_FRAME_BYTES = 2 * 1024 * 1024  # all of a batch's notes frames together, decoded
_SHA256 = re.compile(r"[0-9a-f]{64}")
COMMENT_OPS = ("create", "edit", "delete", "resolve", "reopen")
ITEM_FLAGS = ("done", "follow")


class Refused(Exception):
    """A record not applied; the message is the reason the sender shows on the item."""


def contribution_bytes(record: Contribution) -> bytes:
    """What a contribution's `sig` signs: the canonical JSON of the record without `sig`."""
    return PersonKeys.canonical(record.model_dump(mode="json", exclude={"sig"}))


def make_contribution(
    keys: PersonKeys,
    kind: str,
    session_id: str,
    payload: dict,
    *,
    record_id: str = "",
    at: str = "",
) -> Contribution:
    """A record signed with `keys` (the sender's side; also what the tests send)."""
    unsigned = Contribution(
        id=record_id or secrets.token_hex(6),
        kind=kind,
        session_id=session_id,
        payload=payload,
        author=keys.public_hex,
        at=at or datetime.now(UTC).isoformat(),
        sig="",
    )
    return unsigned.model_copy(update={"sig": keys.sign(contribution_bytes(unsigned))})


def frame_hash(frame: bytes) -> str:
    return hashlib.sha256(frame).hexdigest()


def batch_too_big(batch: ContributionBatch) -> str | None:
    """Why a batch is over the limits (answered 413), or None."""
    if len(batch.records) > MAX_RECORDS:
        return f"At most {MAX_RECORDS} records at a time"
    size = sum(len(b64) * 3 // 4 for b64 in batch.frames.values())
    if size > MAX_FRAME_BYTES:
        return f"At most {MAX_FRAME_BYTES // (1024 * 1024)} MB of notes frames at a time"
    return None


def _decode_frames(raw: dict[str, str]) -> dict[str, bytes | None]:
    """The batch's frames by their stated hash; None for one that is not base64."""
    out: dict[str, bytes | None] = {}
    for digest, b64 in raw.items():
        try:
            out[digest] = base64.b64decode(b64, validate=True)
        except (binascii.Error, ValueError):
            out[digest] = None
    return out


def _record_frames(
    record: Contribution, frames: dict[str, bytes | None]
) -> list[tuple[str, bytes]]:
    """The frames a notes_frames record lists, in order, each checked against its hash."""
    hashes = record.payload.get("frame_hashes")
    if not isinstance(hashes, list) or not hashes:
        raise Refused("No notes frames listed")
    out = []
    for digest in hashes:
        if not isinstance(digest, str) or not _SHA256.fullmatch(digest):
            raise Refused("A notes frame is listed by something that is not a SHA-256")
        frame = frames.get(digest)
        if frame is None:
            raise Refused("A notes frame is missing")
        if frame_hash(frame) != digest:
            raise Refused("A notes frame does not match its hash")
        if len(frame) > shared_notes.MAX_FRAME:
            raise Refused("A notes frame is too big")
        out.append((digest, frame))
    return out


_PART_LABEL = {
    "transcript": "transcript is",
    "notes": "notes are",
    "summary": "summary is",
    "decisions": "decisions are",
    "questions": "open questions are",
    "action_items": "action items are",
}


def _require_part(parts: ShareParts, part: str | None) -> None:
    """Refused unless the share includes `part` (None: the meeting as a whole, always)."""
    if part is not None and not getattr(parts, part):
        raise Refused(f"This meeting's {_PART_LABEL[part]} not shared with you")


def _part_of_target(target: str) -> str | None:
    """The share part a comment target belongs to ('' = the meeting as a whole: None). An
    unknown target is left to the comments service to refuse."""
    return _COMMENT_PART.get(target) if target else None


def _comment_target(app: AppContext, session_id: str, comment_id: str) -> str:
    """The target of the thread an existing comment is in (a reply follows its root)."""
    c = app.repo.get_comment(comment_id)
    if c is None or c.session_id != session_id:
        raise comments.CommentNotFound("Comment not found")
    if c.parent_id:
        root = app.repo.get_comment(c.parent_id)
        return root.target if root is not None else c.target
    return c.target


def _apply_comment(app: AppContext, record: Contribution, parts: ShareParts) -> None:
    p = record.payload
    op, comment_id = p.get("op"), p.get("comment_id")
    if op not in COMMENT_OPS:
        raise Refused(f"Unknown comment operation {op!r}")
    if not isinstance(comment_id, str) or not comment_id:
        raise Refused("A comment id is needed")
    sid = record.session_id
    if op == "create":
        fields = {k: v for k, v in p.items() if k not in ("op", "comment_id")}
        try:
            req = CommentCreate.model_validate(fields)
        except ValidationError as e:
            raise Refused("Not a comment: " + "; ".join(x["msg"] for x in e.errors())) from e
        if req.parent_id:
            _require_part(parts, _part_of_target(_comment_target(app, sid, req.parent_id)))
        else:
            _require_part(parts, _part_of_target(req.target))
            if req.target == "notes" and not shared_notes.available():
                raise comments.NotesNotShared("Live notes are not available on this computer")
        c = comments.create(app, sid, req, comment_id=comment_id)
        _mark_notes_comment(app, sid, c.id, req)
        return
    _require_part(parts, _part_of_target(_comment_target(app, sid, comment_id)))
    if op == "edit":
        text = p.get("text")
        if not isinstance(text, str):
            raise Refused("An edit needs text")
        comments.edit(app, sid, comment_id, text)
    elif op == "delete":
        comments.delete(app, sid, comment_id)
    elif op == "resolve":
        comments.resolve(app, sid, comment_id)
    else:
        comments.reopen(app, sid, comment_id)


def _mark_notes_comment(app: AppContext, session_id: str, comment_id: str, req) -> None:
    """A new notes thread with `notes_ids`: the mark put on the range in the document, as the
    comments route does (`create_marked`), here from a thread. Unmarked, it stays detached."""
    if req.target != "notes" or req.parent_id:
        return
    ids = [i.strip() for i in req.notes_ids]
    if len(ids) != 2 or not all(ids):
        return
    p = access.principal()
    try:
        frames = shared_notes.mark_comment_blocking(
            app, session_id, comment_id, ids[0], ids[1], p.user_id if p else "", p.name if p else ""
        )
    except shared_notes.NotesDocError as e:
        logger.info("Could not mark comment %s in the notes of %s: %s", comment_id, session_id, e)
        return
    if frames:
        shared_notes._on_loop(app, shared_notes._deliver, app, session_id, frames, None)
        app.bus.publish({"type": "comments", "session_id": session_id, "mentions": []})


def _apply_item_flag(app: AppContext, record: Contribution) -> None:
    """Tick (done) or star (follow) an action item by its id, through the action-item route's
    own path (services/item_flags.py)."""
    p = record.payload
    item_id, flag, value = p.get("item_id"), p.get("flag"), p.get("value")
    if flag not in ITEM_FLAGS:
        raise Refused(f"Unknown action item flag {flag!r}")
    if not isinstance(item_id, str) or not item_id or not isinstance(value, bool):
        raise Refused("An action item flag needs item_id and a true or false value")
    session = app.repo.get(record.session_id)
    if session is None:
        raise access.Hidden("Session not found")
    items = session.summary_data.action_items if session.summary_data else []
    idx = next((i for i, a in enumerate(items) if a.id == item_id), None)
    if idx is None:
        raise Refused("No such action item")
    who = access.principal()
    item_flags.set_item_flags(app, session, idx, by=who.name if who else "", **{flag: value})


def _apply_frames(app: AppContext, record: Contribution, frames: list[tuple[str, bytes]]) -> None:
    """The record's frames that are not here yet, through the notes relay."""
    new = [(d, f) for d, f in frames if not app.repo.has_signature("frame:" + d)]
    if not new:
        return
    who = access.principal()
    try:
        shared_notes.accept_frames(
            app,
            record.session_id,
            [f for _, f in new],
            who.user_id if who else "",
            who.name if who else "",
        )
    except shared_notes.FramesRefused as e:
        raise Refused(str(e)) from e
    for digest, _ in new:
        app.repo.add_signature("frame:" + digest, record.author, record.sig)


def _apply(
    app: AppContext,
    person: str,
    record: Contribution,
    frames: dict[str, bytes | None],
) -> ContributionResult:
    """One record, checked and applied (the caller has set the contact's principal)."""
    if not PersonKeys.verify(record.author, record.sig, contribution_bytes(record)):
        raise Refused("The signature does not verify")
    if record.author != person:
        raise Refused("This record is signed by someone else")
    kept = app.repo.signature_of(record.id)
    if kept is not None:
        if kept["signer"] != record.author:
            raise Refused("That record id is taken")
        return ContributionResult(id=record.id, ok=True, reason="")  # applied before
    share = app.repo.peer_share(record.session_id, person)
    if share is None or share.get("revoked_at"):
        raise Refused("This meeting is not shared with you")
    parts = ShareParts.from_stored(share["parts"])
    if record.kind == "comment":
        _apply_comment(app, record, parts)
    elif record.kind == "item_flag":
        _require_part(parts, "action_items")
        _apply_item_flag(app, record)
    else:
        _require_part(parts, "notes")
        _apply_frames(app, record, _record_frames(record, frames))
    app.repo.add_signature(record.id, record.author, record.sig)
    return ContributionResult(id=record.id, ok=True, reason="")


def accept_contributions(
    app: AppContext, contact_row: dict, batch: ContributionBatch
) -> list[ContributionResult]:
    """What a contact sends back, one result per record in order. Each record is checked
    (signature, signed by this contact, live share with the part) and applied as the contact
    (`contacts.principal_of`) through the usual services, so their rules, history lines and
    events apply; idempotent by record id (`signatures`). A bad record is refused with a
    reason and the rest go on. Blocking (notes folds, the vault); sets the principal itself,
    so call it in the thread that does the work."""
    frames = _decode_frames(batch.frames)
    person = contact_row["person"]
    token = access.current.set(contacts.principal_of(contact_row))
    results = []
    try:
        for record in batch.records:
            try:
                results.append(_apply(app, person, record, frames))
            except (Refused, access.Forbidden, comments.BadComment, comments.CommentNotFound,
                    comments.NotesNotShared, LookupError, ValueError) as e:  # fmt: skip
                reason = str(e) or type(e).__name__
                if isinstance(e, access.Hidden):
                    reason = "This meeting is not shared with you"
                results.append(ContributionResult(id=record.id, ok=False, reason=reason))
            except Exception:
                logger.warning("Contribution %s could not be applied", record.id, exc_info=True)
                results.append(
                    ContributionResult(id=record.id, ok=False, reason="It could not be applied")
                )
    finally:
        access.current.reset(token)
    return results


__all__ = [
    "MAX_FRAME_BYTES",
    "MAX_RECORDS",
    "ShareNotFound",
    "accept_contributions",
    "batch_too_big",
    "build_shared_meeting",
    "contribution_bytes",
    "digest_of",
    "frame_hash",
    "make_contribution",
    "verify_shared_meeting",
]
