"""Where a reader's passage of a publication's notes is in the live document (docs/plans/
2026-11-09-live-publications-m4.md, "Item 1"). Pure, except `reader_notes` / `locator` (one
repository read for the meeting's own notes).

A reader selects text in the notes box of the viewer: the notes as published (own notes, chips
as their text, image lines, names and identifiers redacted) as the browser renders them. The
document's `plain` (a fold answer's, positions = seq positions, what `mark_comment_at` takes)
differs from that by the backend's sections, the redaction, image syntax and markdown markers.
So the passage is found in R, the rendering (`as_read`), with `comments.locate_body` (prefix and
suffix pick among repeats, then the fuzzy fallback), and R is aligned with Q, the document's own
lines redacted line by line (`_comparable`: every character of Q knows the range of `plain` it
came from; a name the redaction replaced is ONE range, so a passage touching "Person 2" lands on
all of "Ada Lovelace"). The landing span is redacted again and compared with the reader's text:
a wrong alignment is never marked (None: the comment stays detached, shown by its quote).
"""

from __future__ import annotations

import re
from collections.abc import Callable, Sequence
from difflib import SequenceMatcher
from typing import TYPE_CHECKING

from .comments import FUZZY, _fragment_ratio, _norm, locate_body, plain_markdown
from .notes_sections import parse_refs, sec

if TYPE_CHECKING:
    from ..context import AppContext
    from ..models.publication import PublicationSelection
    from ..models.session import Session
    from .publishing import Redaction

MAX_CHARS = 1_000_000  # notes longer than this (plain or rendered) are not aligned: None
_AUTOJUNK_TOKENS = 20_000  # above this many tokens the global alignment junks popular ones

# (plain, refs of the same answer) -> [start, end) in code points of plain, or None
Locate = Callable[[str, Sequence], tuple[int, int] | None]


# ---- R: the notes as the reader's notes box shows them --------------------------------------

# viewer/src/notes.ts `IMAGE_LINE`, `FENCE` and `PUNCT` (prepareNotes): kept in step by hand.
_IMAGE_LINE = re.compile(
    r"^!\[((?:[^\]\\\n]|\\.)*)\]\(mnemosyne:image/([0-9a-f]{64})\.(?:webp|jpeg)@\d+x\d+\)$"
)
_FENCE = re.compile(r"^ {0,3}(`{3,}|~{3,})")
_PUNCT = re.compile(r"[!\"#$%&'()*+,\-./:;<=>?@\[\\\]^_`{|}~]")


def image_text(caption: str) -> str:
    """The text a picture that is not shown falls back to: viewer/src/notes.ts `imageText`
    (a test pins the two)."""
    return f"[image: {caption}]" if caption else "[image]"


def _unescape_caption(text: str) -> str:
    out, i = [], 0
    while i < len(text):
        if text[i] == "\\" and i + 1 < len(text) and _PUNCT.match(text[i + 1]):
            out.append(text[i + 1])
            i += 2
        else:
            out.append(text[i])
            i += 1
    return "".join(out)


def as_read(notes: str) -> str:
    """R: a publication's notes text (the manifest's `notes`) as plain text the way a reader
    selects in it: image lines as `image_text(caption)` (prepareNotes, fenced code skipped),
    then `plain_markdown` (markers dropped, whitespace collapsed)."""
    lines = (notes or "").split("\n")
    fence: str | None = None
    for n, line in enumerate(lines):
        f = _FENCE.match(line)
        if f:
            mark = f.group(1)
            if fence is None:
                fence = mark[0] * len(mark)
            elif mark[0] == fence[0] and len(mark) >= len(fence):
                fence = None
            continue
        if fence is not None:
            continue
        m = _IMAGE_LINE.match(line)
        if m:
            lines[n] = image_text(_unescape_caption(m.group(1)))
    return plain_markdown("\n".join(lines))


def reader_notes(
    app: AppContext,
    session: Session,
    sel: PublicationSelection,
    redaction: Redaction | None = None,
) -> str:
    """R for the meeting's current notes under `sel`: what `build_content` would publish as the
    notes, `as_read`. "" when the selection leaves the notes out or there are none."""
    from .publishing import Redaction, _notes_text, _own_notes

    notes = _own_notes(app, session)
    if not sel.notes or not notes.strip():
        return ""
    redaction = redaction or Redaction(session, sel)
    return as_read(_notes_text(notes, sel, redaction.text)[0])


def locator(
    app: AppContext,
    session: Session,
    sel: PublicationSelection,
    anchor: dict,
    redaction: Redaction | None = None,
    rendered: str | None = None,
) -> Locate:
    """The `locate` callback for shared_notes.mark_comment_located_blocking: R is worked out
    here, once, outside the notes lock (`rendered`: an R already made, e.g. `as_read` of the
    manifest's notes); the callback aligns it with the fold the lock holds."""
    from .publishing import Redaction

    redaction = redaction or Redaction(session, sel)
    if rendered is None:
        rendered = reader_notes(app, session, sel, redaction)
    redact = redaction.text

    def run(plain: str, refs: Sequence) -> tuple[int, int] | None:
        return locate(plain, refs, anchor, rendered, redact)

    return run


# ---- Q: the document's own lines, redacted, each character with its origin -----------------

_PIECE = re.compile(r"\w+|\s+|[^\w\s]+")  # every character of a line in one piece
_TOKEN = re.compile(r"\w+|[^\w\s]+")  # what the global alignment compares (no whitespace)


def _own_lines(plain: str, refs: Sequence | None) -> int:
    """How many lines of `plain` are the person's own: those above the first `sec:` line (the
    backend's sections are never published as the notes, notes_sections.own_text)."""
    heads = [r.index for r in parse_refs(refs) if sec(r.ref)]
    return min(heads) if heads else plain.count("\n") + 1


def _merged(ops: list[tuple], a: list[str]) -> list[tuple]:
    """Opcodes with a whitespace-only `equal` between two changes folded into one change: the
    redaction replaces a whole name ("Ada Lovelace" -> "Person 2"), never half of one."""
    out: list[list] = []
    for op in ops:
        if (
            len(out) >= 2
            and op[0] != "equal"
            and out[-1][0] == "equal"
            and out[-2][0] != "equal"
            and all(not t.strip() for t in a[out[-1][1] : out[-1][2]])
        ):
            out.pop()
            prev = out[-1]
            out[-1] = ["replace", prev[1], op[2], prev[3], op[4]]
        elif out and op[0] != "equal" and out[-1][0] != "equal":
            prev = out[-1]
            out[-1] = ["replace", prev[1], op[2], prev[3], op[4]]
        else:
            out.append(list(op))
    return [tuple(o) for o in out]


def _line_units(line: str, redact: Callable[[str], str]) -> list[tuple[str, int, int, bool]]:
    """The redacted line in units (text, lo, hi, same): `same` units are the line's own
    characters from `lo`; a changed unit's every character comes from line[lo:hi]."""
    red = redact(line)
    if red == line:
        return [(line, 0, len(line), True)]
    a = list(_PIECE.finditer(line))
    b = list(_PIECE.finditer(red))
    ta, tb = [m.group() for m in a], [m.group() for m in b]
    ops = _merged(SequenceMatcher(None, ta, tb, autojunk=False).get_opcodes(), ta)
    out = []
    for tag, i1, i2, j1, j2 in ops:
        lo = a[i1].start() if i1 < len(a) else len(line)
        hi = a[i2 - 1].end() if i2 > i1 else lo
        text = red[b[j1].start() : b[j2 - 1].end()] if j2 > j1 else ""
        if text:
            out.append((text, lo, hi, tag == "equal"))
    return out


def _comparable(
    plain: str, own: int, redact: Callable[[str], str]
) -> tuple[str, list[int], list[int]]:
    """Q (the own lines of `plain` redacted line by line, whitespace collapsed as
    `plain_markdown` collapses it) and, per character of Q, the [lo, hi) of `plain` it came
    from. Cells (U+001F, tabs) and line ends are whitespace."""
    chars: list[str] = []
    los: list[int] = []
    his: list[int] = []
    space: tuple[int, int] | None = None
    start = 0
    for n, line in enumerate(plain.split("\n")):
        if n >= own:
            break
        for text, lo, hi, same in _line_units(line.replace("\x1f", "\t"), redact):
            for k, c in enumerate(text):
                at = (start + lo + k, start + lo + k + 1) if same else (start + lo, start + hi)
                if c.isspace():
                    if space is None:
                        space = at
                    continue
                if space is not None and chars:
                    chars.append(" ")
                    los.append(space[0])
                    his.append(space[1])
                space = None
                chars.append(c)
                los.append(at[0])
                his.append(at[1])
        if space is None:
            space = (start + len(line), start + len(line) + 1)
        start += len(line) + 1
    return "".join(chars), los, his


# ---- the alignment ---------------------------------------------------------------------------


def _q_range(q: str, r: str, rs: int, re_: int) -> tuple[int, int] | None:
    """R's [rs, re_) as a range of Q, through a token alignment of the two: inside an equal
    token by its offset, a token R has and Q has not (or a changed one) by the edges of the
    Q tokens there."""
    qt = list(_TOKEN.finditer(q))
    rt = list(_TOKEN.finditer(r))
    if not qt or not rt:
        return None
    junk = max(len(qt), len(rt)) > _AUTOJUNK_TOKENS
    sm = SequenceMatcher(None, [m.group() for m in qt], [m.group() for m in rt], autojunk=junk)
    of_r: list[tuple] = [()] * len(rt)
    for op in sm.get_opcodes():
        for j in range(op[3], op[4]):
            of_r[j] = op
    j = next((j for j, m in enumerate(rt) if m.end() > rs), None)
    k = next((k for k in range(len(rt) - 1, -1, -1) if rt[k].start() < re_), None)
    if j is None or k is None or k < j:
        return None
    tag, i1, i2, j1, _ = of_r[j]
    if tag == "equal":
        m = qt[i1 + j - j1]
        qs = m.start() + min(max(0, rs - rt[j].start()), len(m.group()) - 1)
    else:
        qs = qt[i1].start() if i1 < len(qt) else len(q)
    tag, i1, i2, j1, _ = of_r[k]
    if tag == "equal":
        m = qt[i1 + k - j1]
        qe = m.start() + max(1, min(re_ - rt[k].start(), len(m.group())))
    elif i2 > i1:
        qe = qt[i2 - 1].end()
    else:
        qe = qt[i1 - 1].end() if i1 > 0 else 0
    return (qs, qe) if qe > qs else None


def locate(
    plain: str,
    refs: Sequence | None,
    anchor: dict,
    rendered: str,
    redact: Callable[[str], str],
) -> tuple[int, int] | None:
    """[start, end) in code points of `plain` (a fold answer's, `refs` from the same answer) of
    the passage a reader chose in `rendered` (R, `as_read`) with `anchor` ({exact, prefix,
    suffix}), or None: not found in R, not in the person's own lines, notes over MAX_CHARS, or
    the landing span, redacted again by `redact`, does not read as `exact`."""
    exact = (anchor.get("exact") or "").strip()
    if not exact or not plain or len(plain) > MAX_CHARS or len(rendered) > MAX_CHARS:
        return None
    r = plain_markdown(rendered)
    found = locate_body(r, anchor)
    if found is None:
        return None
    rs, re_, _ = found
    q, los, his = _comparable(plain, _own_lines(plain, refs), redact)
    span = _q_range(q, r, rs, re_)
    if span is None:
        return None
    start, end = los[span[0]], his[span[1] - 1]
    if end <= start:
        return None
    want = _norm(plain_markdown(exact))
    got = _norm(redact(plain[start:end].replace("\x1f", "\t")))
    if not want or not got:
        return None
    if want != got and _fragment_ratio(want, got) < FUZZY:
        return None
    return start, end
