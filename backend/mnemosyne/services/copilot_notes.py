"""The copilot's points in the meeting's live notes (docs/plans/2026-11-04-summaries-in-notes.md).

While recording, each new summary point, decision, action item or open question of the live
copilot (services/copilot.py) is added ONCE as a line under a "Copilot" heading at the end of
the notes, written as the copilot (shared_notes.COPILOT_AUTHOR) and never rewritten. A line
starts with the Copilot chip and has the shapes of src/lib/notes/copilotLines.ts: a bullet, a
`Decided: ` bullet, a task `text (owner)` (the owner as text: a person chip would make it a
notes item) and a `Question: ` bullet. Its ref is `cp:<n>` (services/notes_sections.py).

What was added is server state, `CopilotNotes.added`, not the document: an item is new when no
added entry of its kind is the same (services/tasks.py `same_item`), so a line someone reworded
or deleted is never added again. The heading is put back (at the end) when it is gone and there
is something new to add.

Only the meeting's origin writes these lines (library sync's union would double them), never a
meeting someone shared with this person; nothing without the sidecar or with
`copilot_to_notes` off.
"""

from __future__ import annotations

import asyncio
import logging
import re
from typing import TYPE_CHECKING

from ..models.session import CopilotAdded, CopilotNotes
from . import history, notes_sections, shared_notes
from .origin import is_foreign
from .tasks import same_item

if TYPE_CHECKING:
    from ..api.context import AppContext

logger = logging.getLogger(__name__)

HEADING = "## Copilot"
CHIP = "[Copilot](mnemosyne:by/copilot)"  # src/lib/notes/chips.ts byUrl("copilot")
# Characters notes-core's parser would read as inline markup; escaped so the line says them.
_INLINE = re.compile(r"([\\`*_~\[\]<&|])")


def _clean(text: str | None) -> str:
    return " ".join((text or "").split())


def _escape(text: str) -> str:
    return _INLINE.sub(r"\\\1", text)


def _items(notes: CopilotNotes) -> list[tuple[str, str, str | None]]:
    """(kind, text, owner) of every item of the notes, in the panel's order."""
    out: list[tuple[str, str, str | None]] = []
    out += [("summary", _clean(x), None) for x in notes.summary]
    out += [("decision", _clean(x), None) for x in notes.decisions]
    out += [("action", _clean(a.text), _clean(a.owner) or None) for a in notes.action_items]
    out += [("question", _clean(x), None) for x in notes.open_questions]
    return [x for x in out if x[1]]


def new_items(notes: CopilotNotes | None, added: list[CopilotAdded]) -> list[CopilotAdded]:
    """The items of `notes` that nothing in `added` (nor an earlier item of the same notes) of
    the same kind is `same_item` to, numbered on after the highest `n` of `added`."""
    if notes is None:
        return []
    seen = list(added)
    n = max((a.n for a in added), default=-1) + 1
    out = []
    for kind, text, owner in _items(notes):
        if any(a.kind == kind and same_item(a.text, text) for a in seen):
            continue
        item = CopilotAdded(kind=kind, text=text, owner=owner, n=n)  # type: ignore[arg-type]
        n += 1
        out.append(item)
        seen.append(item)
    return out


def line_md(item: CopilotAdded) -> str:
    """The item's line of markdown: the chip, then copilotLines.ts `copilotLine`'s shape."""
    text = _escape(_clean(item.text))
    if item.kind == "action":
        owner = _escape(_clean(item.owner))
        return f"- [ ] {CHIP} " + (f"{text} ({owner})" if owner else text)
    lead = {"decision": "Decided: ", "question": "Question: "}.get(item.kind, "")
    return f"- {CHIP} {lead}{text}"


def _compose(items: list[CopilotAdded]) -> shared_notes.Compose:
    """After the Copilot section's last copilot line, else right after its heading; with no
    such heading, a new one and the lines at the end of the document."""
    new = [notes_sections.New(line_md(i), notes_sections.cp_ref(i.n)) for i in items]

    def compose(markdown: str, refs: list[dict], tasks: list[dict]):
        s = notes_sections.split(markdown, refs)
        section = s.section(notes_sections.COPILOT)
        if section is None:
            heading = notes_sections.New(HEADING, notes_sections.sec_ref(notes_sections.COPILOT))
            edit = notes_sections.append(heading, *new)
        else:
            mine = [x for x in section.lines if notes_sections.cp_n(x.ref) is not None]
            at = mine[-1] if mine else section.heading
            edit = notes_sections.insert_after(at.md_line, *new)
        return notes_sections.compose(s.lines, [edit])

    return compose


def append(app: AppContext, session_id: str, items: list[CopilotAdded]) -> list[CopilotAdded]:
    """Write `items` into the meeting's notes as the copilot; returns those written ([] when
    nothing was: the setting is off, the meeting is foreign or shared with us, no sidecar, the
    document waits for library sync, or the write failed). From a thread, never the loop."""
    if not items or not app.settings.copilot_to_notes:
        return []
    session = app.sessions.get_session(session_id)
    if session is None or is_foreign(session):  # origin or shared_from: not ours to write
        return []
    if not shared_notes.available():
        return []
    try:
        out = shared_notes.edit_blocking(
            app, session_id, _compose(items), shared_notes.COPILOT_AUTHOR
        )
    except Exception as e:  # the notes must not stop the copilot
        logger.warning("Could not add the copilot's points to the notes of %s: %s", session_id, e)
        return []
    if not out:
        return []
    history.log(app, session_id, "copilot_notes_added", count=len(items))
    return list(items)


async def add_new(app: AppContext, session_id: str, notes: CopilotNotes) -> list[CopilotAdded]:
    """The items of `notes` not added yet, written (in a thread) and appended to
    `notes.added`; returns them."""
    items = new_items(notes, notes.added)
    if not items:
        return []
    shared_notes._remember_loop(app)
    written = await asyncio.to_thread(append, app, session_id, items)
    notes.added = [*notes.added, *written]
    return written
