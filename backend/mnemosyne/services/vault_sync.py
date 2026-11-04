"""Ticks come back from Obsidian: the checkboxes in the Action Items of the meeting notes we
wrote (frontmatter `source: mnemosyne`) win over our copy, and ticking an item in the app
rewrites that one line of its note, so the two never disagree on our side.

- `read_ticks` parses a note's Action Items (not "### Others": plain bullets, no boxes).
  `[x]`/`[X]` is done, `[ ]` open, any other status (Tasks' `[/]` In Progress, `[-]`
  Cancelled) is left alone.
- `apply_vault_ticks` runs before every export (api/routes/export.py `before_export`), so
  re-exporting never undoes a tick made in Obsidian, and from `sync_vault`, every few minutes
  while a vault is set, for notes whose mtime changed since the last pass.
- `write_tick` runs when an item is ticked in the app (PATCH action item).

A note that says an item is open while ours is done is only believed when the note was written
after the meeting last changed: notes exported before this existed, then ticked in the app, are
stale, and must not reopen what was finished. For a note with a record (`vault_notes`), the
record decides instead: a box counts as the user's change when its status differs from the one
we last wrote.

`write_note` is how export_session writes the meeting note: the user's edits in the vault are
merged with the new rendering (export/note_merge.py) instead of being overwritten. The summary's
own sections (Summary, Decisions, Action Items, Open Questions) are not merged as text: what the
user changed in them is taken into the summary first (`apply_vault_edits`, from `before_export`
and `sync_vault`), and the rendering's sections are spliced in whole.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import re
import threading
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import TYPE_CHECKING

from ..models.session import ActionItem, NotesItem, SummaryData
from . import history
from .tasks import same_item

if TYPE_CHECKING:
    from ..api.context import AppContext
    from ..models.session import Session

logger = logging.getLogger(__name__)

_BOX = re.compile(r"^(\s*[-*+] \[)(.)(\])(.*?)(\r?\n)?\Z")
_SECTION_END = re.compile(r"^#{1,2}\s")  # "# " or "## ": the next section
_SUBSECTION = re.compile(r"^###\s+(.*?)\s*$")
_SOURCE = re.compile(r"""^source:\s*["']?mnemosyne["']?\s*$""")
# Obsidian Tasks' emoji fields (priority, recurrence, dates, ids): our text ends before them.
_TASKS_FIELDS = re.compile("[\u23eb\U0001f53c\U0001f53d\u23ec\U0001f53a\U0001f501\U0001f4c5"
                           "\U0001f4c6\U0001f5d3\u23f3\u231b\U0001f6eb\u2795\u2705\u274c"
                           "\U0001f194\u26d4\U0001f3c1]")  # fmt: skip
_ISSUE = re.compile(r"\s*\[issue\]\([^)]*\)\s*$")
_LINKED_OWNER = re.compile(r"\s*\(\[\[[^\]]+\]\]\)\s*$")
_DONE_DATE = re.compile("\\s*\u2705\ufe0f?\\s*\\d{4}-\\d{2}-\\d{2}")
_BLOCK_ID = re.compile(r"\s+\^[\w-]+\s*$")
# The id we write (export/templates.py), last on the line; Tasks fields typed after it are no
# reason to lose it.
_OUR_BLOCK = re.compile(r"(?:^|\s)\^m-([0-9a-f]+)(?=\s|$)")
_WIKILINK = re.compile(r"\[\[([^\]|]*)(?:\|([^\]]*))?\]\]")

_lock = threading.Lock()  # an export and the periodic pass must not tick the same item twice
_warned: set[str] = set()  # problems with the merge sidecar already logged (once per process)


@dataclass
class Tick:
    """One checkbox line of a note's Action Items."""

    text: str  # the item's text, without the owner, issue link and Tasks fields we add
    done: bool | None  # None: another status ([/], [-]): left as it is
    line: int  # index in the note's lines (keepends)
    block_id: str = ""  # the `^m-<id>` at the end of the line: the item's id


def is_ours(text: str) -> bool:
    """A note we exported: its frontmatter says `source: mnemosyne`."""
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return False
    for line in lines[1:]:
        if line.strip() == "---":
            return False
        if _SOURCE.match(line.strip()):
            return True
    return False


def strip_links(text: str) -> str:
    """[[Note]] as Note and [[Note|Shown]] as Shown: links to the user's notes (export/links.py)
    are not part of what an item says."""
    return _WIKILINK.sub(lambda m: m.group(2) if m.group(2) is not None else m.group(1), text)


def item_text(rest: str) -> str:
    """What follows a checkbox, without the decorations: block id, Tasks fields (📅 due,
    ✅ done, ...), the issue link and a linked owner ` ([[Owner]])`; other [[links]] in the
    text are replaced by their words."""
    rest = _BLOCK_ID.sub("", rest)
    m = _TASKS_FIELDS.search(rest)
    if m:
        rest = rest[: m.start()]
    rest = _ISSUE.sub("", rest)
    rest = _LINKED_OWNER.sub("", rest)
    return strip_links(rest).strip()


def parse_ticks(text: str) -> list[Tick]:
    """The checkboxes of the "## Action Items" section (up to the next "## " heading), not
    those under "### Others"."""
    ticks: list[Tick] = []
    inside = others = False
    for n, line in enumerate(text.splitlines(keepends=True)):
        bare = line.rstrip("\r\n")
        if not inside:
            inside = bare.strip() == "## Action Items"
            continue
        if _SECTION_END.match(bare):
            break
        sub = _SUBSECTION.match(bare)
        if sub:
            others = sub.group(1) == "Others"
            continue
        if others:
            continue
        m = _BOX.match(line)
        if m is None:
            continue
        box = m.group(2)
        done = True if box in "xX" else False if box == " " else None
        rest = m.group(4)
        blocks = list(_OUR_BLOCK.finditer(rest))
        if blocks:
            rest = rest[: blocks[-1].start()] + rest[blocks[-1].end() :]
        ticks.append(Tick(item_text(rest), done, n, blocks[-1].group(1) if blocks else ""))
    return ticks


def read_ticks(note_path: Path) -> list[Tick]:
    """The Action Items checkboxes of a note we exported; [] when it is missing, unreadable or
    not ours."""
    try:
        text = Path(note_path).read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return []
    return parse_ticks(text) if is_ours(text) else []


def note_path(ctx: AppContext, session: Session, vault_path: str) -> Path:
    """Where export_session writes the meeting's note."""
    from ..export.obsidian import note_stem
    from .meeting_types import obsidian_folder

    folder = obsidian_folder(ctx.settings, session)
    return Path(vault_path).expanduser() / folder / f"{note_stem(session)}.md"


def _forms(text: str, item: ActionItem):
    yield text
    if item.owner:  # an owner not linked (a speaker label, or link_people off): "text (Ana)"
        suffix = f"({item.owner})"
        if text.endswith(suffix):
            yield text[: -len(suffix)].rstrip()


def _match(text: str, items: list[ActionItem], used: set[int]) -> int | None:
    """The first unused item with this text; failing that the first similar one (same_item)."""
    similar = None
    for i, item in enumerate(items):
        if i in used:
            continue
        wanted = strip_links(item.text)  # the note's text has none either
        for form in _forms(text, item):
            if form == wanted.strip():
                return i
            if similar is None and same_item(form, wanted):
                similar = i
    return similar


def pairs(ticks: list[Tick], items: list[ActionItem]) -> list[tuple[int, Tick]]:
    """(item index, tick) for the lines that are action items, each item at most once. A line
    whose block id is an item's id is that item, whatever its text now says; the others are
    matched by text."""
    used: set[int] = set()
    by_id = {}
    for tick in ticks:
        if tick.block_id:
            for i, item in enumerate(items):
                if item.id == tick.block_id and i not in by_id.values():
                    by_id[id(tick)] = i
                    used.add(i)
                    break
    out = []
    for tick in ticks:
        idx = by_id.get(id(tick))
        if idx is None:
            idx = _match(tick.text, items, used)
        if idx is not None:
            used.add(idx)
            out.append((idx, tick))
    return out


def notes_pairs(ticks: list[Tick], items: list[NotesItem]) -> list[tuple[int, Tick]]:
    """(notes item index, tick) for the lines whose block id is the id of an action item typed in
    the notes (services/notes_items.py), each item at most once. Never by text: an item's text
    is the notes' to change, and a line without our block id is not ours to guess at."""
    out: list[tuple[int, Tick]] = []
    used: set[int] = set()
    for tick in ticks:
        if not tick.block_id:
            continue
        idx = next(
            (i for i, n in enumerate(items) if n.id == tick.block_id and i not in used), None
        )
        if idx is not None:
            used.add(idx)
            out.append((idx, tick))
    return out


def apply_vault_ticks(ctx: AppContext, session: Session, vault_path: str) -> bool:
    """Take the done state of the meeting's action items from its note in the vault. True when
    anything changed (the caller reads the session again). A tick of an item that other
    meetings mention too (`task_id`) is the task's: it is fanned out to all of them, and their
    notes' lines rewritten, once the lock is free (`write_tick` takes it). A tick of an action
    item typed in the notes goes through `notes_items.write_fields` (into the notes document
    too), also once the lock is free. Never raises."""
    fan: list[tuple[str, int, bool]] = []
    typed: list[tuple[NotesItem, bool]] = []
    try:
        with _lock:
            changed = _apply(ctx, session, note_path(ctx, session, vault_path), fan, typed)
    except Exception:
        logger.warning("Could not read the ticks of session %s", session.id, exc_info=True)
        return False
    if typed:
        wrote = _write_notes_ticks(ctx, session.id, typed, fan)
        changed = changed or wrote
    for task_id, _, done in fan:
        _fan_out_tick(ctx, session.id, task_id, done, vault_path)
    return changed


def _write_notes_ticks(
    ctx: AppContext,
    session_id: str,
    ticks: list[tuple[NotesItem, bool]],
    fan: list[tuple[str, int, bool]],
) -> bool:
    """Put ticks read from the vault onto the meeting's notes items: a `done` write by
    "Obsidian" (the task line in the document changes too, shared with whoever has the notes
    open), a history line as for a summary item's. An item that cannot be written (no sidecar,
    the line gone) is left as it is. Never raises. True when anything was written."""
    from . import notes_items

    wrote = False
    for item, done in ticks:
        try:
            if not notes_items.write_fields(
                ctx, session_id, item.id, author=("", "Obsidian"), done=done
            ):
                continue
        except Exception as e:
            logger.warning("Could not take a tick of %s from Obsidian: %s", session_id, e)
            continue
        wrote = True
        history.log(
            ctx, session_id, "task_done" if done else "task_reopened",
            task=item.text, by="Obsidian",
        )  # fmt: skip
        if item.task_id:
            fan.append((item.task_id, 0, done))
    if wrote:
        logger.info("Ticks from Obsidian applied to the notes items of session %s", session_id)
        current = ctx.sessions.get_session(session_id)
        if current is not None:
            ctx.bus.publish(
                {"type": "session", "session_id": session_id, "status": current.status.value}
            )
    return wrote


def _fan_out_tick(
    ctx: AppContext, session_id: str, task_id: str, done: bool, vault_path: str | None
) -> None:
    """A tick read from a note counts for the whole task (services/task_links.py set_done, by
    "Obsidian"), and the other meetings' notes say so (those with a checkbox for it; the
    plain "continued in" bullets have none). Never raises, never takes `_lock` itself."""
    from . import task_links

    try:
        obj = task_links.index(ctx.repo).get(task_id)
        others = [m for m in (obj.mentions if obj else []) if m.session_id != session_id]
        if not others:
            return
        task_links.set_done(ctx, task_id, done, by="Obsidian")
        for m in others:
            other = ctx.sessions.get_session(m.session_id)
            if other is None:
                continue
            ctx.bus.publish(
                {"type": "session", "session_id": other.id, "status": other.status.value}
            )
            if other.origin:  # its note is read where it was recorded
                continue
            data = other.summary_data
            idx = (
                next(
                    (i for i, a in enumerate(data.action_items) if a.id and a.id == m.item_id), None
                )
                if data
                else None
            )
            if idx is not None:
                write_tick(ctx, other, idx, done, vault_path)
    except Exception:
        logger.warning("Could not fan out a tick of task %s", task_id, exc_info=True)


def _same_status(recorded: str, done: bool | None) -> bool:
    """Is the status we wrote (a box character) the state the note shows now (Tick.done)?"""
    return (True if recorded in "xX" else False if recorded == " " else None) == done


def _apply(
    ctx: AppContext,
    session: Session,
    path: Path,
    fan: list[tuple[str, int, bool]] | None = None,
    typed: list[tuple[NotesItem, bool]] | None = None,
) -> bool:
    """`fan` collects (task id, item index, done) of the changed items that belong to a task:
    the caller fans them out after releasing `_lock`. `typed` collects (notes item, done) of the
    ticks that belong to action items typed in the notes (a block id of `notes_items`): they
    are written after `_lock` is free (`_write_notes_ticks`), never here, and do not count in
    the return value."""
    from ..export.note_merge import box_at, box_positions  # note_merge imports this module

    try:
        written = datetime.fromtimestamp(path.stat().st_mtime)
        text = path.read_bytes().decode("utf-8")
    except (OSError, UnicodeDecodeError):
        return False
    ticks = parse_ticks(text) if is_ours(text) else []
    if not ticks:
        return False
    session = ctx.sessions.get_session(session.id) or session  # as it is now
    items = session.summary_data.action_items if session.summary_data else []
    record = ctx.repo.get_vault_note(session.id)
    if record is not None and record["path"] != str(path):
        record = None
    positions = box_positions(text) if record is not None else {}

    def theirs(tick: Tick, item: ActionItem) -> bool:
        """Is this box the user's doing, a change from what the item says now?"""
        if tick.done is None or tick.done == item.done:
            return False
        if record is not None:
            # The note is compared with what we wrote, not with the clock: the same status as
            # we wrote is not the user's doing (a stale note included).
            key, n = positions.get(tick.line, ("", 0))
            wrote = box_at(record["boxes"], key, n)
            if wrote is not None and _same_status(wrote[0], tick.done):
                return False
        elif not tick.done and written <= session.updated_at:
            return False  # a note older than our tick: stale, not a reopen
        return True

    # Lines that are action items typed in the notes are theirs, never a summary item's (by a
    # similar text): they go to notes_items.write_fields.
    noted = notes_pairs(ticks, session.notes_items)
    own = {id(t) for _, t in noted}
    if typed is not None:
        typed.extend(
            (session.notes_items[i], t.done)
            for i, t in noted
            if t.done is not None and theirs(t, session.notes_items[i])
        )
    changed = False
    for idx, tick in pairs([t for t in ticks if id(t) not in own], items):
        item = items[idx]
        if not theirs(tick, item):
            continue
        if not ctx.repo.set_action_item_done(session.id, idx, tick.done):
            continue
        item.done = tick.done
        history.log(
            ctx, session.id, "task_done" if tick.done else "task_reopened",
            task=item.text, by="Obsidian",
        )  # fmt: skip
        if fan is not None and item.task_id:
            fan.append((item.task_id, idx, tick.done))
        changed = True
    if changed:
        logger.info("Ticks from Obsidian applied to session %s", session.id)
        ctx.bus.publish(
            {"type": "session", "session_id": session.id, "status": session.status.value}
        )
    return changed


def _set_box(line: str, done: bool, today: date) -> str:
    m = _BOX.match(line)
    if m is None:
        return line
    head, box, close, rest, eol = m.groups()
    if done:
        if box in "xX":
            return line
        box = "x"
        if "\u2705" not in rest:
            body = _BLOCK_ID.sub("", rest.rstrip())
            block = rest.rstrip()[len(body) :]  # the block id stays the last thing on the line
            rest = f"{body} \u2705 {today.isoformat()}{block}"
    else:
        if box not in "xX":
            return line  # open already, or another status: left alone
        box = " "
        rest = _DONE_DATE.sub("", rest)
    return f"{head}{box}{close}{rest}{eol or ''}"


def _atomic_write(path: Path, data: bytes) -> None:
    """The user's note: written whole or not at all."""
    tmp = path.with_name(f".{path.name}.mnemosyne")
    tmp.write_bytes(data)
    tmp.replace(path)


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _keep_record(ctx: AppContext, session: Session, path: Path, before: bytes, new: str) -> None:
    """After write_tick: the note's record follows the new line, so the next export finds the
    vault as we left it. Only when the file was as we wrote it before; with the user's edits in
    it, the record stays and those edits are still found."""
    from ..export.note_merge import normalize

    record = ctx.repo.get_vault_note(session.id)
    if record is None or record["path"] != str(path) or _sha(before) != record["written_sha"]:
        return
    ctx.repo.put_vault_note(
        session.id, str(path), record["history"], normalize(new)[1], _sha(new.encode("utf-8"))
    )


def write_tick(
    ctx: AppContext, session: Session, idx: int, done: bool, vault_path: str | None = None
) -> bool:
    """An item was ticked (or reopened) in the app: rewrite its line in the meeting's note,
    when the note exists and is ours. Everything else in the note stays as it is. True when
    the note was written. Never raises."""
    try:
        vault = vault_path or ctx.settings.obsidian_vault_path
        items = session.summary_data.action_items if session.summary_data else []
        if not vault or not 0 <= idx < len(items):
            return False
        path = note_path(ctx, session, vault)
        with _lock:
            try:
                raw = path.read_bytes()
                text = raw.decode("utf-8")
            except (OSError, UnicodeDecodeError):
                return False
            if not is_ours(text):
                return False
            tick = next((t for i, t in pairs(parse_ticks(text), items) if i == idx), None)
            if tick is None:
                return False
            lines = text.splitlines(keepends=True)
            line = _set_box(lines[tick.line], done, date.today())
            if line == lines[tick.line]:
                return False
            lines[tick.line] = line
            written = "".join(lines).encode("utf-8")
            _atomic_write(path, written)
            ctx.vault_seen[str(path)] = path.stat().st_mtime_ns  # our own write: nothing new
            try:
                _keep_record(ctx, session, path, raw, written.decode("utf-8"))
            except Exception:
                logger.warning("Could not update the record of %s", session.id, exc_info=True)
        return True
    except Exception:
        logger.warning("Could not write a tick to the note of %s", session.id, exc_info=True)
        return False


def _warn_once(key: str, message: str, *args) -> None:
    if key not in _warned:
        _warned.add(key)
        logger.warning(message, *args)


_MERGED, _FRESH, _PLAIN = "merged", "fresh", "plain"


def write_note(ctx: AppContext, session: Session, path: Path, app_text: str) -> None:
    """Write a meeting's note (`app_text`, as rendered) to `path`, keeping what the user
    changed in it since we last wrote it: the two are merged by the mnemosyne-link sidecar
    (export/note_merge.py) against the history stored in `vault_notes`. Without a record, a
    working sidecar or a note of ours, the note is simply written, as it always was. Never
    lets the merge break the export."""
    with _lock:
        try:
            outcome = _merge_note(ctx, session, path, app_text)
        except Exception as e:
            _warn_once("merge", "Keeping the user's edits to Obsidian notes failed: %s", e)
            logger.debug("Merging the note of %s failed", session.id, exc_info=True)
            outcome = _PLAIN
        if outcome != _MERGED:
            path.write_text(app_text, encoding="utf-8")
            # Whatever history there was does not describe the file any more.
            if _forget(ctx, session) and outcome == _FRESH:
                _start_record(ctx, session, path, app_text)
        _remember_written(ctx, session, path, app_text)


def _forget(ctx: AppContext, session: Session) -> bool:
    try:
        ctx.repo.delete_vault_note(session.id)
        return True
    except Exception:
        logger.warning("Could not forget the record of %s", session.id, exc_info=True)
        return False


def _start_record(ctx: AppContext, session: Session, path: Path, app_text: str) -> None:
    """The file was just written from `app_text` alone: a new history starting there. Without
    a working sidecar there is no record."""
    from ..export.note_merge import merge, normalize
    from ..export.note_parse import cut
    from .link import find_binary

    try:
        binary = find_binary()
        if binary is not None:
            rest = cut(normalize(app_text)[0], _in_body(ctx, session))[0]
            history_bytes, _ = merge(binary, None, rest, None)
            _store(ctx, session, path, history_bytes, app_text.encode("utf-8"))
    except Exception as e:
        _warn_once("merge", "Keeping the user's edits to Obsidian notes failed: %s", e)
        _forget(ctx, session)


def _store(ctx: AppContext, session: Session, path: Path, history_bytes: bytes, data: bytes):
    """Remember what was written to `path`: its merge history, boxes and hash."""
    from ..export.note_merge import normalize

    boxes = normalize(data.decode("utf-8"))[1]
    ctx.repo.put_vault_note(session.id, str(path), history_bytes, boxes, _sha(data))
    ctx.vault_seen[str(path)] = path.stat().st_mtime_ns  # our own write: nothing new


def _merge_note(ctx: AppContext, session: Session, path: Path, app_text: str) -> str:
    """The merging write of write_note: _MERGED when the note was written; _FRESH when the
    caller writes `app_text` and starts a history (no record, or one for another file);
    _PLAIN when it writes `app_text` and keeps no record (not our note, no sidecar)."""
    from ..export.note_merge import lines_both_changed, merge, normalize, restore
    from ..export.note_parse import cut, splice
    from .link import find_binary

    raw = text = None
    try:
        raw = path.read_bytes()
        text = raw.decode("utf-8")
    except (OSError, UnicodeDecodeError):
        pass
    if raw is not None and (text is None or not is_ours(text)):
        return _PLAIN  # someone else's file: overwritten as before
    binary = find_binary()
    if binary is None:
        _warn_once("missing", "mnemosyne-link not found: Obsidian notes are overwritten")
        return _PLAIN
    record = ctx.repo.get_vault_note(session.id)
    if record is None or record["path"] != str(path):
        return _FRESH
    # The summary's sections are not merged as text: what the user changed in them was taken
    # into the summary first (apply_vault_edits), so the rendering's are the merge.
    in_body = _in_body(ctx, session)
    app_norm, app_boxes = normalize(app_text)
    app_rest, app_sections = cut(app_norm, in_body)
    vault_norm, vault_boxes = normalize(text) if text is not None else (None, {})
    changed = raw is not None and _sha(raw) != record["written_sha"]
    vault_rest = cut(vault_norm, in_body)[0] if changed and vault_norm is not None else None
    history_bytes, merged = merge(binary, record["history"], app_rest, vault_rest)
    final = restore(splice(merged, app_sections), app_boxes, vault_boxes)
    data = final.encode("utf-8")
    _atomic_write(path, data)
    try:
        _store(ctx, session, path, history_bytes, data)
    except Exception:
        _forget(ctx, session)
        logger.warning("Could not record the note of %s", session.id, exc_info=True)
    if changed:
        history.log(
            ctx, session.id, "note_merged",
            both=lines_both_changed(normalize(final)[0], app_norm, vault_norm or ""),
        )  # fmt: skip
    return _MERGED


# ---- the summary's sections, read back -----------------------------------------------------
#
# The Summary, Decisions, Action Items and Open Questions of a note are a projection of the
# summary doc (services/summary_doc.py): an export renders them whole, and what the user changed
# in them is taken into the summary before (apply_vault_edits), not merged as text. The doc's
# `vault` keeps what the last export wrote of them (`written`, per block, without [[links]]) and
# the lines the user added under items (`annex`, rendered back by export/templates.py). Working
# out and taking the edits is services/summary_readback.py, shared with the live notes' Summary
# section (services/notes_summary.py).


def _doc_vault(ctx: AppContext, session_id: str) -> dict:
    raw = ctx.repo.get_summary_doc(session_id) or {}
    vault = raw.get("vault") or {}
    return vault if isinstance(vault, dict) else {}


def vault_annex(ctx: AppContext, session_id: str) -> dict[str, list[str]]:
    """The lines the user added under the summary's items in the meeting's note, by item id
    (or "@<section>"), for the rendering. {} when there are none or no doc. Never raises."""
    try:
        annex = _doc_vault(ctx, session_id).get("annex") or {}
        return {k: list(v) for k, v in annex.items() if isinstance(v, list)}
    except Exception:
        logger.warning("Could not read the note annex of %s", session_id, exc_info=True)
        return {}


def _in_body(ctx: AppContext, session: Session) -> set[str]:
    """Headings of the summary body (now and as last written): they stay in its section."""
    from ..export.note_parse import body_headings

    try:
        written = (_doc_vault(ctx, session.id).get("written") or {}).get("body")
    except Exception:
        written = None
    return body_headings(session.summary, written)


def _update_vault(ctx: AppContext, session_id: str, change) -> None:
    """change(vault) on the doc's `vault`, stored (nothing when there is no doc)."""
    from .summary_doc import _lock as doc_lock

    with doc_lock(session_id):
        raw = ctx.repo.get_summary_doc(session_id)
        if raw is None:
            return
        vault = raw.get("vault")
        if not isinstance(vault, dict):
            vault = {"written": {"body": None, "blocks": {}}, "annex": {}}
        change(vault)
        raw["vault"] = vault
        ctx.repo.put_summary_doc(session_id, raw)


def _remember_written(ctx: AppContext, session: Session, path: Path, app_text: str) -> None:
    """After a note was written: what it says of each block of the summary, for the next
    apply_vault_edits to tell the user's changes from ours. Never raises."""
    from ..export.note_parse import body_text, parse
    from . import summary_readback as rb
    from .summary_doc import SummaryDocUnavailable, ensure_doc

    try:
        if session.summary_data is None and not session.summary:
            return
        if ensure_doc(ctx, session.id) is None:
            return
        known = rb.known(session.summary_data)
        note = parse(app_text, known, _in_body(ctx, session))
        blocks = {
            e.id: rb.written_entry(sec, e, e.id in known and known[e.id][1] == e.text)
            for sec, entries in note.sections.items()
            for e in entries
            if e.id
        }
        written = {
            "recorded": True,
            "path": str(path),
            "body": note.body,
            "body_exact": note.body == body_text(session.summary),
            "blocks": blocks,
        }

        def change(vault: dict) -> None:
            vault["written"] = written

        _update_vault(ctx, session.id, change)
    except SummaryDocUnavailable:
        pass
    except Exception:
        logger.warning("Could not remember the note of %s", session.id, exc_info=True)


def apply_vault_edits(ctx: AppContext, session: Session, vault_path: str) -> bool:
    """Take what the user changed in the summary's sections of the meeting's note into the
    summary (services/summary_doc.py apply_human, by "Obsidian"): reworded, re-owned, re-dated,
    added and deleted items, and the body. Lines that are not items are kept as the note's
    annex. True when the summary changed (the caller reads the session again). Never raises;
    nothing without a summary doc and a record of what the last export wrote."""
    from .link import find_binary
    from .summary_doc import SummaryDocUnavailable

    try:
        with _lock:
            if find_binary() is not None:  # without it, nothing comes back: no ids needed
                _ensure_ids(ctx, session.id)
            return _take_edits(ctx, session, note_path(ctx, session, vault_path))
    except SummaryDocUnavailable:
        return False
    except Exception:
        logger.warning("Could not take the note edits of %s", session.id, exc_info=True)
        return False


def _ensure_ids(ctx: AppContext, session_id: str) -> None:
    """The summary's items get their ids before a note shows them (as `^m-<id>`)."""
    from .summary_doc import _ids_of
    from .summary_doc import _lock as doc_lock
    from .summary_ids import ensure_ids

    with doc_lock(session_id):
        session = ctx.repo.get(session_id)
        data = session.summary_data if session is not None else None
        if data is None:
            return
        fixed = ensure_ids(data.model_copy(deep=True))
        if _ids_of(fixed) != _ids_of(data):
            ctx.repo.update_fields(session_id, summary_data=fixed)


def _take_edits(ctx: AppContext, session: Session, path: Path) -> bool:
    from ..export.note_parse import parse
    from . import summary_readback as rb
    from .summary_doc import SECTIONS, ensure_doc, section
    from .summary_doc import _lock as doc_lock

    try:
        raw = path.read_bytes()
        text = raw.decode("utf-8")
    except (OSError, UnicodeDecodeError):
        return False
    if not is_ours(text):
        return False
    record = ctx.repo.get_vault_note(session.id)
    if record is not None and record["path"] == str(path) and _sha(raw) == record["written_sha"]:
        return False  # the note is as we wrote it
    sid = session.id
    with doc_lock(sid):
        if ensure_doc(ctx, sid) is None:
            return False
        before = ctx.repo.get(sid)
        if before is None:
            return False
        data = before.summary_data or SummaryData()
        written = _doc_vault(ctx, sid).get("written") or {}
        blocks = written.get("blocks") or {}
        if not written.get("recorded") or written.get("path") != str(path):
            blocks = {}  # nothing to compare with: only the annex is kept
        known = {i: (w["owner"], w["text"]) for i, w in blocks.items()}
        note = parse(text, known or rb.known(data), _in_body(ctx, before))
        if not blocks:
            ids = {i for s in SECTIONS for i, _ in section(data, s)}
            for entries in note.sections.values():
                for e in entries:
                    if e.id and e.id not in ids:
                        e.id = ""
            edits = rb.empty_edits()
            for sec, entries in note.sections.items():
                edits.sections[sec] = [(e.id, e) for e in entries]
            annex = rb.annex(note, edits, {})
            _update_vault(ctx, sid, lambda v: v.__setitem__("annex", annex))
            return False
        taken = rb.take(
            ctx, before, note, written, by="Obsidian", redact=ctx.settings.redact_exports
        )
        edits, ids = taken.edits, taken.ids
        annex = rb.annex(note, edits, ids)

        def change(vault: dict) -> None:
            vault["annex"] = annex
            if not taken.taken:
                return
            w = vault.setdefault("written", {})
            wb = w.setdefault("blocks", {})
            for sec, rows in edits.sections.items():
                for block_id, e in rows:
                    key = block_id or ids.get(id(e), "")
                    if key and (key in edits.changed or key in edits.merges or id(e) in ids):
                        wb[key] = rb.written_entry(sec, e)
            for block_id in edits.deleted:
                wb.pop(block_id, None)
            if taken.body_changed:
                w["body"] = note.body

        _update_vault(ctx, sid, change)
    if not taken.taken:
        return False
    logger.info("Edits from Obsidian taken into session %s", sid)
    rb.announce(ctx, before, "note_edits_taken", taken)
    return True


def sync_vault(ctx: AppContext) -> int:
    """One pass: apply the ticks and summary edits of every summarized meeting's note that
    changed since the last pass (by mtime, remembered in ctx.vault_seen); notes are never
    written here. Returns the meetings changed."""
    from ..export.obsidian import note_stem
    from .meeting_types import available_types

    st = ctx.settings
    vault = st.obsidian_vault_path
    if not vault or ctx.locked:
        return 0
    root = Path(vault).expanduser()
    if not root.is_dir():
        return 0
    folders = {st.obsidian_subfolder} | {
        t.obsidian_folder for t in available_types(st) if t.obsidian_folder
    }
    changed = 0
    for summary in ctx.repo.list_summaries():
        if not summary.has_summary or summary.origin or summary.shared_from:
            continue  # the computer that recorded a meeting reads its note (the owner's hub)
        stem = note_stem(summary)  # name and date are all it needs
        for folder in folders:
            path = root / folder / f"{stem}.md"
            try:
                mtime = path.stat().st_mtime_ns
            except OSError:
                continue
            key = str(path)
            if ctx.vault_seen.get(key) == mtime:
                continue
            ctx.vault_seen[key] = mtime
            session = ctx.sessions.get_session(summary.id)
            if session is None or note_path(ctx, session, vault) != path:
                continue  # another meeting's folder, or no longer there
            ticked = apply_vault_ticks(ctx, session, vault)
            # Edits to the summary's sections too; the note is only written by an export.
            if apply_vault_edits(ctx, session, vault) or ticked:
                changed += 1
    return changed


async def sync_loop(ctx: AppContext, interval: float) -> None:
    """sync_vault every `interval` seconds, started with the app (AppContext)."""
    while True:
        await asyncio.sleep(interval)
        try:
            await asyncio.to_thread(sync_vault, ctx)
        except Exception:
            logger.exception("Reading ticks from the Obsidian vault failed")
