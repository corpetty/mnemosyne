"""Chips in the meeting notes, for everything that is not the notes editor (phase 11).

The editor draws a link whose URL starts with `mnemosyne:` as a pill (src/lib/notes/chips.ts).
In `Session.notes`, the markdown notes-core renders, a chip is an ordinary link:

    [12:34](mnemosyne:t/<part>/<seconds>)
    [@Ada Lovelace](mnemosyne:person/Ada%20Lovelace)
    [Planning, Oct 14](mnemosyne:meeting/<hex id>)
    [Copilot](mnemosyne:by/copilot)

Readers that are not the editor must never see those URLs: `plain_notes` turns each chip into its
text (the summary's hint, MCP, published notes), `obsidian_notes` into what the vault understands.
Both are strict like the TypeScript `parseChip`: a link that is not exactly one of the three forms
is left as it is, and so is anything that is not a well-formed inline link.

The scanner follows how notes-core writes and reads links (notes-core/src/render.rs `url`,
`escape_char`; parse.rs `link_tail`): backslash escapes before ASCII punctuation, a link text that
ends at its first unescaped `]` (code spans may hold one), a bare or `<...>` destination.

An image (services/notes_media.py) is a line of its own, `![caption](mnemosyne:image/<value>)`, the
caption escaped like link text. `plain_notes` turns it into `[image: caption]` (`[image]` without a
caption); `obsidian_notes` into `![[file|caption]]` when the export has the file.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import unquote

from .notes_media import parse_value

_TIME = re.compile(r"mnemosyne:t/([0-9]+)/([0-9]+)", re.ASCII)
_PERSON = re.compile(r"mnemosyne:person/([^/]+)")
_MEETING = re.compile(r"mnemosyne:meeting/([0-9a-f]{8,32})")
_BY = re.compile(r"mnemosyne:by/(copilot)")

# An image line as notes-core writes it: the whole line, the caption is escaped link text.
IMAGE_LINE = re.compile(r"^!\[((?:[^\]\\\n]|\\.)*)\]\(mnemosyne:image/([^)\s]+)\)$", re.MULTILINE)

_MAX_SAFE = 2**53 - 1

_PUNCT = frozenset("!\"#$%&'()*+,-./:;<=>?@[\\]^_`{|}~")


@dataclass(frozen=True)
class Chip:
    kind: str  # "time" | "person" | "meeting" | "by"
    text: str  # the link text, backslash escapes removed
    value: str  # person: the name; meeting: the id; time: "<part>/<seconds>"; by: "copilot"


def parse_chip(url: str) -> tuple[str, str] | None:
    """(kind, value) of a chip URL, or None for anything but exactly one of the four forms."""
    if m := _TIME.fullmatch(url):
        if max(int(m[1]), int(m[2])) > _MAX_SAFE:  # Number.isSafeInteger in the TS parser
            return None
        return "time", f"{m[1]}/{m[2]}"
    if m := _PERSON.fullmatch(url):
        try:
            name = unquote(m[1], errors="strict")
        except UnicodeDecodeError:
            return None
        if not _valid_escapes(m[1]) or not name:
            return None
        return "person", name
    if m := _MEETING.fullmatch(url):
        return "meeting", m[1]
    if m := _BY.fullmatch(url):
        return "by", m[1]
    return None


def _valid_escapes(raw: str) -> bool:
    """Every `%` starts two hex digits (decodeURIComponent throws on anything else)."""
    return all(
        re.fullmatch(r"%[0-9A-Fa-f]{2}", raw[i : i + 3]) for i, c in enumerate(raw) if c == "%"
    )


def _unescape(s: str) -> str:
    out: list[str] = []
    i = 0
    while i < len(s):
        if s[i] == "\\" and i + 1 < len(s) and s[i + 1] in _PUNCT:
            out.append(s[i + 1])
            i += 2
        else:
            out.append(s[i])
            i += 1
    return "".join(out)


def _code_end(md: str, i: int) -> int | None:
    """`i` at a backtick run: the index just after the matching code span, else None."""
    j = i
    while j < len(md) and md[j] == "`":
        j += 1
    run = j - i
    k = j
    while k < len(md):
        if md[k] == "`":
            e = k
            while e < len(md) and md[e] == "`":
                e += 1
            if e - k == run:
                return e
            k = e
        else:
            k += 1
    return None


def _link_at(md: str, i: int) -> tuple[str, str, int] | None:
    """`md[i]` is `[`: (raw text, url, end index) of the inline link starting there, else None."""
    n = len(md)
    j = i + 1
    while j < n:
        c = md[j]
        if c == "\\" and j + 1 < n and md[j + 1] in _PUNCT:
            j += 2
        elif c == "`":
            end = _code_end(md, j)
            if end is None:
                j += 1
                while j < n and md[j] == "`":
                    j += 1
            else:
                j = end
        elif c == "[":
            return None  # notes-core escapes a bracket inside a link; this is not its link
        elif c == "]":
            break
        elif c == "\n":
            return None
        else:
            j += 1
    else:
        return None
    text = md[i + 1 : j]
    if md[j + 1 : j + 2] != "(":
        return None
    k = j + 2
    while k < n and md[k] in " \t":
        k += 1
    url: list[str] = []
    if md[k : k + 1] == "<":
        k += 1
        while True:
            if k >= n:
                return None
            c = md[k]
            if c == ">":
                k += 1
                break
            if c in "<\n":
                return None
            if c == "\\" and k + 1 < n and md[k + 1] in _PUNCT:
                url.append(md[k + 1])
                k += 2
            else:
                url.append(c)
                k += 1
    else:
        depth = 0
        while k < n:
            c = md[k]
            if c == "\\" and k + 1 < n and md[k + 1] in _PUNCT:
                url.append(md[k + 1])
                k += 2
                continue
            if c.isspace() or ord(c) < 32:
                break
            if c == "(":
                depth += 1
            elif c == ")":
                if depth == 0:
                    break
                depth -= 1
            url.append(c)
            k += 1
        if depth:
            return None
    while k < n and md[k] in " \t":
        k += 1
    if md[k : k + 1] != ")":  # a title, or no end: not a link we rewrite
        return None
    return text, "".join(url), k + 1


def _rewrite(md: str, fn: Callable[[Chip], str]) -> str:
    """`md` with every chip link replaced by what `fn` returns for it."""
    if "mnemosyne:" not in md:
        return md
    out: list[str] = []
    n = len(md)
    i = 0
    last = 0
    while i < n:
        c = md[i]
        if c == "\\" and i + 1 < n and md[i + 1] in _PUNCT:
            i += 2
        elif c == "`":
            end = _code_end(md, i)
            if end is None:
                while i < n and md[i] == "`":
                    i += 1
            else:
                i = end
        elif c == "[":
            link = _link_at(md, i)
            if link is None:
                i += 1
                continue
            text, url, end = link
            chip = parse_chip(url)
            if chip is None:
                i = end  # an ordinary link: untouched
                continue
            line_start = md.rfind("\n", 0, i) + 1
            row = md[line_start:i].lstrip().startswith("|")
            out.append(md[last:i])
            replacement = fn(Chip(chip[0], _unescape(text), chip[1]))
            out.append(replacement.replace("|", "\\|") if row else replacement)
            last = i = end
        else:
            i += 1
    out.append(md[last:])
    return "".join(out)


@dataclass(frozen=True)
class ImageLine:
    sha: str  # the plain content hash
    ext: str  # "webp" | "jpeg"
    width: int
    height: int
    caption: str  # backslash escapes removed
    start: int  # the line's span in the markdown
    end: int


def image_lines(md: str) -> list[ImageLine]:
    """The image lines of `md` in order (the same image may repeat). A line whose value is not
    exactly `<sha256>.<webp|jpeg>@<w>x<h>` is not an image and is not listed."""
    out: list[ImageLine] = []
    if "mnemosyne:image/" not in md:
        return out
    for m in IMAGE_LINE.finditer(md):
        value = parse_value(m[2])
        if value is not None:
            out.append(ImageLine(*value, _unescape(m[1]), m.start(), m.end()))
    return out


def _rewrite_images(md: str, fn: Callable[[ImageLine], str]) -> str:
    """`md` with every image line replaced by what `fn` returns for it."""
    lines = image_lines(md)
    if not lines:
        return md
    out: list[str] = []
    last = 0
    for line in lines:
        out.append(md[last : line.start])
        out.append(fn(line))
        last = line.end
    out.append(md[last:])
    return "".join(out)


def _plain_image(line: ImageLine) -> str:
    caption = re.sub(r"\s+", " ", line.caption).strip()
    return f"[image: {caption}]" if caption else "[image]"


def _chip_text(chip: Chip) -> str:
    # In the notes the copilot chip is followed by a space: "Copilot:" + " " reads "Copilot: ...".
    return "Copilot:" if chip.kind == "by" else chip.text


def plain_notes(md: str) -> str:
    """The notes with every chip replaced by its text: `12:34`, `@Ada Lovelace`, a meeting's
    title, `Copilot:` for the copilot chip, and every image line by `[image: caption]` (`[image]`
    without a caption). Nothing in the result names a `mnemosyne:` URL."""
    return _rewrite(_rewrite_images(md, _plain_image), _chip_text)


def task_line_parts(md: str) -> tuple[str | None, str]:
    """(owner, text) of a task line's words: the owner is the first person chip's name (None
    without one); the text is `md` with person, time and copilot chips removed, a meeting chip kept
    as its text (the title is what the task is about)."""
    owner: list[str] = []

    def cut(chip: Chip) -> str:
        if chip.kind == "meeting":
            return chip.text
        if chip.kind == "person" and not owner:
            owner.append(chip.value)
        return ""

    text = _rewrite(md, cut)
    return (owner[0] if owner else None), text


_WIKI_BAD = re.compile(r"[\[\]|#^\\\n]")
_SIZE_ALIAS = re.compile(r"[0-9]+(x[0-9]+)?", re.ASCII)  # `![[file|100]]` is a width, not a caption


def obsidian_notes(
    md: str,
    link_people: bool,
    meeting_note: Callable[[str], tuple[str, str] | None] | None = None,
    image_file: Callable[[str, str], str | None] | None = None,
) -> str:
    """The notes as the vault should have them: a time chip is its text, a person chip `[[Name]]`
    when `link_people` (else its text), a meeting chip `[[stem|title]]` when `meeting_note(id)`
    gives that meeting's exported note as (stem, title), else its text. An image line is
    `![[file|caption]]` when `image_file(sha, ext)` gives the file's name in the vault (the export
    wrote it), else the plain `[image: caption]`."""

    def image(line: ImageLine) -> str:
        file = None
        if image_file is not None:
            try:
                file = image_file(line.sha, line.ext)
            except Exception:  # a lookup that fails is the plain form, never a failed export
                file = None
        if not file or _WIKI_BAD.search(file):
            return _plain_image(line)
        alias = re.sub(r"\s+", " ", _WIKI_BAD.sub(" ", line.caption)).strip()
        if not alias or _SIZE_ALIAS.fullmatch(alias):  # Obsidian would read a number as a size
            return f"![[{file}]]"
        return f"![[{file}|{alias}]]"

    def one(chip: Chip) -> str:
        if chip.kind == "person" and link_people and not _WIKI_BAD.search(chip.value):
            return f"[[{chip.value}]]"
        if chip.kind == "meeting" and meeting_note is not None:
            try:
                found = meeting_note(chip.value)
            except Exception:  # a lookup that fails is a title, never a failed export
                found = None
            if found and found[0] and not _WIKI_BAD.search(found[0]):
                stem, title = found
                alias = re.sub(r"\s+", " ", _WIKI_BAD.sub(" ", title or chip.text)).strip()
                return f"[[{stem}|{alias}]]" if alias and alias != stem else f"[[{stem}]]"
        return _chip_text(chip)

    return _rewrite(_rewrite_images(md, image), one)


def exported_meeting_note(repo, session_id: str) -> tuple[str, str] | None:
    """(file name without .md, title) of a meeting whose note is in the vault, for
    `obsidian_notes`; None when it was never exported, or the reader cannot see the meeting."""
    try:
        record = repo.get_vault_note(session_id)
        if not record:
            return None
        session = repo.get(session_id)
        if session is None:
            return None
        return Path(record["path"]).stem, session.name
    except Exception:
        return None


def retarget_chips(md: str, fn: Callable[[str, str], str | None]) -> str:
    """`md` with the URL of each time and meeting chip replaced when `fn(kind, value)` returns a
    new value (`"<part>/<seconds>"` for a time chip, the id for a meeting chip); None leaves the
    link as it is. Only the URL changes: the link text stays as typed (combining meetings,
    services/combine.py). Person chips and every other link are untouched."""
    if "mnemosyne:" not in md:
        return md
    out: list[str] = []
    n = len(md)
    i = 0
    last = 0
    while i < n:
        c = md[i]
        if c == "\\" and i + 1 < n and md[i + 1] in _PUNCT:
            i += 2
        elif c == "`":
            end = _code_end(md, i)
            if end is None:
                while i < n and md[i] == "`":
                    i += 1
            else:
                i = end
        elif c == "[":
            link = _link_at(md, i)
            if link is None:
                i += 1
                continue
            text, url, end = link
            chip = parse_chip(url)
            new = fn(*chip) if chip is not None and chip[0] in ("time", "meeting") else None
            if new is not None and new != chip[1]:
                head = i + 1 + len(text) + 2  # past "[text]("
                prefix = "mnemosyne:t/" if chip[0] == "time" else "mnemosyne:meeting/"
                out.append(md[last:head])
                out.append(prefix + new + ")")
                last = end
            i = end
        else:
            i += 1
    out.append(md[last:])
    return "".join(out)
