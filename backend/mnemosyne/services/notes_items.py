"""Action items from the meeting notes (phase 11 of docs/plans/2026-10-12-hashseq-collaboration.md,
"Action items from the notes"; build order and corrections C13-C17 in
docs/plans/2026-10-25-phase-11-tasks.md).

A task line of the live notes (kind `task` / `task_done`) with a person chip is an action item of
the meeting; one without stays a checklist in the notes. Every answer of the notes sidecar lists
the task lines (`tasks`: line id, done, text, link marks, the request frame that made it); `apply`
turns that list into `Session.notes_items` at each place a fold result is stored: the three local
sites in services/shared_notes.py (`_fold_locked`, `_set_text_blocking`, `mark_comment_blocking`)
and library sync's `store_union` / `adopt` (services/sync_notes.py).

Derived from the line, again at every fold: `owner` (the first person chip's name), `text` (the
line without person and time chips, whitespace collapsed; nothing left = no item), `at` (the
first time chip: its part's offset on the meeting's timeline plus its seconds; a part the meeting
does not know = None) and `done` (`task_done`). Kept from the stored item: `follow`, `due`,
`issue_url`, `task_id`, its `id` and `by`.

An item belongs to its line by the line id (the element id of the line's `\\n`). `set_text` (API
writes, catch-ups of text written around the document) diffs lines and may give a line another
id; so a stored item whose line is gone hands its state to a new line with the same normalized
text (services/tasks.py `_norm`) and the same owner (case-insensitive), first such line in
document order, each stored item at most once. Close text (`same_item`) is not enough: a task
edited AND moved by one API write starts over rather than inherit a deleted task's link.

Ids (C14): 16 hex from the line id (its first 16, else the next 16-character window that is not
all digits: identifier redaction takes digit runs for numbers), else from a hash of it; never one
a stored item has. The item's task key is `<session>:<id>` (services/task_links.py `_key`).

History (C17): only local folds log (`log=True`): `notes_task_added` for a new item and
`task_done` / `task_reopened` for a tick typed in the notes, `by` the author of the frame the
sidecar names for the line (the latest that added its `\\n` or its task kind), else the site's
fallback. Lines that only "seed", "api" (catch-ups of text written around the document, e.g. by
library sync's text merge, which the other machine already logged) or a sync author made are
derived silently, as are sync unions and adoptions: each machine derives its own items and the
events travel with the meeting.
"""

from __future__ import annotations

import hashlib
import logging
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from .. import access
from ..models.session import NotesItem
from .notes_chips import parse_chip
from .tasks import _norm

if TYPE_CHECKING:
    from ..api.context import AppContext

logger = logging.getLogger(__name__)

ID_LEN = 16
_HEX = re.compile(r"[0-9a-f]+")
# Authors whose lines are derived without a history line (services/shared_notes.py
# SEED_AUTHOR / API_AUTHOR; sync_notes.author gives "sync (<peer>)").
_SILENT = ("seed", "api")
_SEPARATORS = " \t:-–—,;"  # left at an end of the text by a chip cut out there


@dataclass
class Line:
    """One task line with a person chip, as derived from the sidecar's `tasks`."""

    line: str
    done: bool
    text: str
    owner: str
    at: float | None
    frame: int | None


def item_id(line: str, taken: set[str]) -> str:
    """The item id for a line id (C14): 16 hex, not all digits, not in `taken`."""
    line = line.lower()
    windows = [line[i : i + ID_LEN] for i in range(0, len(line), ID_LEN)]
    n = 0
    while True:
        for w in windows:
            if len(w) == ID_LEN and _HEX.fullmatch(w) and not w.isdigit() and w not in taken:
                return w
        digest = hashlib.sha256(f"mnemosyne/notes-item/{line}/{n}".encode()).hexdigest()
        windows = [digest[i : i + ID_LEN] for i in range(0, len(digest), ID_LEN)]
        n += 1


def _int(x: Any) -> bool:
    return isinstance(x, int) and not isinstance(x, bool)


def lines_of(tasks: list, offsets: dict[int, float]) -> list[Line]:
    """The sidecar's task lines that are action items, in document order. `offsets`: part ->
    its start on the meeting's timeline (SessionRepository.part_offsets)."""
    out: list[Line] = []
    for t in tasks:
        if not isinstance(t, dict):
            continue
        line, text, links = t.get("line"), t.get("text"), t.get("links")
        if not (isinstance(line, str) and _HEX.fullmatch(line.lower()) and isinstance(text, str)):
            continue
        owner: str | None = None
        at: float | None = None
        timed = False
        cuts: list[tuple[int, int]] = []
        spans = [
            (s, e, u)
            for s, e, u in (x for x in (links or []) if isinstance(x, list) and len(x) == 3)
            if _int(s) and _int(e) and isinstance(u, str) and 0 <= s <= e
        ]
        for start, end, url in sorted(spans, key=lambda x: x[0]):
            chip = parse_chip(url)
            if chip is None or chip[0] == "meeting":  # a meeting chip's title stays in the text
                continue
            kind, value = chip
            cuts.append((start, end))
            if kind == "person" and owner is None:
                owner = value
            elif kind == "time" and not timed:
                timed = True
                part, seconds = (int(x) for x in value.split("/"))
                offset = offsets.get(part, 0.0 if part == 0 else None)
                at = None if offset is None else offset + seconds
        if owner is None:
            continue  # a checklist line, not an action item
        kept: list[str] = []
        pos = 0
        for start, end in cuts:
            if start > pos:
                kept.append(text[pos:start])
            pos = max(pos, end)
        kept.append(text[pos:])
        plain = " ".join("".join(kept).split())
        # "@Ada: send the deck" is "send the deck", "Send the deck — @Ada" "Send the deck":
        # separators a chip at either end leaves behind go with it.
        if cuts and not text[: cuts[0][0]].strip():
            plain = plain.lstrip(_SEPARATORS)
        if cuts and not text[max(e for _, e in cuts) :].strip():
            plain = plain.rstrip(_SEPARATORS)
        if not plain:
            continue
        frame = t.get("frame")
        out.append(
            Line(
                line=line.lower(),
                done=bool(t.get("done")),
                text=plain,
                owner=owner,
                at=at,
                frame=frame if _int(frame) else None,
            )
        )
    return out


def _same_owner(a: str | None, b: str | None) -> bool:
    return (a or "").strip().casefold() == (b or "").strip().casefold()


def _silent(author: str | None) -> bool:
    return author is not None and (author in _SILENT or author.startswith("sync ("))


@dataclass
class Change:
    """What one derivation did: the items as stored now, and what is new or ticked."""

    items: list[NotesItem]
    added: list[tuple[NotesItem, str | None]]  # new items, with who (None = not known)
    ticked: list[tuple[NotesItem, str | None]]  # items whose done changed, with who
    changed: bool


def derive(
    stored: list[NotesItem],
    lines: list[Line],
    authors: list[str] | None = None,
    fallback: str | None = None,
) -> Change:
    """The items for `lines`, carrying state from `stored` (see the module docstring). Pure."""

    def who(line: Line) -> str | None:
        if line.frame is not None and authors and 0 <= line.frame < len(authors):
            return authors[line.frame]
        return fallback

    by_line = {s.line: s for s in stored}
    live = {x.line for x in lines}
    result: list[NotesItem | None] = [None] * len(lines)
    claimed: set[str] = set()
    ticked: list[tuple[NotesItem, str | None]] = []

    def carry(j: int, s: NotesItem) -> None:
        line = lines[j]
        item = s.model_copy(
            update={
                "line": line.line,
                "text": line.text,
                "owner": line.owner,
                "at": line.at,
                "done": line.done,
            }
        )
        result[j] = item
        claimed.add(s.id)
        if s.done != line.done:
            ticked.append((item, who(line)))

    for j, line in enumerate(lines):
        s = by_line.get(line.line)
        if s is not None and s.id not in claimed:
            carry(j, s)
    orphans = [s for s in stored if s.line not in live]
    for j, line in enumerate(lines):
        if result[j] is not None:
            continue
        for s in orphans:
            if (
                s.id not in claimed
                and _same_owner(s.owner, line.owner)
                and _norm(s.text) == _norm(line.text)
            ):
                carry(j, s)
                break
    taken = {s.id for s in stored}
    added: list[tuple[NotesItem, str | None]] = []
    for j, line in enumerate(lines):
        if result[j] is not None:
            continue
        by = who(line)
        item = NotesItem(
            id=item_id(line.line, taken),
            line=line.line,
            text=line.text,
            owner=line.owner,
            at=line.at,
            done=line.done,
            by="" if by is None or _silent(by) else by,
        )
        taken.add(item.id)
        result[j] = item
        added.append((item, by))
    items = [r for r in result if r is not None]
    changed = [i.model_dump() for i in items] != [s.model_dump() for s in stored]
    return Change(items=items, added=added, ticked=ticked, changed=changed)


def unique_author(authors: list[str]) -> str | None:
    """The one author of these frames, None when there are several or none."""
    names = set(authors)
    return names.pop() if len(names) == 1 else None


def apply(
    app: AppContext,
    session_id: str,
    tasks: Any,
    *,
    authors: list[str] | None = None,
    fallback: str | None = None,
    log: bool = True,
) -> list[str]:
    """Derive the meeting's notes items from a sidecar answer's `tasks` and store them when they
    changed (no new version: the notes write beside it counts). `authors`: the author of each
    request frame, by index (the sidecar's `frame`); `fallback`: who made a line the sidecar
    names no frame for. `log`: a local fold (history lines, task linking for new items); False
    for library sync. Returns the new items' ids. Never raises: a failure is logged, the stored
    items stay."""
    if not isinstance(tasks, list):
        return []  # a sidecar that does not report task lines
    try:
        repo = app.repo
        stored = repo.notes_items(session_id)
        if stored is None:
            return []
        change = derive(stored, lines_of(tasks, repo.part_offsets(session_id)), authors, fallback)
        if not change.changed:
            return []
        repo.set_notes_items(session_id, change.items)
    except Exception:
        logger.warning("Could not derive the notes items of %s", session_id, exc_info=True)
        return []
    new = [item.id for item, _ in change.added]
    if not log:
        return new
    from . import history

    for item, by in change.added:
        if not _silent(by):
            history.log(
                app, session_id, "notes_task_added", task=item.text, owner=item.owner, by=by or ""
            )
    for item, by in change.ticked:
        if not _silent(by):
            kind = "task_done" if item.done else "task_reopened"
            history.log(app, session_id, kind, task=item.text, by=by or "")
    if new:
        link_new(app, session_id)
    return new


def link_new(app: AppContext, session_id: str) -> None:
    """Link the meeting's unlinked items to earlier open tasks, as a summary edit does
    (summary_edit.link_quietly, which honours `task_linking` and never raises), seeing what the
    meeting's owner sees on a team server (whoever's typing made the fold may see more or
    less). A meeting shared with me is never linked."""
    if app.settings.task_linking == "off":
        return
    try:
        if app.repo.notes_source(session_id) is None or app.repo.shared_from(session_id):
            return
        owner = app.repo.owner_of(session_id) or ""
        who = (
            access.Principal(owner, "", access.MEMBER) if app.settings.team_mode and owner else None
        )
        token = access.current.set(who)
        try:
            from .summary_edit import link_quietly

            link_quietly(app, session_id)
        finally:
            access.current.reset(token)
    except Exception:
        logger.warning("Linking the notes items of %s failed", session_id, exc_info=True)


# ---- state written from outside the notes (C15, C16 of docs/plans/2026-10-25-phase-11-tasks.md)

STATE_FIELDS = frozenset({"done", "follow", "due", "issue_url", "task_id"})
_KEEP: Any = object()


def find(app: AppContext, session_id: str, item_id: str) -> tuple[int, NotesItem] | None:
    """(position, item) of the meeting's notes item with this id, None for none."""
    for j, item in enumerate(app.repo.notes_items(session_id) or []):
        if item_id and item.id == item_id:
            return j, item
    return None


def _ticker(app: AppContext) -> tuple[str, str]:
    """(author id, name) for a frame the app writes for whoever acts: the signed-in person, else
    the microphone's name (as comments' marks, services/comments.py `_author`)."""
    p = access.principal()
    if p is not None:
        return p.user_id, p.name
    return "", (app.settings.local_speaker_name or "").strip() or "You"


def write_fields(
    app: AppContext,
    session_id: str,
    item_id: str,
    *,
    author: tuple[str, str] | None = None,
    **fields: Any,
) -> bool:
    """Set state fields (`done`, `follow`, `due`, `issue_url`, `task_id`) of one notes item: the
    one writer for every path that is not typing in the notes (the notes-items route, task
    fan-out, linking, reconcile; the vault read-back once it ticks notes items). A new version
    of the meeting (library sync carries the state). `done` also goes into the document: a
    `set_kind` frame by `author` ((id, name); default whoever acts), broadcast to open editors
    (shared_notes.set_kind_blocking), so the next fold derives the same value. The meeting's
    notes lock is held throughout, so a fold cannot write an older state over this one.

    INTERNAL: no access check, no history line, no bus event (the callers do those). True when
    the item exists. Raises shared_notes.NotesDocError when `done` could not be put into the
    document (then nothing changed), ValueError for another field."""
    from . import shared_notes

    bad = set(fields) - STATE_FIELDS
    if bad:
        raise ValueError(f"Cannot set notes item fields: {sorted(bad)}")
    repo = app.repo
    with shared_notes.hub(app).lock(session_id):
        found = find(app, session_id, item_id)
        if found is None:
            return False
        _, item = found
        if "done" not in fields or bool(fields["done"]) == item.done:
            return repo.set_notes_item_fields(session_id, item_id, **fields)
        done = bool(fields["done"])
        # The state first: the fold that set_kind runs then sees no tick of its own to log
        # (the caller logs it, once, as for a summary item).
        repo.set_notes_item_fields(session_id, item_id, **{**fields, "done": done})
        author_id, name = author or _ticker(app)
        try:
            shared_notes.set_kind_blocking(
                app, session_id, item.line, "task_done" if done else "task", author_id, name
            )
        except shared_notes.NotesDocError:
            repo.set_notes_item_fields(session_id, item_id, **{k: getattr(item, k) for k in fields})
            raise
        return True


def update(
    app: AppContext,
    session_id: str,
    item_id: str,
    *,
    done: bool | None = None,
    follow: bool | None = None,
    due: Any = _KEEP,
    by: str = "",
) -> tuple[int, NotesItem]:
    """The notes-items route: tick (`done`), star (`follow`) or set the deadline (`due`, None
    clears it) of a notes item, with the access rules of a summary item's (services/item_flags.py
    and routes/tasks.py): done and follow need read access and belong to the task (a linked
    item's other mentions follow, services/task_links.py, which logs the tick); due needs write
    access. On a meeting someone shared with me its notes items are read-only (access.Forbidden
    "Tick it in the notes"). Text and owner change in the notes only. Returns (position, item)
    as stored now. LookupError: no such (visible) meeting or item. Blocking (the sidecar)."""
    from . import history, task_links

    repo = app.repo
    if not repo.exists(session_id):
        raise LookupError("Session not found")
    if repo.shared_from(session_id):
        raise access.Forbidden("Tick it in the notes")
    found = find(app, session_id, item_id)
    if found is None:
        raise LookupError("No such action item")
    if due is not _KEEP:
        repo._check_write(session_id)  # an edit of the item, as for a summary item's due
    obj = task_links.object_of(repo, session_id, item_id, repo.visible_ids())
    if obj is None:
        raise LookupError("No such action item")
    _, item = found
    if done is not None and done != item.done:
        task_links.set_done(app, obj.key, done, by=by, acting=session_id)
    if follow is not None and follow != item.follow:
        task_links.set_follow(app, obj.key, follow, acting=session_id)
    if due is not _KEEP and due != item.due:
        write_fields(app, session_id, item_id, due=due)
        history.log(app, session_id, "task_edited", task=item.text, by=by)
    task_links._notify(app, {session_id})  # the one `session` event for this meeting
    found = find(app, session_id, item_id)
    if found is None:  # gone from the notes meanwhile
        raise LookupError("No such action item")
    return found
