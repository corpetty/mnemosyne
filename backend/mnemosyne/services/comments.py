"""Comments on a meeting: threads on transcript lines, moments of the audio, summary blocks and
summary text (docs/plans/2026-10-12-hashseq-collaboration.md, "Phase 5 design"; storage in
SessionRepository, routes/comments.py).

A comment keeps where it was written, not a pointer into today's data, and is re-anchored when
read:

- transcript ("segment"): a time span (part, start, end), seconds into the part like bookmarks
  and live flags, so it follows edits, splits, merges and a new transcription. It lands on the
  line it overlaps most; a line counts when the overlap is at least half of the comment's span or
  half of the line's own duration (so a comment on several lines, or a line split in two, still
  lands). A zero-length span is a moment of the audio (from the player): the line playing then.
- decision / question / item: the summary block's stable id (services/summary_ids.py), which
  survives rewording.
- body: a text-quote selector (exact, prefix, suffix) on the summary text, with a fuzzy fallback.
- notes (the live shared notes, services/shared_notes.py): the mark `comment:<id>` the editor puts
  on the range in the document. The thread is made first (this module mints the id and stores
  the mark's name as its anchor), then the client marks the range; the sidecar's fold reports
  each mark's range in the notes' plain text, kept beside the snapshot (`notes_marks`). A
  range present: attached, `notes_range` = [start, end) in code points; `changed` when its
  text is not the quote any more. No range (every character deleted, or the client never
  marked it): detached, shown with its quote.

`changed`: the text it was written about is not what is there now. `detached`: its place is gone;
it is shown with its quote and never deleted on its own.

Who may do what (team server, access.py): anyone who can read the meeting comments; authors edit
their own; the author, the meeting's owner or an admin deletes; the meeting's owner, an admin or
the thread's author resolves and reopens. On the desktop (nobody signed in) everything is allowed,
except on a meeting someone shared with me (phase 8, services/shared_with_me.py): there I act as
myself as the owner's hub knows me (`contacts.my_principal`, a member who does not own it), and
only on the parts shared with me, the same rules the hub applies when it takes the comment.
"""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass
from datetime import datetime
from difflib import SequenceMatcher
from typing import TYPE_CHECKING
from uuid import uuid4

from .. import access
from ..models.comment import Comment, CommentCreate, CommentThread, SessionComments
from ..models.session import Session, SummaryData
from ..models.shared_meeting import ShareParts
from ..models.transcript import TranscriptSegment
from . import history, share_push, shared_notes, shared_with_me
from .contacts import pid_of
from .parts import offsets, part_of
from .summary_doc import section

if TYPE_CHECKING:
    from ..api.context import AppContext

TARGETS = ("", "segment", "body", "decision", "question", "item", "notes")
SECTIONS = {"decision": "decisions", "question": "open_questions", "item": "action_items"}
MIN_OVERLAP = 0.5  # of the comment's span, or of the line's own duration
NEAR = 5.0  # seconds: a moment between lines is on the nearest line this close
FUZZY = 0.85  # difflib ratio for summary text that was reworded
EXCERPT = 80  # characters of a comment in the meeting's history


class BadComment(ValueError):
    """A comment that cannot be placed or a thread that is not there (answered 400)."""


class NotesNotShared(RuntimeError):
    """A comment on the notes without the live document to mark it in (answered 409)."""


class CommentNotFound(LookupError):
    """No such comment in this meeting (answered 404)."""


def _norm(text: str) -> str:
    return " ".join(re.findall(r"\w+", (text or "").lower()))


def excerpt(text: str, limit: int = EXCERPT) -> str:
    flat = " ".join(text.split())
    return flat if len(flat) <= limit else flat[: limit - 1].rstrip() + "…"


# ---- who ---------------------------------------------------------------------------------


def _author(app: AppContext, session: Session | None = None) -> tuple[str, str]:
    """(author id, name): the signed-in person, or on the desktop "" and the microphone's name;
    on a meeting shared with me, my `p:` id and name (shared_with_me.acting)."""
    p = shared_with_me.acting(app, session) if session is not None else access.principal()
    if p is not None:
        return p.user_id, p.name
    return "", (app.settings.local_speaker_name or "").strip() or "You"


def parse_mentions(app: AppContext, text: str) -> list[str]:
    """User ids named with @ (full name, or a first name nobody else on the team has),
    case-insensitive, in order of appearance. Empty where there are no team users (desktop)."""
    if "@" not in text:
        return []
    users = [u for u in app.users.list() if not u.disabled]
    if not users:
        return []
    names: list[tuple[str, str]] = []
    firsts = Counter(u.name.split()[0].lower() for u in users if u.name.split())
    for u in users:
        words = u.name.lower().split()
        if not words:
            continue
        names.append((" ".join(words), u.id))
        if len(words) > 1 and firsts[words[0]] == 1:
            names.append((words[0], u.id))
    names.sort(key=lambda n: -len(n[0]))
    low = " ".join(text.lower().split())
    found: list[str] = []
    for m in re.finditer(r"(?<![\w@])@", low):  # not the @ of an email address
        rest = low[m.end() :]
        for name, user_id in names:
            after = rest[len(name) : len(name) + 1]
            if rest.startswith(name) and not (after.isalnum() or after == "_"):
                if user_id not in found:
                    found.append(user_id)
                break
    return found


# ---- anchors when written ----------------------------------------------------------------


def _line_at(lines: list[TranscriptSegment], t: float, quote: str = "") -> int | None:
    """The line playing at `t` on the meeting's timeline (where two touch, the later one, unless
    the earlier one's text is the quote)."""
    best, best_key = None, None
    q = _norm(quote)
    for i, ln in enumerate(lines):
        if ln.start <= t <= ln.end:
            key = (bool(q) and _norm(ln.text) == q, ln.start)
            if best_key is None or key > best_key:
                best, best_key = i, key
    return best


def _segment_anchor(session: Session, req: CommentCreate) -> tuple[dict, str]:
    lines = session.transcript
    starts = offsets(session)
    if req.seconds is not None:
        t = float(req.seconds)
        if t < 0:
            raise BadComment("The moment must not be before the start")
        part = part_of(t, starts)
        at = t - starts[part]
        line = _line_at(lines, t)
        return {"part": part, "start": at, "end": at}, lines[line].text if line is not None else ""
    if req.idx is None:
        raise BadComment("Say which transcript line (idx) or moment (seconds) it is on")
    last = req.idx if req.idx_to is None else req.idx_to
    if not 0 <= req.idx <= last < len(lines):
        raise BadComment("No such transcript line")
    chosen = lines[req.idx : last + 1]
    start, end = chosen[0].start, max(ln.end for ln in chosen)
    part = part_of(start, starts)
    off = starts[part]
    anchor = {"part": part, "start": start - off, "end": max(end, start) - off}
    return anchor, " ".join(ln.text for ln in chosen)


def _block_text(data: SummaryData | None, target: str, target_id: str) -> str | None:
    if data is None or not target_id:
        return None
    for block_id, text in section(data, SECTIONS[target]):
        if block_id == target_id:
            return text
    return None


def notes_mark(comment_id: str) -> str:
    """The name of the mark a notes comment has in the live document."""
    return f"comment:{comment_id}"


def _anchor(session: Session, req: CommentCreate, comment_id: str) -> tuple[str, str, dict, str]:
    """(target, target_id, anchor, quote) for a new thread (`comment_id`: the id it will have)."""
    target = req.target
    if target not in TARGETS:
        raise BadComment(f"Unknown target {target!r}")
    if target == "":
        return "", "", {}, ""
    if target == "notes":
        if not req.exact.strip():
            raise BadComment("Select the text of the notes to comment on (exact)")
        return target, "", {"mark": notes_mark(comment_id)}, req.exact
    if target == "segment":
        anchor, quote = _segment_anchor(session, req)
        return target, "", anchor, quote
    if target == "body":
        exact = req.exact
        if plain_markdown(exact) and plain_markdown(exact) not in plain_markdown(session.summary):
            raise BadComment("That text is not in the summary")
        anchor = {"exact": exact, "prefix": req.prefix, "suffix": req.suffix}
        return target, "", anchor, exact
    text = _block_text(session.summary_data, target, req.target_id)
    if text is None:
        raise BadComment(f"No such {target} in the summary")
    return target, req.target_id, {}, text


# ---- anchors when read -------------------------------------------------------------------


def place_segment(
    anchor: dict, quote: str, lines: list[TranscriptSegment], starts: dict[int, float]
) -> tuple[int | None, bool, bool, float | None]:
    """(line index, changed, detached, seconds on the meeting's timeline) of a transcript
    comment in today's transcript. A moment of the audio is never detached while its part is
    there: it is on the line playing then, else the nearest line within NEAR seconds, else on
    no line."""
    part = anchor.get("part")
    if not isinstance(part, int) or part not in starts or "start" not in anchor:
        return None, False, True, None
    s = starts[part] + float(anchor["start"])
    e = max(s, starts[part] + float(anchor.get("end", anchor["start"])))
    span = e - s
    q = _norm(quote)
    if span <= 0:
        line = _line_at(lines, s, quote)
        if line is None:
            line = _near(lines, s)
            return line, False, False, s
        return line, bool(q) and q not in _norm(lines[line].text), False, s
    best, best_key, covered = None, None, []
    for i, ln in enumerate(lines):
        overlap = min(ln.end, e) - max(ln.start, s)
        if overlap <= 0:
            continue
        own = ln.end - ln.start
        if overlap < MIN_OVERLAP * span - 1e-9 and not (
            own > 0 and overlap >= MIN_OVERLAP * own - 1e-9
        ):
            continue
        covered.append(i)
        key = (round(overlap, 6), _norm(ln.text) == q, -ln.start)
        if best_key is None or key > best_key:
            best, best_key = i, key
    if best is None:
        return None, False, True, s
    # Changed: the words written about are no longer in the lines it covers (a split or a
    # merge keeps them; an edit does not).
    now = _norm(" ".join(lines[i].text for i in covered))
    return best, bool(q) and q not in now, False, s


def _near(lines: list[TranscriptSegment], t: float) -> int | None:
    """The line whose start or end is nearest to `t`, if within NEAR seconds."""
    best, best_gap = None, NEAR
    for i, ln in enumerate(lines):
        gap = min(abs(ln.start - t), abs(ln.end - t))
        if gap <= best_gap and (best is None or gap < best_gap):
            best, best_gap = i, gap
    return best


def _sentences(text: str) -> list[tuple[int, int]]:
    spans, start = [], 0
    for m in re.finditer(r"(?<=[.!?])\s+|\n+", text):
        if m.start() > start:
            spans.append((start, m.start()))
        start = m.end()
    if start < len(text):
        spans.append((start, len(text)))
    return spans


_RULE = re.compile(r"^[ \t]*([-*_])(?:[ \t]*\1){2,}[ \t]*$", re.M)
_LINK = re.compile(r"!?\[([^\]]*)\]\([^)]*\)")
_LINE_MARK = re.compile(
    r"^[ \t]*(?:#{1,6}[ \t]+|>[ \t]?|(?:[-*+]|\d+[.)])[ \t]+(?:\[[ xX]\][ \t]+)?)+", re.M
)
_EMPHASIS = re.compile(r"\*\*|__|~~|`|\*|(?<!\w)_|_(?!\w)")


def plain_markdown(text: str) -> str:
    """Markdown as the reader sees it rendered, for matching a selection: no heading, list or
    quote markers, no emphasis or code ticks, links as their text, whitespace collapsed."""
    text = _RULE.sub(" ", text or "")
    text = _LINK.sub(r"\1", text)
    text = _LINE_MARK.sub("", text)
    text = _EMPHASIS.sub("", text)
    return " ".join(text.split())


def locate_body(summary: str, anchor: dict) -> tuple[int, int, bool] | None:
    """(start, end, changed) of a summary-text comment in today's summary, or None when gone.
    The selection is of rendered text, so it is matched against `plain_markdown(summary)` (and
    start/end are offsets into that). The exact text first (prefix and suffix pick among
    repeats), then the best fuzzy match over windows of sentences."""
    summary = plain_markdown(summary)
    exact = plain_markdown(anchor.get("exact") or "")
    if not exact:
        return (0, len(summary), False) if summary else None
    hits = [m.start() for m in re.finditer(re.escape(exact), summary)]
    if hits:
        prefix = plain_markdown(anchor.get("prefix") or "")
        suffix = plain_markdown(anchor.get("suffix") or "")

        def context(at: int) -> int:
            before = summary[:at].rstrip()[-len(prefix) :] if prefix else ""
            after = summary[at + len(exact) :].lstrip()[: len(suffix)]
            score = 0
            while (
                score < min(len(before), len(prefix)) and before[-1 - score] == prefix[-1 - score]
            ):
                score += 1
            tail = 0
            while tail < min(len(after), len(suffix)) and after[tail] == suffix[tail]:
                tail += 1
            return score + tail

        at = max(hits, key=context)  # first of the best: max keeps the first on ties
        return at, at + len(exact), False
    want = _norm(exact)
    if not want:
        return None
    sentences = _sentences(summary)
    width = len(_sentences(exact)) + 1
    best, best_ratio = None, 0.0
    for i in range(len(sentences)):
        for n in range(1, width + 1):
            if i + n > len(sentences):
                break
            start, end = sentences[i][0], sentences[i + n - 1][1]
            window = _norm(summary[start:end])
            if not window:
                continue
            ratio = _fragment_ratio(want, window)
            if ratio > best_ratio:
                best, best_ratio = (start, end), ratio
    if best is None or best_ratio < FUZZY:
        return None
    return best[0], best[1], True


def _fragment_ratio(want: str, window: str) -> float:
    """How well `want` matches the part of `window` it lines up with (a selection may be a
    fragment of a sentence)."""
    whole = SequenceMatcher(None, want, window, autojunk=False)
    blocks = [b for b in whole.get_matching_blocks() if b.size]
    if not blocks:
        return 0.0
    region = window[blocks[0].b : blocks[-1].b + blocks[-1].size]
    return max(whole.ratio(), SequenceMatcher(None, want, region, autojunk=False).ratio())


# ---- reading -----------------------------------------------------------------------------


def place_notes(
    comment_id: str, quote: str, marks: tuple[str, dict] | None
) -> tuple[list[int] | None, bool]:
    """(range, changed) of a notes comment from the last fold's `marks` (plain text and
    ranges): range None = detached. `changed`: the marked text is not the quote any more."""
    if marks is None:
        return None, False
    plain, ranges = marks
    found = ranges.get(comment_id)
    if not isinstance(found, list) or len(found) != 2:
        return None, False
    start = max(0, min(int(found[0]), len(plain)))
    end = max(start, min(int(found[1]), len(plain)))
    if end == start:
        return None, False
    now = plain[start:end].replace("\x1f", "\t")
    return [start, end], bool(_norm(quote)) and _norm(now) != _norm(quote)


@dataclass
class _Meeting:
    session: Session
    starts: dict[int, float]
    owner: str
    principal: access.Principal | None
    app: AppContext
    owns: bool = True  # the principal may change the meeting (its owner, an admin)
    parts: ShareParts | None = None  # a meeting shared with me: what of it was
    sharer_pid: str = ""  # ... and its owner's `p:` id
    _marks: tuple[str, dict] | None = None  # the notes' plain text and comment ranges
    _marks_read: bool = False

    def marks(self) -> tuple[str, dict] | None:
        if not self._marks_read:
            self._marks, self._marks_read = self.app.repo.notes_marks(self.session.id), True
        return self._marks

    @classmethod
    def of(cls, app: AppContext, session: Session) -> _Meeting:
        owner = app.repo.session_owner(session.id) or ""
        shared = session.shared_from
        return cls(
            session=session,
            starts=offsets(session),
            owner=owner,
            principal=shared_with_me.acting(app, session),
            app=app,
            # Someone else's meeting shared with me is never mine to moderate.
            owns=not shared and access.can_write(owner),
            parts=shared_with_me.parts_of(app, session.id, shared) if shared else None,
            sharer_pid=pid_of(shared) if shared else "",
        )

    def require_part(self, target: str) -> None:
        """On a meeting shared with me: Forbidden unless the target's part was shared."""
        shared_with_me.require_part(self.parts, shared_with_me.COMMENT_PART.get(target))

    def rights(self, c: Comment, root_author: str) -> tuple[bool, bool, bool]:
        """(can_edit, can_delete, can_resolve)."""
        root = not c.parent_id
        p = self.principal
        if p is None:
            return not c.deleted, not c.deleted, root
        admin = p.role == access.ADMIN
        mine = bool(c.author_id) and c.author_id == p.user_id
        owns = self.owns
        thread_mine = bool(root_author) and root_author == p.user_id
        return (
            not c.deleted and (mine or admin),
            not c.deleted and (mine or owns),
            root and (owns or thread_mine),
        )

    def view(self, c: Comment, root_author: str | None = None) -> Comment:
        out = c.model_copy()
        if self.sharer_pid and not c.author_id:
            # On a meeting shared with me no author id is its owner's (never mine, "You").
            out.author_id = self.sharer_pid
        s = self.session
        if not c.parent_id and not c.deleted:
            if c.target == "segment":
                out.segment_idx, out.changed, out.detached, out.at_seconds = place_segment(
                    c.anchor, c.quote, s.transcript, self.starts
                )
            elif c.target == "body":
                found = locate_body(s.summary or "", c.anchor)
                out.detached = found is None
                out.changed = bool(found and found[2])
            elif c.target == "notes":
                out.notes_range, out.changed = place_notes(c.id, c.quote, self.marks())
                out.detached = out.notes_range is None
            elif c.target in SECTIONS:
                text = _block_text(s.summary_data, c.target, c.target_id)
                out.detached = text is None
                out.changed = text is not None and _norm(text) != _norm(c.quote)
        out.can_edit, out.can_delete, out.can_resolve = self.rights(
            c, c.author_id if root_author is None else root_author
        )
        return out


def _session(app: AppContext, session_id: str) -> Session:
    session = app.repo.get(session_id)
    if session is None:
        raise access.Hidden("Session not found")
    return session


def threads(app: AppContext, session_id: str) -> SessionComments:
    """The meeting's threads, oldest first, each re-anchored to today's transcript and summary."""
    session = _session(app, session_id)
    meeting = _Meeting.of(app, session)
    comments = app.repo.list_comments(session_id)
    roots = {c.id: c for c in comments if not c.parent_id}
    replies: dict[str, list[Comment]] = {}
    out: list[CommentThread] = []
    for c in comments:
        if c.parent_id and c.parent_id in roots:
            replies.setdefault(c.parent_id, []).append(c)
    for c in comments:
        if c.parent_id and c.parent_id in roots:
            continue
        # A reply whose root is gone (it can come so through sync) is shown as its own thread.
        own = replies.get(c.id, [])
        if c.deleted and not own:
            continue
        out.append(
            CommentThread(
                root=meeting.view(c),
                replies=[meeting.view(r, c.author_id) for r in own],
            )
        )
    return SessionComments(threads=out, can_comment=True)


def _get(app: AppContext, session_id: str, comment_id: str) -> Comment:
    c = app.repo.get_comment(comment_id)
    if c is None or c.session_id != session_id:
        raise CommentNotFound("Comment not found")
    return c


def _publish(app: AppContext, session_id: str, mentions: list[str] | None = None) -> None:
    app.bus.publish({"type": "comments", "session_id": session_id, "mentions": mentions or []})


def _view_one(app: AppContext, session_id: str, comment_id: str) -> Comment:
    c = _get(app, session_id, comment_id)
    meeting = _Meeting.of(app, _session(app, session_id))
    root_author = c.author_id
    if c.parent_id:
        root = app.repo.get_comment(c.parent_id)
        root_author = root.author_id if root is not None else ""
    return meeting.view(c, root_author)


# ---- writing -----------------------------------------------------------------------------


COMMENT_ID = re.compile(r"[0-9a-f]{12}")


def create(app: AppContext, session_id: str, req: CommentCreate, comment_id: str = "") -> Comment:
    """A new comment or reply by the caller. `comment_id`: one the client minted (a contact's
    computer, phase 8: it shows the comment before the hub has it); an id already used is
    refused unless it is this same author's comment in this meeting (sent twice), which is
    returned as it is."""
    session = _session(app, session_id)
    text = req.text.strip()
    if not text:
        raise BadComment("A comment needs text")
    fields: dict = {}
    if comment_id:
        if not COMMENT_ID.fullmatch(comment_id):
            raise BadComment("A comment id is 12 hex characters")
        try:
            existing = app.repo.get_comment(comment_id)
        except access.Hidden:
            raise BadComment("That comment id is taken") from None
        if existing is not None:
            if existing.session_id == session_id and existing.author_id == _author(app, session)[0]:
                return _view_one(app, session_id, comment_id)
            raise BadComment("That comment id is taken")
    else:
        comment_id = uuid4().hex[:12]  # known first: a notes comment's mark carries it
    parts = shared_with_me.parts_of(app, session_id, session.shared_from)
    if req.parent_id:
        parent = app.repo.get_comment(req.parent_id)
        if parent is None or parent.session_id != session_id:
            raise BadComment("No such thread in this meeting")
        if parent.parent_id:
            raise BadComment("Reply to the thread's first comment")
        shared_with_me.require_part(parts, shared_with_me.COMMENT_PART.get(parent.target))
        fields["parent_id"] = parent.id
        target = parent.target
    else:
        shared_with_me.require_part(parts, shared_with_me.COMMENT_PART.get(req.target))
        if req.target == "notes" and not shared_notes.available():
            raise NotesNotShared("The notes are not shared on this server")
        target, target_id, anchor, quote = _anchor(session, req, comment_id)
        fields.update(target=target, target_id=target_id, anchor=anchor, quote=quote)
    author_id, author = _author(app, session)
    mentions = parse_mentions(app, text)
    comment_id = app.repo.add_comment(
        session_id,
        id=comment_id,
        text=text,
        author_id=author_id,
        author=author,
        mentions=mentions,
        **fields,
    )
    history.log(
        app,
        session_id,
        "commented",
        by=author,
        target=target,
        reply=bool(req.parent_id),
        excerpt=excerpt(text),
    )
    if session.shared_from:  # on its way to the owner (services/share_push.py)
        share_push.enqueue_comment(
            app, session_id, "create", {"comment_id": comment_id, **req.model_dump(mode="json")}
        )
    _publish(app, session_id, mentions)
    return _view_one(app, session_id, comment_id)


async def create_marked(app: AppContext, session_id: str, req: CommentCreate) -> Comment:
    """`create`, and for a new thread on the notes with `notes_ids`, the mark put on the range
    in the live document (off the event loop). A mark that cannot be put leaves the thread
    detached, with its quote."""
    c = create(app, session_id, req)
    if req.target != "notes" or req.parent_id:
        return c
    ids = [i.strip() for i in req.notes_ids]
    if len(ids) != 2 or not all(ids):
        return c
    author_id, author = _author(app, _session(app, session_id))
    if await shared_notes.mark_comment(app, session_id, c.id, ids[0], ids[1], author_id, author):
        share_push.notes_typed(app, session_id)  # my mark on a meeting shared with me goes back
        _publish(app, session_id)  # the ranges are known now
        return _view_one(app, session_id, c.id)
    return c


def _check(app: AppContext, session_id: str, c: Comment, what: str) -> None:
    meeting = _Meeting.of(app, _session(app, session_id))
    root_author, target = c.author_id, c.target
    if c.parent_id:
        root = app.repo.get_comment(c.parent_id)
        root_author = root.author_id if root is not None else ""
        target = root.target if root is not None else c.target
    meeting.require_part(target)
    can_edit, can_delete, can_resolve = meeting.rights(c, root_author)
    allowed = {"edit": can_edit, "delete": can_delete, "resolve": can_resolve}[what]
    if not allowed:
        raise access.Forbidden(
            {
                "edit": "Only its author can change a comment",
                "delete": "Only its author or the meeting's owner can delete a comment",
                "resolve": "Only the meeting's owner or the thread's author can resolve it",
            }[what]
        )


def edit(app: AppContext, session_id: str, comment_id: str, text: str) -> Comment:
    c = _get(app, session_id, comment_id)
    if c.deleted:
        raise CommentNotFound("Comment not found")
    _check(app, session_id, c, "edit")
    text = text.strip()
    if not text:
        raise BadComment("A comment needs text (delete it instead)")
    mentions = parse_mentions(app, text)
    app.repo.update_comment_text(comment_id, text, mentions)
    share_push.enqueue_comment(app, session_id, "edit", {"comment_id": comment_id, "text": text})
    _publish(app, session_id, [m for m in mentions if m not in c.mentions])
    return _view_one(app, session_id, comment_id)


def delete(app: AppContext, session_id: str, comment_id: str) -> None:
    c = _get(app, session_id, comment_id)
    if c.deleted:
        raise CommentNotFound("Comment not found")
    _check(app, session_id, c, "delete")
    app.repo.delete_comment(comment_id)
    if c.parent_id:
        # The last reply under a deleted first comment takes the empty thread with it.
        root = app.repo.get_comment(c.parent_id)
        if root is not None and root.deleted:
            if not any(x.parent_id == root.id for x in app.repo.list_comments(session_id)):
                app.repo.delete_comment(root.id)
    share_push.enqueue_comment(app, session_id, "delete", {"comment_id": comment_id})
    _publish(app, session_id)


def _resolve(app: AppContext, session_id: str, comment_id: str, resolved: bool) -> Comment:
    c = _get(app, session_id, comment_id)
    if c.parent_id:
        raise BadComment("Resolve the thread, not a reply")
    _check(app, session_id, c, "resolve")
    if (c.resolved_at is not None) == resolved:
        return _view_one(app, session_id, comment_id)
    _, by = _author(app, _session(app, session_id))
    app.repo.set_comment_resolved(comment_id, datetime.now() if resolved else None, by)
    history.log(
        app,
        session_id,
        "comment_resolved" if resolved else "comment_reopened",
        by=by,
        target=c.target,
        excerpt=excerpt(c.text or c.quote),
    )
    share_push.enqueue_comment(
        app, session_id, "resolve" if resolved else "reopen", {"comment_id": comment_id}
    )
    _publish(app, session_id)
    return _view_one(app, session_id, comment_id)


def resolve(app: AppContext, session_id: str, comment_id: str) -> Comment:
    return _resolve(app, session_id, comment_id, True)


def reopen(app: AppContext, session_id: str, comment_id: str) -> Comment:
    return _resolve(app, session_id, comment_id, False)


# ---- tasks and decisions across meetings -------------------------------------------------

OBJECT_TARGETS = {"task": "item", "decision": "decision"}


def _object(app: AppContext, kind: str, key: str) -> list[tuple[str, str, str, object]]:
    """The object's visible mentions, newest first, as (session_id, name, item id, mention)."""
    from . import decision_links, task_links

    if kind not in OBJECT_TARGETS:
        raise BadComment(f"Unknown kind {kind!r}")
    visible = app.repo.visible_ids()
    if kind == "task":
        obj = task_links.index(app.repo, visible).get(key)
        mentions = [] if obj is None else [(m.item_id, m) for m in obj.mentions]
    else:
        obj = decision_links.index(app.repo, visible).get(key)
        mentions = [] if obj is None else [(m.decision_id, m) for m in obj.mentions]
    if not mentions:
        raise CommentNotFound(f"No such {kind}")
    return [(m.session_id, m.session_name, item_id, m) for item_id, m in mentions]


def for_object(app: AppContext, kind: str, key: str) -> list[dict]:
    """The threads on a task or decision, grouped by the visible mention they were written on
    (newest meeting first): [{session_id, session_name, created_at, item_id, threads}]."""
    target = OBJECT_TARGETS.get(kind)
    out = []
    for session_id, name, item_id, m in _object(app, kind, key):
        if not item_id:
            continue
        found = [
            t
            for t in threads(app, session_id).threads
            if t.root.target == target and t.root.target_id == item_id
        ]
        out.append(
            {
                "session_id": session_id,
                "session_name": name,
                "created_at": m.created_at,
                "item_id": item_id,
                "threads": found,
            }
        )
    return out


def post_to_object(app: AppContext, kind: str, key: str, text: str, parent_id: str = "") -> Comment:
    """Comment on a task or decision: a new thread goes to the head mention (the newest
    visible meeting), a reply to the meeting of the thread it answers."""
    mentions = _object(app, kind, key)
    if parent_id:
        parent = app.repo.get_comment(parent_id)
        if parent is None or parent.session_id not in {m[0] for m in mentions}:
            raise CommentNotFound("No such thread on this " + kind)
        return create(app, parent.session_id, CommentCreate(parent_id=parent_id, text=text))
    session_id, _, item_id, _ = mentions[0]
    if not item_id:
        raise BadComment(f"This {kind} has no id to comment on")
    return create(
        app,
        session_id,
        CommentCreate(target=OBJECT_TARGETS[kind], target_id=item_id, text=text),
    )
