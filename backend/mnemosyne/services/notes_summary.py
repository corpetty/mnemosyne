"""The summary at the end of the meeting's live notes (docs/plans/2026-11-04-summaries-in-notes.md).

A "Summary" section mirrors summary_data block by block: `## Summary` (ref `sec:summary`), the
body markdown, then `### Decisions`, `### Action items` and `### Open questions` (refs
`sec:<name>`), one line per entry with ref `sum:<id>` (services/summary_ids.py). Decisions and
questions are `- text`; action items `- [ ] text (Owner) 📅 YYYY-MM-DD` (`[x]` when done; the
owner as text, never a person chip, or the line would be a notes item). Empty sub-sections are
left out. The headings' text is display only: the refs say what a line is.

`sync` writes it as SUMMARY_AUTHOR through shared_notes.edit_blocking, changing only the lines
whose rendering changed: each block's line is found by its ref wherever it is; a line of a block
the summary no longer has goes; a new block goes after its predecessor's line, else at the end
of its sub-section. Lines nobody's ref names (the person's, also those typed between our items)
are never touched, except the body: the lines between `## Summary` and the next ref'd line.

The summarize job runs it with `create`: missing headings are made (the whole section where the
Copilot section was, else at the end), and the copilot's lines go: one whose text is still what
the copilot wrote is deleted, an edited one stays as the person's line (its `cp:` ref cleared),
and the Copilot heading goes once no copilot line is left under it (the lines left under it
then join the person's notes above the Summary; a Copilot section after the Summary, from a
later part, keeps its heading while lines are left, or they would run into the Summary's last
sub-section). Every other write of the
summary (a tick, an edit, library sync) runs it without (api/context.py, debounced): a heading
someone deleted is not made again, nor a line of a block written before (deleting it is
deleting the item, for the read-back), and a line changed in the notes since we wrote it is left
for the read-back unless the summary changed it too.

What was written is kept in the summary doc's `notes` slot (`written`: the body and, per block,
its fields, our rendering `md` and the document's line after the write `shown`), so the
read-back tells the person's edits from ours and a rendering the document normalizes is not
written again and again.

Only the origin machine writes (library sync's union would double the section), never a meeting
shared with this person; nothing with `summary_in_notes` off, without the sidecar or a summary.

The other way, `read_back` (a shared_notes.AFTER_FOLD hook; `register` is called by
AppContext and on import): what people changed in the section is taken into the summary as
Obsidian edits are (services/summary_readback.py, by the author of the fold's frames): a
reworded, re-owned or re-dated line is the item edited (three-way when the summary moved too),
a deleted line the item deleted (tombstoned), a new bullet under a sub-section a new entry, the
lines under `## Summary` the body. A sub-section whose heading is gone changes nothing. A box
ticked or cleared ticks the task (item_flags: its other mentions, history, the vault note), in a
thread once the notes lock is free, since the fan-out may take another meeting's notes lock;
text edits take only this meeting's summary doc lock and the repository's. A task line with a
person chip is a notes item, never a summary item. The section is handed to
export/note_parse.py rebuilt from the refs (`_view`): canonical `## Decisions` / `## Action
Items` / `## Open Questions` headings, ` ^m-<id>` after each `sum:` line, only the top-level
bullets of each sub-section (so nothing typed there is ever taken for a heading); the body is
read here, never parsed.

Loops: `written` (with `shown`, the document's line) is what the read-back compares with, so a
line as we wrote it is never an edit, and after taking an edit it records the line as the
document has it (its `md` our rendering only when the line says what the summary now says, else
None, so the sync writes the merge result). Fold results stored while this machine writes the
section (or reads it back) are held and read back once after, against the new `written`
(`_holding`). A sync first folds what is pending, so the person's edits are read back before
a rendering could overwrite them.
"""

from __future__ import annotations

import asyncio
import logging
import re
import threading
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from ..models.session import ActionItem, CopilotAdded, SummaryData
from . import history, shared_notes
from . import notes_sections as ns
from .copilot_notes import CHIP, line_md
from .origin import is_foreign

if TYPE_CHECKING:
    from ..api.context import AppContext

logger = logging.getLogger(__name__)

ITEM_SECTIONS = (ns.DECISIONS, ns.ACTION_ITEMS, ns.OPEN_QUESTIONS)
HEADINGS = {
    ns.SUMMARY: "## Summary",
    ns.DECISIONS: "### Decisions",
    ns.ACTION_ITEMS: "### Action items",
    ns.OPEN_QUESTIONS: "### Open questions",
}
SLOT = "notes"  # the summary doc's slot (services/summary_doc.py update_slot)
_END = None  # an insertion point: the end of the document
_OWN_HEADING = re.compile(r"^\s{0,3}#{1,6}\s*summary\s*#*\s*$", re.IGNORECASE)


# ---- rendering (pure) ----------------------------------------------------------------------


def _clean(text: str | None) -> str:
    return " ".join((text or "").split())


def _trim(lines: list[str]) -> list[str]:
    """Without the blank lines at either end."""
    start, end = 0, len(lines)
    while start < end and not lines[start].strip():
        start += 1
    while end > start and not lines[end - 1].strip():
        end -= 1
    return lines[start:end]


def item_md(section: str, text: str, item: ActionItem | None = None) -> str:
    """One entry's line: `- text`, or for an action item `- [ ] text (Owner) 📅 date`."""
    text = _clean(text)
    if section != ns.ACTION_ITEMS or item is None:
        return f"- {text}"
    owner = f" ({_clean(item.owner)})" if _clean(item.owner) else ""
    due = f" 📅 {item.due.isoformat()}" if item.due else ""  # Obsidian Tasks, note_parse's _DUE
    return f"- [{'x' if item.done else ' '}] {text}{owner}{due}"


@dataclass
class Block:
    id: str
    section: str
    md: str
    text: str
    owner: str | None = None
    due: str | None = None
    done: bool | None = None


@dataclass
class Wanted:
    """The section as the summary says it: the body's lines and each sub-section's blocks."""

    body: list[str]
    blocks: dict[str, list[Block]] = field(default_factory=dict)

    @property
    def body_text(self) -> str:
        return "\n".join(self.body)

    def all(self) -> list[Block]:
        return [b for sec in ITEM_SECTIONS for b in self.blocks.get(sec, [])]


def wanted(data: SummaryData | None, body: str | None) -> Wanted:
    """The blocks of `data` (those with an id: a ref needs one) and the body's lines."""
    lines = _trim((body or "").replace("\r\n", "\n").split("\n"))
    if lines and _OWN_HEADING.match(lines[0]):  # a body that opens with "## Summary" itself
        lines = _trim(lines[1:])
    w = Wanted(lines)
    data = data or SummaryData()
    for sec in ITEM_SECTIONS:
        out: list[Block] = []
        if sec == ns.ACTION_ITEMS:
            for a in data.action_items:
                if a.id and _clean(a.text):
                    out.append(
                        Block(
                            a.id,
                            sec,
                            item_md(sec, a.text, a),
                            _clean(a.text),
                            _clean(a.owner) or None,
                            a.due.isoformat() if a.due else None,
                            a.done,
                        )
                    )
        else:
            texts = data.decisions if sec == ns.DECISIONS else data.open_questions
            ids = data.decision_ids if sec == ns.DECISIONS else data.question_ids
            for i, text in enumerate(texts):
                if i < len(ids) and ids[i] and _clean(text):
                    out.append(Block(ids[i], sec, item_md(sec, text), _clean(text)))
        w.blocks[sec] = out
    return w


def render(data: SummaryData | None, body: str | None) -> list[ns.New]:
    """The whole section as new lines with their refs (body lines have none); empty
    sub-sections left out."""
    w = wanted(data, body)
    out = [ns.New(HEADINGS[ns.SUMMARY], ns.sec_ref(ns.SUMMARY))]
    out += [ns.New(line) for line in w.body]
    for sec in ITEM_SECTIONS:
        if w.blocks[sec]:
            out.append(ns.New(HEADINGS[sec], ns.sec_ref(sec)))
            out += [ns.New(b.md, ns.sum_ref(b.id)) for b in w.blocks[sec]]
    return out


def _copilot_text(md: str) -> str:
    """A copilot line's text as compared at the summarize job: chip out, whitespace collapsed."""
    return " ".join(md.replace(CHIP, " ").split())


def _body_region(section: ns.Section) -> list[ns.Line]:
    """The body: the Summary heading's lines up to the next ref'd line, without trailing blank
    lines."""
    region: list[ns.Line] = []
    for x in section.lines:
        if x.ref:
            break
        region.append(x)
    while region and not region[-1].md.strip():
        region.pop()
    return region


def _shown_body(section: ns.Section | None) -> str | None:
    if section is None:
        return None
    return "\n".join(_trim([x.md for x in _body_region(section)]))


# ---- planning the edit (pure) --------------------------------------------------------------


def plan(
    s: ns.Sections,
    w: Wanted,
    written: dict,
    *,
    create: bool,
    copilot: dict[int, str] | None = None,
    wrote: set[str] | None = None,
) -> list[ns.Edit]:
    """The edits that bring the document's section (`s`) to `w`. `written`: the `notes` slot's
    `written` (what the last sync wrote and the document showed); `copilot`: n -> the line the
    copilot wrote, for `create`; `wrote` gets the ids of the blocks written ("" the body). []
    when nothing differs."""
    edits: list[ns.Edit] = []
    wrote = set() if wrote is None else wrote
    gone: set[int] = set()  # md_lines deleted
    blocks_w: dict = written.get("blocks") or {}

    def drop(x: ns.Line) -> None:
        edits.append(ns.delete(x.md_line))
        gone.add(x.md_line)

    def put(at: int | None, new: list[ns.New]) -> None:
        if new:
            edits.append(ns.append(*new) if at is _END else ns.insert_after(at, *new))

    def stale(rendered: str, shown: str, w: dict | None) -> bool:
        """The document's text needs our rendering."""
        if shown == rendered:
            return False
        if w and w.get("md") == rendered:
            if w.get("shown") == shown:
                return False  # it is ours, as the document renders it
            if not create:
                return False  # changed in the notes since, the summary did not: the read-back's
        return True

    by_id = {b.id: b for b in w.all()}
    adopted: set[int] = set()  # md_lines of the person's lines that became a block's
    first: dict[str, ns.Line] = {}
    for x in s.with_prefix(ns.SUM):
        block_id = ns.sum_id(x.ref)
        if block_id not in by_id:
            drop(x)  # a block the summary no longer has
        elif x.ref in first:
            edits.append(ns.retag(x, ""))  # a second line with the ref: the person's
        else:
            first[x.ref] = x

    cop = s.section(ns.COPILOT)
    if create:
        for x in s.with_prefix(ns.CP):
            mine = copilot.get(ns.cp_n(x.ref)) if copilot else None
            if mine is not None and _copilot_text(x.md) == _copilot_text(mine):
                drop(x)
            else:
                edits.append(ns.retag(x, ""))  # edited (or not known): the person's line now
        if cop is not None:
            # No copilot line is left under it: it goes, unless lines left under it (edited
            # ones, the person's) would then run into the section above, which is ours.
            left = [x for x in cop.lines if x.md.strip() and x.md_line not in gone]
            if not left or s.sections[0] is cop:
                drop(cop.heading)

    heads = {name: s.section(name) for name in (ns.SUMMARY, *ITEM_SECTIONS)}
    summary = heads[ns.SUMMARY]
    anchor: int | None = _END  # where a new Summary section goes
    if summary is None:
        present = [h.heading.md_line for name, h in heads.items() if h is not None]
        if present:
            anchor = min(present) - 1  # before the first sub-section left
        elif cop is not None:
            anchor = cop.last.md_line  # where the Copilot section was
    cursor: int | None
    if summary is None:
        if create:
            put(anchor, [ns.New(HEADINGS[ns.SUMMARY], ns.sec_ref(ns.SUMMARY))])
            put(anchor, [ns.New(line) for line in w.body])
            wrote.add("")
        cursor = anchor
    else:
        region = _body_region(summary)
        body_w = {"md": written.get("body"), "shown": written.get("body_shown")}
        if stale(w.body_text, _shown_body(summary) or "", body_w):
            for x in region:
                drop(x)
            put(summary.heading.md_line, [ns.New(line) for line in w.body])
            wrote.add("")
        cursor = summary.last.md_line

    for sec in ITEM_SECTIONS:
        head = heads[sec]
        blocks = w.blocks[sec]
        if head is None:
            if not create or not blocks:
                continue  # not mirrored until the next summarize job
            # Lines of its blocks left without their heading: it goes back above the first.
            orphans = [first[ns.sum_ref(b.id)].md_line for b in blocks if ns.sum_ref(b.id) in first]
            at = min(orphans) - 1 if orphans else cursor
            put(at, [ns.New(HEADINGS[sec], ns.sec_ref(sec))])
            end = at
        else:
            end = head.last.md_line
            cursor = end
        prev: int | None = None
        for b in blocks:
            x = first.get(ns.sum_ref(b.id))
            if x is not None:
                if stale(b.md, x.md, blocks_w.get(b.id)):
                    edits.append(ns.replace(x.md_line, b.md))
                    wrote.add(b.id)
                prev = x.md_line
            elif (y := _adoptable(head, blocks_w.get(b.id), adopted)) is not None:
                # A line the person typed that the read-back took as this block: it gets the ref.
                md = b.md if stale(b.md, y.md, blocks_w.get(b.id)) else y.md
                edits.append(ns.replace(y.md_line, md, ns.sum_ref(b.id)))
                adopted.add(y.md_line)
                wrote.add(b.id)
                prev = y.md_line
            elif create or b.id not in blocks_w:
                at = end if prev is None else prev
                put(at, [ns.New(b.md, ns.sum_ref(b.id))])
                wrote.add(b.id)
                prev = at
        if head is not None and not blocks:
            left = [x for x in head.lines if x.md.strip() and x.md_line not in gone]
            if not left:
                drop(head.heading)  # nothing left under it
    return edits


def _adoptable(head: ns.Section | None, w: dict | None, taken: set[int]) -> ns.Line | None:
    """The person's line (no ref) under `head` that the read-back recorded as this block's
    (`shown`), not taken by another block."""
    shown = w.get("shown") if w else None
    if head is None or not isinstance(shown, str):
        return None
    return next(
        (x for x in head.lines if x.ref is None and x.md_line not in taken and x.md == shown),
        None,
    )


# ---- the write -----------------------------------------------------------------------------


def _skip(app: AppContext, session_id: str):
    """The meeting when its notes get the summary here, else None."""
    if not app.settings.summary_in_notes:
        return None
    session = app.repo.get(session_id)
    if session is None or is_foreign(session):  # origin or shared_from: not ours to write
        return None
    if session.summary_data is None and not (session.summary or "").strip():
        return None
    return session if shared_notes.available() else None


def _copilot_lines(app: AppContext, session) -> dict[int, str]:
    """n -> the line the copilot wrote, from what it added (stored and live)."""
    added: dict[int, CopilotAdded] = {}
    for notes in (session.copilot_notes, app.copilot_notes.get(session.id)):
        for a in getattr(notes, "added", None) or []:
            added.setdefault(a.n, a)
    return {n: line_md(a) for n, a in added.items()}


def _written(app: AppContext, session_id: str) -> dict:
    raw = app.repo.get_summary_doc(session_id) or {}
    slot = raw.get(SLOT) if isinstance(raw.get(SLOT), dict) else {}
    written = slot.get("written")
    return written if isinstance(written, dict) else {}


def _ensure_doc(app: AppContext, session_id: str) -> None:
    """The summary doc (where `written` lives) and the items' ids, before anything is shown."""
    from .summary_doc import SummaryDocUnavailable, ensure_doc

    try:
        ensure_doc(app, session_id)
    except SummaryDocUnavailable:
        pass


def _record(app: AppContext, session_id: str, w: Wanted, before: dict, wrote: set[str]) -> None:
    """`written` after a sync: a block (or the body) the document shows as we render it, or
    that this sync wrote, is recorded with the line the document now has (`shown`: what it made
    of ours); any other keeps its old record (a line someone changed or deleted since, or one
    of a section not mirrored: the read-back's to take)."""
    from .summary_doc import update_slot

    source = app.repo.notes_source(session_id)
    notes = source[0] if source else ""
    s = ns.split(notes, app.repo.notes_refs(session_id, notes) or [])
    old = before.get("blocks") or {}
    blocks: dict[str, dict] = {}
    for b in w.all():
        x = s.find(ns.sum_ref(b.id))
        if x is None or (x.md != b.md and b.id not in wrote):
            if b.id in old:
                blocks[b.id] = old[b.id]
            continue
        blocks[b.id] = {
            "section": b.section,
            "text": b.text,
            "owner": b.owner,
            "due": b.due,
            "box": b.section == ns.ACTION_ITEMS,
            "done": b.done,
            "exact": True,
            "md": b.md,
            "shown": x.md,
        }
    body, shown = before.get("body"), before.get("body_shown")
    now = _shown_body(s.section(ns.SUMMARY))
    if now is not None and (now == w.body_text or "" in wrote):
        body, shown = w.body_text, now
    written = {"body": body, "body_exact": True, "body_shown": shown, "blocks": blocks}
    if written == before:
        return

    def change(slot: dict) -> None:
        slot["written"] = written

    update_slot(app, session_id, SLOT, change)


def _unchanged(app: AppContext, session_id: str, compose: shared_notes.Compose) -> bool:
    """Nothing to write, told from the stored fold alone (no sidecar run): the document is
    folded, nothing was written around it, and composing on it changes nothing."""
    repo = app.repo
    if not repo.has_notes_doc(session_id) or repo.has_unfolded_notes(session_id):
        return False
    if repo.notes_drift(session_id) is not None:
        return False
    source = repo.notes_source(session_id)
    if source is None:
        return False
    refs = repo.notes_refs(session_id, source[0])
    if refs is None:
        return False
    return compose(source[0], refs, []) is None


def sync(app: AppContext, session_id: str, *, create: bool) -> bool:
    """Bring the notes' Summary section in line with the summary (see the module doc); True
    when the document was written. From a thread, never the event loop. Never raises."""
    try:
        with shared_notes.unrestricted():
            return _sync(app, session_id, create)
    except Exception as e:  # the notes must not break a summary or a tick
        logger.warning("Could not write the summary into the notes of %s: %s", session_id, e)
        return False


def _sync(app: AppContext, session_id: str, create: bool) -> bool:
    if _skip(app, session_id) is None:
        return False
    state: dict = {}

    def compose(markdown: str, refs: list[dict], tasks: list[dict]):
        session = app.repo.get(session_id)
        if session is None:
            return None
        w = wanted(session.summary_data, session.summary)
        written = _written(app, session_id)
        copilot = _copilot_lines(app, session) if create else None
        wrote: set[str] = set()
        state.update(wanted=w, written=written, wrote=wrote)
        s = ns.split(markdown, refs)
        edits = plan(s, w, written, create=create, copilot=copilot, wrote=wrote)
        return ns.compose(s.lines, edits) if edits else None

    def write() -> list:
        if _unchanged(app, session_id, compose):
            out = []
        else:
            _ensure_doc(app, session_id)
            out = shared_notes.edit_blocking(app, session_id, compose, shared_notes.SUMMARY_AUTHOR)
        if "wanted" in state:
            _record(app, session_id, state["wanted"], state["written"], state["wrote"])
        return out

    # One writer at a time per meeting, `written` included (edit_blocking takes it again).
    with shared_notes.hub(app).lock(session_id):
        # What people typed and is not folded yet is read back first (the fold's hook), so
        # the rendering below never overwrites an edit the summary has not taken.
        shared_notes.fold_blocking(app, session_id)
        out = _holding(app, session_id, write)
    if out and create:
        history.log(app, session_id, "notes_summary_written")
    return bool(out)


async def after_summarize(app: AppContext, session_id: str) -> bool:
    """The summarize job's sync (headings made, the copilot's lines replaced), in a thread. A
    debounced sync the job's own writes scheduled is cancelled first: this one covers them."""
    cancel = getattr(app, "cancel_summary_notes", None)
    if cancel is not None:
        cancel(session_id)
    shared_notes._remember_loop(app)
    return await asyncio.to_thread(sync, app, session_id, create=True)


# ---- the read-back (notes -> summary) ------------------------------------------------------

# Fold results of a meeting stored while this machine writes its section or reads it back (the
# meeting's notes lock held, so only that thread adds here): read back once after (`_holding`).
_held: dict[str, list[tuple[dict, list[int], str | None]]] = {}

_CANON = {
    ns.DECISIONS: "## Decisions",
    ns.ACTION_ITEMS: "## Action Items",
    ns.OPEN_QUESTIONS: "## Open Questions",
}  # note_parse's headings
_BULLET = re.compile(r"^[-*+] ")  # a top-level bullet: note_parse's entries
_TASK = re.compile(r"^[-*+] \[[ xX]\]")


def _holding(app: AppContext, session_id: str, fn):
    """fn() with the meeting's fold results held (the read-back then compares them with a
    `written` that is not up to date yet); the last one held is read back after. Nested: fn()."""
    if session_id in _held:
        return fn()
    _held[session_id] = held = []
    try:
        result = fn()
    finally:
        _held.pop(session_id, None)
    if held:
        _held[session_id] = []  # what this read-back's own writes fold is its own
        try:
            _read_back(app, session_id, *held[-1])
        finally:
            _held.pop(session_id, None)
    return result


def read_back(
    app: AppContext, session_id: str, reply: dict, seqs: list[int], author: str | None
) -> None:
    """shared_notes.AFTER_FOLD: take what people changed in the notes' Summary section into
    the summary (see the module doc). Never raises."""
    if session_id in _held:
        _held[session_id].append((reply, seqs, author))
        return
    try:
        _holding(app, session_id, lambda: _read_back(app, session_id, reply, seqs, author))
    except Exception as e:  # the notes must not break a fold
        logger.warning("Could not read the summary back from the notes of %s: %s", session_id, e)
        logger.debug("Reading back the notes of %s failed", session_id, exc_info=True)


def _item_text(text: str) -> str:
    """An item's text as its line reads back (note_parse): what `item_md` rendered of it."""
    from .vault_sync import strip_links

    return strip_links(_clean(text)).strip()


def _body_text(summary: str) -> str:
    """The summary's body as the section shows it."""
    return wanted(None, summary).body_text


def _is_notes_item(md: str) -> bool:
    """A task line with a person chip: an action item of the notes (services/notes_items.py)."""
    from .notes_chips import task_line_parts

    return bool(_TASK.match(md)) and task_line_parts(md)[0] is not None


def _parse_line(sec: str, md: str, block_id: str, known: tuple[str | None, str]):
    """note_parse's entry for one of our lines, or None."""
    from ..export.note_parse import parse
    from .notes_chips import plain_notes

    alias = "0" * 8  # note_parse reads hex ids only
    note = parse(f"{_CANON[sec]}\n{plain_notes(md)} ^m-{alias}", {alias: known})
    entries = note.sections.get(sec) or []
    return entries[0] if entries else None


def _base(written: dict) -> dict:
    """`written` as summary_readback compares with it: per block what its line reads as (the
    document's line when it says otherwise than our rendering), the body as shown."""
    blocks: dict[str, dict] = {}
    for block_id, w in (written.get("blocks") or {}).items():
        if not isinstance(w, dict) or w.get("section") not in ITEM_SECTIONS:
            continue
        b = {
            "section": w["section"],
            "text": w.get("text") or "",
            "owner": w.get("owner"),
            "due": w.get("due"),
            "box": bool(w.get("box")),
            "exact": True,
        }
        line = w.get("shown") if isinstance(w.get("shown"), str) else w.get("md")
        if isinstance(line, str):
            e = _parse_line(w["section"], line, block_id, (b["owner"], b["text"]))
            if e is not None:
                b.update(text=e.text, owner=e.owner, due=e.due, box=e.box)
        blocks[block_id] = b
    body = written.get("body_shown")
    if body is None:
        body = written.get("body")
    return {"blocks": blocks, "body": body, "body_exact": True}


@dataclass
class _View:
    """The section as note_parse reads it, and each entry's line in the notes."""

    note: object  # export/note_parse.py Note
    lines: dict[int, ns.Line]  # id(entry) -> its line


def _view(s: ns.Sections, blocks: dict[str, dict], current: dict[str, str]) -> _View:
    """The adapter (C10 of the plan): each item sub-section whose heading is there, as note_parse
    expects it: its canonical `## ` heading, then only its top-level bullets: our lines (`sum:`
    ref) with ` ^m-<id>` appended, those of another sub-section's block left out with the
    person's lines after them (the lines of a sub-section whose heading went); the person's
    bullets as they are, except task lines with a person chip (notes items). Nothing else is
    passed on, so no line typed there can be taken for a heading. The body is the Summary
    heading's lines up to the first ref'd line (as written), read here."""
    from ..export.note_parse import parse
    from .notes_chips import plain_notes

    text: list[str] = []
    order: list[tuple[str, ns.Line, str]] = []
    alias: dict[str, str] = {}  # block id -> the hex id note_parse reads (`^m-` + hex only)
    for name in ITEM_SECTIONS:
        sec = s.section(name)
        if sec is None:
            continue
        text.append(_CANON[name])
        foreign = False
        for x in sec.lines:
            if x.ref:
                block_id = ns.sum_id(x.ref)
                home = (blocks.get(block_id) or {}).get("section") or current.get(block_id or "")
                if block_id is None or home is None:
                    continue  # not a summary block's line (or one of a block long gone)
                foreign = home != name
                if foreign:
                    continue
                md = f"{plain_notes(x.md)} ^m-{alias.setdefault(block_id, f'{len(alias):08x}')}"
            elif foreign or not _BULLET.match(x.md) or _is_notes_item(x.md):
                continue
            else:
                md = plain_notes(x.md)
            text.append(md)
            order.append((name, x, md))
    known = {alias[i]: (b["owner"], b["text"]) for i, b in blocks.items() if i in alias}
    note = parse("\n".join(text) + "\n", known) if text else parse("")
    real = {v: k for k, v in alias.items()}
    for entries in note.sections.values():
        for e in entries:
            e.id = real.get(e.id, "")
    summary = s.section(ns.SUMMARY)
    note.body = _shown_body(summary)
    lines: dict[int, ns.Line] = {}
    for name, entries in note.sections.items():
        rest = [(x, md) for sec, x, md in order if sec == name]
        k = 0
        for e in entries:
            while k < len(rest) and rest[k][1] != e.line:
                k += 1
            if k < len(rest):
                lines[id(e)] = rest[k][0]
                k += 1
    return _View(note, lines)


def _as_written(s: ns.Sections, written: dict) -> bool:
    """The section is as `written` recorded it: no line of it changed, went or came (the
    read-back's cheap exit, before any parsing)."""
    blocks = written.get("blocks") or {}
    summary = s.section(ns.SUMMARY)
    if summary is not None:
        body = written.get("body_shown")
        if body is None:
            body = written.get("body")
        if (_shown_body(summary) or "") != (body or ""):
            return False
    for name in ITEM_SECTIONS:
        sec = s.section(name)
        if sec is None:
            continue
        seen: set[str] = set()
        for x in sec.lines:
            block_id = ns.sum_id(x.ref)
            if block_id is not None:
                w = blocks.get(block_id)
                if not isinstance(w, dict):
                    return False
                shown = w.get("shown") if isinstance(w.get("shown"), str) else w.get("md")
                if x.md != shown:
                    return False
                seen.add(block_id)
            elif x.ref is None and _BULLET.match(x.md) and not _is_notes_item(x.md):
                return False  # a bullet of the person's: maybe a new entry
        for block_id, w in blocks.items():
            if isinstance(w, dict) and w.get("section") == name and block_id not in seen:
                return False
    return True


def _who(authors: list[str], frame, author: str | None) -> str:
    """Who made a change: the author of the frame that made the line, else the one author of
    the fold's frames, else the fold's own author, else "notes"."""
    if isinstance(frame, int) and not isinstance(frame, bool) and 0 <= frame < len(authors):
        if authors[frame]:
            return authors[frame]
    names = {a for a in authors if a}
    if len(names) == 1:
        return names.pop()
    return author or "notes"


def _read_back(
    app: AppContext, session_id: str, reply: dict, seqs: list[int], author: str | None
) -> bool:
    if not app.settings.summary_in_notes:
        return False
    with shared_notes.unrestricted():  # whoever may edit the notes was checked at their frame
        session = app.repo.get(session_id)
        if session is None or is_foreign(session):  # origin only; never a meeting shared with me
            return False
        if session.summary_data is None and not (session.summary or "").strip():
            return False
        markdown, refs = reply.get("markdown"), shared_notes.refs_of(reply)
        if not isinstance(markdown, str) or refs is None:
            return False
        written = _written(app, session_id)
        if not written.get("blocks") and written.get("body") is None:
            return False  # nothing of the summary written into these notes yet
        s = ns.split(markdown, refs)
        if _as_written(s, written):
            return False
        try:
            authors = app.repo.notes_op_authors(session_id, seqs)
        except Exception:
            logger.warning("Could not read the notes authors of %s", session_id, exc_info=True)
            authors = []
        tasks = reply.get("tasks")
        ticked = _take_ticks(
            app, session, tasks if isinstance(tasks, list) else [], written, authors, author
        )
        return _take_edits(app, session_id, s, written, _who(authors, None, author)) or ticked


def _take_ticks(
    app: AppContext,
    session,
    tasks: list,
    written: dict,
    authors: list[str],
    author: str | None,
) -> bool:
    """A box of our action item lines ticked or cleared in the notes ticks the task
    (item_flags: fan-out to its other mentions, the vault note). Only a box that differs from
    both what we wrote and what the item says now is a person's doing (a stale line is not).
    The ticks are only collected here: they run once the meeting's notes lock is free
    (`_later`), as the fan-out may take another meeting's. True when any was collected."""
    blocks = written.get("blocks") or {}
    items = session.summary_data.action_items if session.summary_data else []
    ticks: list[tuple[str, bool, str]] = []
    for t in tasks:
        if not isinstance(t, dict):
            continue
        block_id = ns.sum_id(t.get("ref"))
        w = blocks.get(block_id or "")
        if not isinstance(w, dict) or w.get("section") != ns.ACTION_ITEMS:
            continue
        item = next((a for a in items if a.id == block_id), None)
        done = bool(t.get("done"))
        if item is None or done == bool(w.get("done")) or done == item.done:
            continue
        ticks.append((item.id, done, _who(authors, t.get("frame"), author)))
    _later(app, session.id, ticks)
    return bool(ticks)


# Ticks read back and not run yet: (meeting, block id, done), so a fold meanwhile does not
# collect them again; the threads running them (`wait_ticks`).
_pending: set[tuple[str, str, bool]] = set()
_tick_threads: set[threading.Thread] = set()
_tick_guard = threading.Lock()


def _later(app: AppContext, session_id: str, ticks: list[tuple[str, bool, str]]) -> None:
    """Run the ticks in a thread of their own, which waits for the meeting's notes lock to be
    free (the fold that read them is over) and never holds it: no notes lock is held while
    the fan-out takes another meeting's (two people ticking one task in two meetings' notes
    would deadlock both)."""
    with _tick_guard:
        fresh = [t for t in ticks if (session_id, t[0], t[1]) not in _pending]
        _pending.update((session_id, block_id, done) for block_id, done, _ in fresh)
        if not fresh:
            return
        thread = threading.Thread(
            target=_run_ticks, args=(app, session_id, fresh), name="notes-ticks", daemon=True
        )
        _tick_threads.add(thread)
    thread.start()


def _run_ticks(app: AppContext, session_id: str, ticks: list[tuple[str, bool, str]]) -> None:
    try:
        with shared_notes.hub(app).lock(session_id):
            pass  # after the fold that read them, holding nothing while ticking
        with shared_notes.unrestricted():
            for block_id, done, by in ticks:
                try:
                    _tick(app, session_id, block_id, done, by)
                except Exception:
                    logger.warning("Could not take a tick from the notes of %s", session_id,
                                   exc_info=True)  # fmt: skip
    finally:
        with _tick_guard:
            for block_id, done, _ in ticks:
                _pending.discard((session_id, block_id, done))
            _tick_threads.discard(threading.current_thread())


def _tick(app: AppContext, session_id: str, block_id: str, done: bool, by: str) -> None:
    """One tick from the notes, unless the item says so already (or is gone)."""
    from . import item_flags

    session = app.repo.get(session_id)
    if session is None or is_foreign(session) or session.summary_data is None:
        return
    items = session.summary_data.action_items
    idx = next((i for i, a in enumerate(items) if a.id == block_id), None)
    if idx is None or items[idx].done == done:
        return
    kinds = ("task_done", "task_reopened")
    logged = sum(e["kind"] in kinds for e in app.repo.events(session_id))
    item = item_flags.set_item_flags(app, session, idx, done=done, by=by)
    if sum(e["kind"] in kinds for e in app.repo.events(session_id)) == logged:
        # item_flags logs only a tick by someone other than the owner (the signed-in person);
        # a tick in the notes is always someone's: theirs.
        history.log(app, session_id, kinds[0] if done else kinds[1], task=item.text, by=by)


def wait_ticks(timeout: float = 10.0) -> bool:
    """Wait for the ticks read back so far to have run (tests, shutdown). False on timeout."""
    deadline = time.monotonic() + timeout
    while True:
        with _tick_guard:
            threads = list(_tick_threads)
        if not threads:
            return True
        for thread in threads:
            thread.join(max(0.0, deadline - time.monotonic()))
        if time.monotonic() >= deadline:
            with _tick_guard:
                return not _tick_threads


def _take_edits(app: AppContext, session_id: str, s: ns.Sections, written: dict, by: str) -> bool:
    """Text edits (summary_readback.take, by `by`) and `written` brought up to the lines."""
    from . import summary_readback as rb
    from .summary_doc import SummaryDocUnavailable, ensure_doc, section
    from .summary_doc import _lock as doc_lock

    try:
        with doc_lock(session_id):
            if ensure_doc(app, session_id) is None:
                return False
            before = app.repo.get(session_id)
            if before is None:
                return False
            data = before.summary_data or SummaryData()
            current = {i: sec for sec in ITEM_SECTIONS for i, _ in section(data, sec) if i}
            base = _base(written)
            view = _view(s, base["blocks"], current)
            taken = rb.take(
                app, before, view.note, base, by=by, body_of=_body_text, text_of=_item_text
            )
            if not taken.taken:
                return False
            _remember_taken(app, session_id, view, taken)
    except SummaryDocUnavailable:
        return False
    logger.info("Edits in the notes taken into the summary of %s", session_id)
    rb.announce(app, before, "notes_summary_edits_taken", taken, by=by)
    return True


def _remember_taken(app: AppContext, session_id: str, view: _View, taken) -> None:
    """`written` after edits were taken: each changed, merged or new block as its line says now
    (`shown`: the line; `md`: our rendering when the line says what the summary now says, else
    None, so the next sync writes the merge result); deleted blocks forgotten; the body as
    shown."""
    from .summary_doc import update_slot

    now = wanted(taken.data, taken.body)
    by_id = {b.id: b for b in now.all()}
    edits = taken.edits

    def says(sec: str, e, b: Block) -> bool:
        if sec != ns.ACTION_ITEMS:
            return e.text == b.text
        return (e.text, e.owner, e.due, bool(e.done)) == (b.text, b.owner, b.due, bool(b.done))

    def change(slot: dict) -> None:
        written = slot.get("written")
        if not isinstance(written, dict):
            written = slot["written"] = {"body": None, "blocks": {}}
        wb = written.setdefault("blocks", {})
        for sec, rows in edits.sections.items():
            for block_id, e in rows:
                key = block_id or taken.ids.get(id(e), "")
                if not key or not (
                    key in edits.changed or key in edits.merges or id(e) in taken.ids
                ):
                    continue
                b, x = by_id.get(key), view.lines.get(id(e))
                if b is None or x is None:
                    wb.pop(key, None)
                    continue
                wb[key] = {
                    "section": sec,
                    "text": e.text,
                    "owner": e.owner,
                    "due": e.due,
                    "box": e.box,
                    "done": e.done if e.box else b.done,
                    "exact": True,
                    "md": b.md if says(sec, e, b) else None,
                    "shown": x.md,
                }
        for block_id in edits.deleted:
            wb.pop(block_id, None)
        if taken.body_changed:
            shown = view.note.body
            written["body"] = now.body_text if now.body_text == shown else None
            written["body_shown"] = shown
            written["body_exact"] = True

    update_slot(app, session_id, SLOT, change)


def register() -> None:
    """`read_back` among shared_notes.AFTER_FOLD (once)."""
    if read_back not in shared_notes.AFTER_FOLD:
        shared_notes.AFTER_FOLD.append(read_back)


register()
