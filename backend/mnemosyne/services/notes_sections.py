"""The backend's sections at the end of a meeting's live notes (docs/plans/2026-11-04-summaries-
in-notes.md): the copilot's lines and the summary. Pure, except `own_notes` (one repository read).

A `ref` line mark (notes-core; never in the markdown, so readers see ordinary lines) names what
a line IS. The sidecar reports them in every answer (`refs`: line, ref, index, md_line; see
services/shared_notes.py `refs_of`) and takes them in `set_text` by line of the request's
markdown. The values:

- `sec:<name>` a section's heading: summary, decisions, action_items, open_questions, copilot;
- `sum:<id>` a summary block's line (a decision, question or action item; services/summary_ids.py);
- `cp:<n>` the copilot's line number n (`CopilotAdded.n`).

`split` cuts a markdown into the person's own text (everything above the first `sec:` line) and
the sections; `compose` changes some lines of it and gives the `set_text` refs for the result,
for shared_notes.edit_blocking. Lines are counted with `markdown.split("\\n")`, as the sidecar's
`md_line` is: a markdown ending in a newline has an empty last piece, which is not a line.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from ..storage.sqlite import SessionRepository

SEC, SUM, CP = "sec:", "sum:", "cp:"
SUMMARY, DECISIONS, ACTION_ITEMS, OPEN_QUESTIONS, COPILOT = (
    "summary",
    "decisions",
    "action_items",
    "open_questions",
    "copilot",
)
SECTION_NAMES = (SUMMARY, DECISIONS, ACTION_ITEMS, OPEN_QUESTIONS, COPILOT)


# ---- ref values ----------------------------------------------------------------------------


def sec_ref(name: str) -> str:
    return SEC + name


def sum_ref(block_id: str) -> str:
    return SUM + block_id


def cp_ref(n: int) -> str:
    return f"{CP}{n}"


def sec(ref: str | None) -> str | None:
    """The section name of a `sec:<name>` ref (any name, known or not), else None."""
    if ref and ref.startswith(SEC) and len(ref) > len(SEC):
        return ref[len(SEC) :]
    return None


def sum_id(ref: str | None) -> str | None:
    """The summary block id of a `sum:<id>` ref, else None."""
    if ref and ref.startswith(SUM) and len(ref) > len(SUM):
        return ref[len(SUM) :]
    return None


def cp_n(ref: str | None) -> int | None:
    """The number of a `cp:<n>` ref (n a decimal >= 0), else None."""
    if ref and ref.startswith(CP):
        n = ref[len(CP) :]
        if n.isascii() and n.isdigit():
            return int(n)
    return None


@dataclass(frozen=True)
class Ref:
    """One entry of a sidecar answer's `refs`."""

    line: str  # the element id of the line's `\n` (hex, as a task's `line`)
    ref: str
    index: int  # the line's number in the document
    md_line: int  # the line of the answer's markdown where it starts


def _int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def parse_refs(value: dict | Sequence | None) -> list[Ref]:
    """The refs of a sidecar answer (a dict with `refs`), of a stored or `refs_of` list, or of
    Refs already; malformed entries are dropped."""
    if isinstance(value, dict):
        value = value.get("refs")
    if not isinstance(value, Sequence) or isinstance(value, str):
        return []
    out = []
    for r in value:
        if isinstance(r, Ref):
            out.append(r)
        elif (
            isinstance(r, dict)
            and isinstance(r.get("line"), str)
            and isinstance(r.get("ref"), str)
            and r["ref"]
            and _int(r.get("index"))
            and _int(r.get("md_line"))
        ):
            out.append(Ref(r["line"], r["ref"], r["index"], r["md_line"]))
    return out


# ---- split ---------------------------------------------------------------------------------


@dataclass
class Line:
    """One line of the markdown and the ref of the document line that starts there."""

    md_line: int
    md: str
    ref: str | None = None
    line: str | None = None  # that document line's element id


@dataclass
class Section:
    """A `sec:` heading and the lines after it up to the next `sec:` line (or the end)."""

    name: str
    heading: Line
    lines: list[Line] = field(default_factory=list)

    def items(self) -> list[Line]:
        """Its ref'd lines (the heading not included)."""
        return [x for x in self.lines if x.ref]

    @property
    def last(self) -> Line:
        """Its last line that is not blank, else the heading: where a new line goes after."""
        for x in reversed(self.lines):
            if x.md.strip():
                return x
        return self.heading


@dataclass
class Sections:
    markdown: str
    lines: list[Line]  # every piece of markdown.split("\n"), the empty last one included
    own_end: int  # md_line of the first `sec:` line; len(lines) when there is none
    sections: list[Section]  # in document order

    @property
    def own(self) -> str:
        """The person's own text: the markdown above the first `sec:` line, its trailing blank
        lines dropped, ending in a newline (the whole markdown when there is no section)."""
        if self.own_end >= len(self.lines):
            return self.markdown
        text = "\n".join(x.md for x in self.lines[: self.own_end]).rstrip("\n")
        return text + "\n" if text else ""

    def section(self, name: str) -> Section | None:
        """The first section of that name, None when its heading is not there."""
        return next((s for s in self.sections if s.name == name), None)

    def find(self, ref: str) -> Line | None:
        """The (first) line with this ref, wherever it is."""
        return next((x for x in self.lines if x.ref == ref), None)

    def with_prefix(self, prefix: str) -> list[Line]:
        """Every line whose ref starts with `prefix` (SEC, SUM, CP), in document order."""
        return [x for x in self.lines if x.ref and x.ref.startswith(prefix)]


def split(markdown: str, refs: dict | Sequence | None) -> Sections:
    """The markdown's lines with their refs (a ref whose md_line is past the end is dropped;
    two at one line: the first), the own text's end and the sections. A section whose heading
    is gone does not exist: its lines belong to the section before it (or the own text)."""
    pieces = markdown.split("\n")
    lines = [Line(i, md) for i, md in enumerate(pieces)]
    for r in parse_refs(refs):
        if 0 <= r.md_line < len(lines) and lines[r.md_line].ref is None:
            lines[r.md_line].ref = r.ref
            lines[r.md_line].line = r.line
    end = len(lines) - 1 if pieces[-1] == "" else len(lines)
    heads = [x for x in lines[:end] if sec(x.ref)]
    sections = []
    for i, head in enumerate(heads):
        stop = heads[i + 1].md_line if i + 1 < len(heads) else end
        sections.append(Section(sec(head.ref) or "", head, lines[head.md_line + 1 : stop]))
    own_end = heads[0].md_line if heads else len(lines)
    return Sections(markdown, lines, own_end, sections)


def own_text(markdown: str, refs: dict | Sequence | None) -> str:
    """`Sections.own` of the markdown."""
    return split(markdown, refs).own


def own_notes(repo: SessionRepository, session_id: str, notes: str) -> str:
    """A meeting's own notes: `notes` (its `Session.notes`) above the backend's sections, by
    the refs of the document's last fold. `notes` whole when there are no stored refs or they
    are not of this text (written around the document: the md_lines do not describe it)."""
    refs = repo.notes_refs(session_id, notes)
    if not refs:
        return notes
    return own_text(notes, refs)


# ---- compose -------------------------------------------------------------------------------


@dataclass(frozen=True)
class New:
    """A line to insert: its markdown (one line) and its ref (None or "": none)."""

    md: str
    ref: str | None = None


@dataclass(frozen=True)
class Edit:
    kind: str  # "replace", "delete", "insert" (after `at`; -1 = at the start) or "append"
    at: int = -1  # an md_line of the markdown split
    new: tuple[New, ...] = ()
    ref: str | None = None  # replace: the line's new ref (None keeps it, "" clears it)


def replace(at: int, md: str, ref: str | None = None) -> Edit:
    """Line `at` says `md`; its ref stays unless `ref` is given ("" clears it)."""
    return Edit("replace", at, (New(md),), ref)


def retag(line: Line, ref: str) -> Edit:
    """The line's text stays, its ref becomes `ref` ("" clears it)."""
    return replace(line.md_line, line.md, ref)


def delete(at: int) -> Edit:
    return Edit("delete", at)


def _news(new: Iterable[New | tuple[str, str | None] | str]) -> tuple[New, ...]:
    out = []
    for n in new:
        if isinstance(n, New):
            out.append(n)
        elif isinstance(n, str):
            out.append(New(n))
        else:
            out.append(New(*n))
    return tuple(out)


def insert_after(at: int, *new: New | tuple[str, str | None] | str) -> Edit:
    """New lines after line `at` (-1: before the first), in this order. Several inserts after
    the same line go in the order of the edits."""
    return Edit("insert", at, _news(new))


def append(*new: New | tuple[str, str | None] | str) -> Edit:
    """New lines at the end of the document (before its final newline)."""
    return Edit("append", -1, _news(new))


def _check_line(at: int, lines: list[Line], kind: str) -> None:
    if not 0 <= at < len(lines):
        raise ValueError(f"{kind}: no line {at}")


def compose(lines: list[Line], edits: Iterable[Edit]) -> tuple[str, dict[str, str]]:
    """The markdown of `lines` (`Sections.lines`) with `edits` applied, and the `set_text`
    refs for it: every line of the NEW markdown that is not blank gets its ref, or "" for
    none, so the sidecar's diff cannot hand one line's ref to its neighbour. Kept lines keep
    theirs. Raises ValueError for a line that is not there or edited twice."""
    lines = list(lines)
    if not lines:
        lines = [Line(0, "")]
    changed: dict[int, Edit] = {}
    after: dict[int, list[New]] = {}
    tail: list[New] = []
    for e in edits:
        if e.kind in ("replace", "delete"):
            _check_line(e.at, lines, e.kind)
            if e.at in changed:
                raise ValueError(f"line {e.at} is edited twice")
            changed[e.at] = e
        elif e.kind == "insert":
            if e.at != -1:
                _check_line(e.at, lines, "insert")
            after.setdefault(e.at, []).extend(e.new)
        elif e.kind == "append":
            tail.extend(e.new)
        else:
            raise ValueError(f"unknown edit {e.kind!r}")
    closed = lines[-1].md == ""  # the markdown ends in a newline
    last = len(lines) - 2 if closed else len(lines) - 1  # the last real line (-1: none)
    if tail:
        after.setdefault(last, []).extend(tail)

    out: list[tuple[str, str | None]] = [(n.md, n.ref) for n in after.get(-1, [])]
    for i, x in enumerate(lines):
        e = changed.get(i)
        if e is None:
            out.append((x.md, x.ref))
        elif e.kind == "replace":
            out.append((e.new[0].md, x.ref if e.ref is None else e.ref))
        out.extend((n.md, n.ref) for n in after.get(i, []))
    if tail and not closed:
        out.append(("", None))  # what was appended ends in a newline too
    refs = {str(n): ref or "" for n, (md, ref) in enumerate(out) if md.strip() or ref}
    return "\n".join(md for md, _ in out), refs
