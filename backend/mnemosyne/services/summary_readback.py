"""The summary's sections read back into the summary: the core shared by the meeting's Obsidian
note (services/vault_sync.py `apply_vault_edits`) and the Summary section of its live notes
(services/notes_summary.py `read_back`).

Both are projections of the summary doc (services/summary_doc.py): what the person changed in
them is taken into the summary as a hand edit (`apply_human`), never merged as text. The caller
parses its projection into an export/note_parse.py `Note` (sections of entries with the ids of
the blocks they show) and keeps a record of what it last wrote (`written`: per block its
section, text, owner, due date and box, and the body). `take` works out what changed against
that record:

- a line whose text, owner or due date differs from what was written is the item edited; when
  the summary changed the item too since, the three are merged by the sidecar (`base`);
- a line without an id (or one never written) is the written block of its section with the same
  text, then a similar one (tasks.same_item); failing that a new entry;
- a written block whose line is gone is deleted (tombstoned by apply_human), but only in a
  section the projection still shows: a section whose heading is gone changes nothing;
- the body likewise, three-way when the summary's body moved too.

The summary data passed to apply_human is the whole summary with only those changes (C13 of
docs/plans/2026-11-04-summaries-in-notes.md): items of sections the projection does not show
are kept.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date
from typing import TYPE_CHECKING

from ..models.session import ActionItem, SummaryData
from . import history
from .tasks import same_item
from .vault_sync import strip_links

if TYPE_CHECKING:
    from ..api.context import AppContext
    from ..models.session import Session

logger = logging.getLogger(__name__)

# summarization/privacy.py's markers: text holding one was redacted, it never goes back.
_REDACTED = re.compile(r"\[(?:SSN|routing number|date of birth|(?:account|card) ••\d{0,4})\]")

_warned: set[str] = set()  # problems with the merge sidecar already logged (once per process)


def _warn_once(key: str, message: str, *args) -> None:
    if key not in _warned:
        _warned.add(key)
        logger.warning(message, *args)


def _vault_text(text: str) -> str:
    """An item's text as an Obsidian note shows it: [[links]] as their words."""
    return strip_links(text).strip()


def known(data: SummaryData | None) -> dict[str, tuple[str | None, str]]:
    """id -> (owner, text) of the summary's blocks, as a note shows them (note_parse's `known`)."""
    from .summary_doc import SECTIONS, section

    if data is None:
        return {}
    owners = {a.id: a.owner or None for a in data.action_items}
    return {
        i: (owners.get(i), strip_links(t).strip())
        for s in SECTIONS
        for i, t in section(data, s)
        if i
    }


def written_entry(sec: str, e, exact: bool = True) -> dict:
    """What a note said of a block. `exact`: its text is the summary's as it was rendered (not
    redacted, nothing cut off): only such a block may be changed from the note."""
    return {
        "section": sec,
        "text": e.text,
        "owner": e.owner,
        "due": e.due,
        "box": e.box,
        "exact": exact,
    }


@dataclass
class Edits:
    """What a note changed, worked out against what was last written."""

    sections: dict[str, list[tuple[str, object]]]  # sec -> [(resolved id or "", entry)]
    changed: dict[str, dict]  # id -> {"text"?, "owner"?, "due"?}
    merges: dict[str, tuple[str, str, str]]  # id ("" for the body) -> (base, app, vault)
    deleted: set[str]
    new: dict[int, str]  # id(entry) -> section, for new items
    loose: dict[int, str]  # id(entry) -> section: new lines not taken (redacted export)
    body: str | None  # the new body, or None


def empty_edits() -> Edits:
    return Edits({}, {}, {}, set(), {}, {}, None)


def resolve(note, written: dict[str, dict], current: dict[str, str]) -> dict[int, str]:
    """id(entry) -> the block it is: its block id when the last write wrote it, else (a line
    without one, or one we never wrote) a written block of its section that no line names, by
    the same text, then a similar one (tasks.same_item)."""
    from .tasks import _norm

    out: dict[int, str] = {}
    used: set[str] = set()
    for entries in note.sections.values():
        for e in entries:
            if e.id and e.id in written and e.id not in used:
                out[id(e)] = e.id
                used.add(e.id)
    for exact in (True, False):
        for sec, entries in note.sections.items():
            for e in entries:
                if id(e) in out:
                    continue
                for block_id, w in written.items():
                    if block_id in used or w["section"] != sec or block_id not in current:
                        continue
                    a, b = e.text, w["text"]
                    if (_norm(a) == _norm(b)) if exact else same_item(a, b):
                        out[id(e)] = block_id
                        used.add(block_id)
                        break
    return out


def work_out(
    note,
    written: dict,
    data: SummaryData,
    summary: str,
    redact: bool,
    body_of: Callable[[str], str] | None = None,
    text_of: Callable[[str], str] = _vault_text,
) -> Edits:
    """The edits of `note` against `written` (`blocks`, `body`, `body_exact`). `body_of(summary)`
    and `text_of(item text)`: the summary's body and an item's text as the projection renders
    them (default: an Obsidian note's)."""
    from ..export.note_parse import body_text
    from .summary_doc import SECTIONS, _iso, section

    body_of = body_of or body_text
    blocks: dict[str, dict] = written.get("blocks") or {}
    current = {i: t for s in SECTIONS for i, t in section(data, s)}
    items = {a.id: a for a in data.action_items}
    resolved = resolve(note, blocks, current)
    edits = empty_edits()

    def eligible(block_id: str) -> bool:  # only what the note showed as it was (not redacted)
        return bool(blocks[block_id].get("exact", True))

    def marked(text: str) -> bool:
        return redact and _REDACTED.search(text) is not None

    seen: set[str] = set()
    for sec, entries in note.sections.items():
        rows: list[tuple[str, object]] = []
        for e in entries:
            block_id = resolved.get(id(e), "")
            rows.append((block_id, e))
            if not block_id:
                if marked(e.text):  # kept in the note, not taken
                    edits.loose[id(e)] = sec
                else:
                    edits.new[id(e)] = sec
                continue
            seen.add(block_id)
            if block_id not in current or not eligible(block_id) or marked(e.text):
                continue
            w = blocks[block_id]
            change: dict = {}
            if e.text != w["text"]:
                app_now = text_of(current[block_id])
                if app_now == w["text"]:
                    change["text"] = e.text
                else:
                    edits.merges[block_id] = (w["text"], app_now, e.text)
            item = items.get(block_id)
            if item is not None and w["section"] == "action_items":
                if e.owner != w["owner"] and (item.owner or None) == w["owner"]:
                    change["owner"] = e.owner
                if e.box and w["box"] and e.due != w["due"] and _iso(item.due) == w["due"]:
                    change["due"] = e.due
            if change:
                edits.changed[block_id] = change
        edits.sections[sec] = rows
    for block_id, w in blocks.items():
        if (
            w["section"] in note.sections
            and block_id not in seen
            and block_id in current
            and eligible(block_id)
        ):
            edits.deleted.add(block_id)

    old = written.get("body")
    if note.body is not None and note.body != (old or ""):
        app_now = body_of(summary)
        if written.get("body_exact", True) and not marked(note.body):
            if app_now == (old or ""):
                edits.body = note.body
            else:
                edits.merges[""] = (old or "", app_now, note.body)
    return edits


def three_way(merges: dict[str, tuple[str, str, str]]) -> dict[str, str]:
    """One-off three-way merges by the sidecar (`base`): id -> merged text. A merge that fails
    keeps the app's text."""
    from ..export.note_merge import MergeError, merge_many
    from .link import find_binary

    if not merges:
        return {}
    keys = list(merges)
    binary = find_binary()
    results: list = []
    if binary is not None:
        try:
            requests = [{"base": b, "app": a, "vault": v} for b, a, v in merges.values()]
            results = merge_many(binary, requests)
        except MergeError as e:
            _warn_once("pull", "Merging note edits failed: %s", e)
    out = {}
    for n, key in enumerate(keys):
        r = results[n] if n < len(results) else None
        out[key] = r[1] if isinstance(r, tuple) else merges[key][1]
    return out


def apply_edits(data: SummaryData, summary: str, edits: Edits, merged: dict[str, str]):
    """The summary data and body with the edits; new entries without ids (apply_human gives
    them), in the note's order after the line before them. Returns (data, body, positions of
    the new entries as {id(entry): (section, index)}, counts per section)."""
    from .summary_doc import _AT, _IDS, SECTIONS, section

    data = data.model_copy(deep=True)
    counts = {s: 0 for s in SECTIONS}
    positions: dict[int, tuple[str, int]] = {}
    for sec in SECTIONS:
        if sec == "action_items":
            rows = [(a.id, a) for a in data.action_items]
        else:
            ats = getattr(data, _AT[sec])
            rows = [
                (i, (t, ats[n] if n < len(ats) else None))
                for n, (i, t) in enumerate(section(data, sec))
            ]
        out: list[tuple[str, object]] = []
        for block_id, value in rows:
            if block_id in edits.deleted:
                counts[sec] += 1
                continue
            change = dict(edits.changed.get(block_id, {}))
            if block_id in merged:
                change["text"] = merged[block_id]
            if change:
                counts[sec] += 1
                if sec == "action_items":
                    update = {k: v for k, v in change.items() if k in ("text", "owner")}
                    if "due" in change:
                        update["due"] = date.fromisoformat(change["due"]) if change["due"] else None
                    value = value.model_copy(update=update)
                else:
                    value = (change.get("text", value[0]), value[1])
            out.append((block_id, value))
        # New lines, each after the line before it in the note (else first).
        anchor = None
        for block_id, e in edits.sections.get(sec, []):
            if id(e) not in edits.new:
                if any(k == block_id for k, _ in out):
                    anchor = block_id
                continue
            if sec == "action_items":
                value = ActionItem(
                    text=e.text,
                    owner=e.owner,
                    due=date.fromisoformat(e.due) if e.due else None,
                    done=bool(e.done),
                )
            else:
                value = (e.text, None)
            key = f"\0{id(e)}"
            keys = [k for k, _ in out]
            pos = 0 if anchor is None else keys.index(anchor) + 1
            out.insert(pos, (key, value))
            anchor = key
            counts[sec] += 1
        for n, (k, _) in enumerate(out):
            if k.startswith("\0"):
                positions[int(k[1:])] = (sec, n)
        if sec == "action_items":
            data.action_items = [
                v.model_copy(update={"id": "" if k.startswith("\0") else k}) for k, v in out
            ]
        else:
            texts = [v[0] for _, v in out]
            if sec == "decisions":
                data.decisions = texts
            else:
                data.open_questions = texts
            setattr(data, _AT[sec], [v[1] for _, v in out])
            setattr(data, _IDS[sec], ["" if k.startswith("\0") else k for k, _ in out])
    body = summary
    if edits.body is not None:
        body = edits.body
    if "" in merged:
        body = merged[""]
    return data, body, positions, counts


def annex(note, edits: Edits, ids: dict[int, str]) -> dict[str, list[str]]:
    """The note's lines that are not items, by the id of the item above them (or the head of
    their section); lines not taken as items (a redacted export) go with them."""
    out: dict[str, list[str]] = {}
    for key, lines in note.heads.items():
        out[f"@{key}"] = list(lines)
    for sec, rows in edits.sections.items():
        last = f"@{sec}"
        others = False
        for block_id, e in rows:
            if e.others and not others:  # the first line under "### Others"
                others = True
                last = "@others"
            key = block_id or ids.get(id(e), "")
            if key:
                last = key
                if e.annex:
                    out.setdefault(key, []).extend(e.annex)
                continue
            lines = [e.line, *e.annex] if id(e) in edits.loose else list(e.annex)
            if lines:
                out.setdefault(last, []).extend(lines)
    return {k: v for k, v in out.items() if v}


@dataclass
class Taken:
    """What `take` did: the summary as written (or as it was, when nothing was taken)."""

    data: SummaryData
    body: str
    edits: Edits
    ids: dict[int, str]  # id(entry) -> the id apply_human gave a new entry
    counts: dict[str, int]  # per section: entries changed, added or deleted
    body_changed: bool

    @property
    def taken(self) -> bool:
        return any(self.counts.values()) or self.body_changed


def take(
    ctx: AppContext,
    before: Session,
    note,
    written: dict,
    *,
    by: str,
    redact: bool = False,
    body_of: Callable[[str], str] | None = None,
    text_of: Callable[[str], str] = _vault_text,
) -> Taken:
    """Take the edits of `note` (against `written`) into the meeting's summary: work them out,
    merge three-way where the summary moved too, record them in the summary doc as a hand edit
    by `by` (apply_human: ids for new entries, tombstones for deleted ones), keep the version
    before and write the summary. The caller holds summary_doc's lock for the meeting and has
    made sure the doc exists. Raises SummaryDocUnavailable."""
    from .summary_doc import apply_human, section

    sid = before.id
    data = before.summary_data or SummaryData()
    summary = before.summary or ""
    edits = work_out(note, written, data, summary, redact, body_of, text_of)
    merged = three_way(edits.merges)
    new_data, new_body, positions, counts = apply_edits(data, summary, edits, merged)
    body_changed = new_body != summary
    out = Taken(data, summary, edits, {}, counts, body_changed)
    if not out.taken:
        return out
    stored, body, _ = apply_human(ctx, sid, new_data, new_body, by=by)
    for key, (sec, n) in positions.items():
        out.ids[key] = section(stored, sec)[n][0]
    ctx.repo.keep_version(before, "edited")
    ctx.repo.update_fields(sid, summary=body, summary_data=stored)
    out.data, out.body = stored, body
    return out


def announce(ctx: AppContext, before: Session, kind: str, taken: Taken, **extra) -> None:
    """After edits were taken: a history line `kind` (items, decisions, questions, body, and
    `extra`), a record sealed and the meeting announced on the bus."""
    from . import records

    sid = before.id
    history.log(
        ctx, sid, kind,
        items=taken.counts["action_items"], decisions=taken.counts["decisions"],
        questions=taken.counts["open_questions"], body=taken.body_changed, **extra,
    )  # fmt: skip
    records.seal_later(ctx, sid, "summary_edited")
    # `changed`: an open copy of the meeting reloads its summary (controller.svelte.ts).
    ctx.bus.publish(
        {"type": "session", "session_id": sid, "status": before.status.value, "changed": "summary"}
    )
