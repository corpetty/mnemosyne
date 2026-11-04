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
"""

from __future__ import annotations

import asyncio
import logging
import re
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

    # One writer at a time per meeting, `written` included (edit_blocking takes it again).
    with shared_notes.hub(app).lock(session_id):
        if _unchanged(app, session_id, compose):
            out = []
        else:
            _ensure_doc(app, session_id)
            out = shared_notes.edit_blocking(app, session_id, compose, shared_notes.SUMMARY_AUTHOR)
        if "wanted" in state:
            _record(app, session_id, state["wanted"], state["written"], state["wrote"])
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
