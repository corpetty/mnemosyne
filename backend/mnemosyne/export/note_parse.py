"""The summary's part of an exported meeting note, read back (services/vault_sync.py
`apply_vault_edits`) and kept out of phase 1's text merge (`_merge_note`).

The projection sections are "## Summary" (the body), "## Decisions", "## Action Items" (with
"### Others") and "## Open Questions": what export/templates.py renders from the summary. Each
runs from its heading to the next "# "/"## " heading (or the callout of another assistant's
notes); only the first of each counts. In the item sections every top-level bullet is an entry:
its id is the `^m-<id>` block id at the end of the line (none: a line the user added), its text
is what follows the bullet without the decorations we add (vault_sync.item_text's rules) and
without [[links]]. Other lines (sub-bullets, paragraphs) are the "annex" of the entry above them,
or of the section's head when no entry comes before them.

Owners: a ` ([[X]])` / ` (X)` suffix (a "### Others" line: an `[[X]]: ` / `X: ` prefix). Whether
a plain one is an owner or part of the text depends on what the line said before (`known`, per
id: the owner and text last written): an item written with an owner keeps reading one; an item
written without one reads a new linked owner as one (unless its text had it), a plain one as
text; a line without a known id takes a linked owner, and a plain one when it looks like a name.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from ..services.vault_sync import _BLOCK_ID, _ISSUE, _TASKS_FIELDS, strip_links

HEADINGS = {
    "Summary": "summary",
    "Decisions": "decisions",
    "Action Items": "action_items",
    "Open Questions": "open_questions",
}
ITEM_SECTIONS = ("decisions", "action_items", "open_questions")
OTHERS = "others"  # the head of "### Others" (an annex key: "@others")

_HEADING = re.compile(r"^#{1,2}\s")
_CALLOUT = re.compile(r"^> \[!note\]- Notes by ")  # export/templates.py, another assistant's notes
_SUB = re.compile(r"^###\s+(.*?)\s*$")
_BULLET = re.compile(r"^[-*+] (.*)$")
_BOXED = re.compile(r"^\[(.)\] ?(.*)$")
# Our block id: last on the line, though Tasks fields typed after it are no reason to lose it.
_OUR_ID = re.compile(r"(?:^|\s)\^m-([0-9a-f]+)(?=\s|$)")
_DUE = re.compile("\U0001f4c5️?\\s*(\\d{4}-\\d{2}-\\d{2})")
_SUFFIX = re.compile(r"\s*\((\[\[[^\]]+\]\]|[^()]+)\)\s*$")
_PREFIX = re.compile(r"^(\[\[[^\]]+\]\]|[^:\[\]]{1,40}):\s+")
# A task a later meeting continues is a plain bullet ending in this (export/templates.py).
_CONTINUED = re.compile(r"\s*\u21a9 continued in \[\[[^\]]*\]\]\s*$")
_DONE_MARK = re.compile(r"\s+\(done\)\s*$")
_NAME = re.compile(r"^(?:SPEAKER_\d+|UNKNOWN|[A-Z][\w'.-]*(?: [A-Z][\w'.-]*){0,2})$")

PLACEHOLDER = "%% mnemosyne {} %%"

Known = dict[str, tuple[str | None, str]]  # id -> (owner, text) as last written


@dataclass
class Entry:
    """One top-level bullet of an item section."""

    id: str  # "" for a line without our block id
    text: str
    owner: str | None = None
    due: str | None = None  # ISO date (📅); only read on checkbox lines
    done: bool | None = None  # the box: None for no box or another status
    box: bool = False  # a checkbox line (its 📅 due is shown)
    others: bool = False  # under "### Others"
    annex: list[str] = field(default_factory=list)
    line: str = ""  # the line as it is in the note


@dataclass
class Note:
    body: str | None = None  # None: no Summary section
    sections: dict[str, list[Entry]] = field(default_factory=dict)  # the item sections present
    heads: dict[str, list[str]] = field(default_factory=dict)  # section (or "others") -> annex


def body_headings(*bodies: str | None) -> set[str]:
    """Headings ("# "/"## ") inside summary bodies: they do not end the Summary section."""
    out: set[str] = set()
    for body in bodies:
        for line in (body or "").splitlines():
            if _HEADING.match(line):
                out.add(strip_links(line.strip()))
    return out


def body_text(summary: str | None) -> str:
    """A summary body as the note's Summary section reads back (`parse`), before linking."""
    return strip_links("\n".join(_trim((summary or "").splitlines(), leading=True)))


def spans(lines: list[str], in_body: set[str] = frozenset()) -> dict[str, tuple[int, int]]:
    """name -> (first line, end) of each projection section, in `lines` (keepends or not)."""
    out: dict[str, tuple[int, int]] = {}
    current: str | None = None
    start = 0
    for n, line in enumerate(lines):
        bare = line.rstrip("\r\n")
        heading = bool(_HEADING.match(bare))
        name = HEADINGS.get(bare[3:].strip()) if bare.startswith("## ") else None
        if name in out:  # only the first of each counts
            name = None
        if name is None and current == "summary" and strip_links(bare.strip()) in in_body:
            continue  # a heading of the body itself
        if current is not None and (heading or _CALLOUT.match(bare)):
            out[current] = (start, n)
            current = None
        if name is not None:
            current, start = name, n
            out[name] = (n, len(lines))
    if current is not None:
        out[current] = (start, len(lines))
    return out


def cut(text: str, in_body: set[str] = frozenset()) -> tuple[str, dict[str, str]]:
    """The note with each projection section replaced by a placeholder line, and the sections'
    texts (heading included) by name."""
    lines = text.splitlines(keepends=True)
    found = spans(lines, in_body)
    by_start = {start: (name, end) for name, (start, end) in found.items()}
    out: list[str] = []
    sections: dict[str, str] = {}
    n = 0
    while n < len(lines):
        if n in by_start:
            name, end = by_start[n]
            sections[name] = "".join(lines[n:end])
            out.append(PLACEHOLDER.format(name) + "\n")
            n = end
            continue
        out.append(lines[n])
        n += 1
    return "".join(out), sections


def splice(merged: str, sections: dict[str, str]) -> str:
    """The placeholders of `merged` replaced by `sections` (the rendering's): the first of each
    name; another copy, or one without a section, goes."""
    marks = {PLACEHOLDER.format(name): name for name in HEADINGS.values()}
    used: set[str] = set()
    out: list[str] = []
    for line in merged.splitlines(keepends=True):
        name = marks.get(line.strip())
        if name is None:
            out.append(line)
            continue
        if name in sections and name not in used:
            used.add(name)
            out.append(sections[name])
    return "".join(out)


def _trim(lines: list[str], leading: bool) -> list[str]:
    while lines and not lines[-1].strip():
        lines.pop()
    while leading and lines and not lines[0].strip():
        lines.pop(0)
    return lines


def _is_owner(name: str, linked: bool, item_id: str, rest: str, known: Known) -> bool:
    k = known.get(item_id) if item_id else None
    if k is None:
        return linked or bool(_NAME.match(name))
    owner, text = k
    if owner is not None:
        return True
    # Written without an owner: a linked one is new, unless the text had it already.
    return linked and strip_links(rest).strip() != text


def _owner_name(raw: str) -> tuple[str, bool]:
    if raw.startswith("[[") and raw.endswith("]]"):
        inner = raw[2:-2]
        return inner.split("|", 1)[-1].strip(), True
    return raw.strip(), False


def _entry(line: str, others: bool, sec: str, known: Known) -> Entry | None:
    m = _BULLET.match(line)
    if m is None:
        return None
    rest = m.group(1)
    box = done = None
    if sec == "action_items":
        b = _BOXED.match(rest)
        if b is not None:
            box = b.group(1)
            rest = b.group(2)
            done = True if box in "xX" else False if box == " " else None
    ours = list(_OUR_ID.finditer(rest))
    item_id = ours[-1].group(1) if ours else ""
    if ours:
        rest = rest[: ours[-1].start()] + rest[ours[-1].end() :]
    rest = _BLOCK_ID.sub("", rest)
    if sec == "action_items" and box is None and not others:
        rest = _CONTINUED.sub("", rest)  # not part of the text, nor of the owner suffix
    entry = Entry(id=item_id, text="", box=box is not None, done=done, others=others, line=line)
    if sec != "action_items":
        entry.text = strip_links(rest).strip()
        return entry if entry.text else None
    if box is not None:
        due = _DUE.search(rest)
        entry.due = due.group(1) if due else None
        t = _TASKS_FIELDS.search(rest)
        if t:
            rest = rest[: t.start()]
        rest = _ISSUE.sub("", rest).rstrip()
    if others and box is None:
        rest = _DONE_MARK.sub("", rest)
        p = _PREFIX.match(rest)
        if p is not None:
            name, linked = _owner_name(p.group(1))
            if name and _is_owner(name, linked, item_id, rest, known):
                entry.owner = name
                rest = rest[p.end() :]
    else:
        s = _SUFFIX.search(rest)
        if s is not None:
            name, linked = _owner_name(s.group(1))
            if name and _is_owner(name, linked, item_id, rest, known):
                entry.owner = name
                rest = rest[: s.start()]
    entry.text = strip_links(rest).strip()
    return entry if entry.text else None


def parse(text: str, known: Known | None = None, in_body: set[str] = frozenset()) -> Note:
    """The projection sections of a note (see the module doc)."""
    known = known or {}
    lines = text.splitlines()
    note = Note()
    for name, (start, end) in spans(lines, in_body).items():
        inner = lines[start + 1 : end]
        if name == "summary":
            note.body = strip_links("\n".join(_trim(list(inner), leading=True)))
            continue
        entries: list[Entry] = []
        head: list[str] = []
        heads = {name: head}
        annex = head
        others = False
        for line in inner:
            sub = _SUB.match(line) if name == "action_items" else None
            if sub is not None and sub.group(1) == "Others" and not others:
                others = True
                _trim(annex, leading=annex is head)
                annex = heads[OTHERS] = []
                continue
            entry = _entry(line, others, name, known)
            if entry is None:
                annex.append(line)
                continue
            _trim(annex, leading=annex is head or annex is heads.get(OTHERS))
            entries.append(entry)
            annex = entry.annex
        _trim(annex, leading=annex is head or annex is heads.get(OTHERS))
        note.sections[name] = entries
        for key, lines_ in heads.items():
            if lines_:
                note.heads[key] = lines_
    return note
