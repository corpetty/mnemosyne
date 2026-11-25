"""From what Mnemosyne writes (markdown summaries, digests, notes) to what an engine should say.

`speakable` drops what a listener cannot hear (links' addresses, `[[wiki links]]`' brackets,
checkboxes, emphasis marks, image lines, code fences) and turns structure into sentences: a
heading becomes its own sentence, a list item ends with a full stop, a table row reads its cells
in order. `chunks` cuts the result into pieces of a few sentences, since cloning models are
reliable on short inputs and drift or skip words on long ones. `respell` applies the person's
pronunciations ("Mnemosyne -> nee-MOSS-ih-nee", setting `narration_pronunciations`).
"""

from __future__ import annotations

import re
from dataclasses import dataclass

CHUNK_CHARS = 320  # a target; a single long sentence is cut at commas, then at spaces
MIN_CHARS = 60  # a shorter last piece joins the one before it

_FENCE = re.compile(r"^```.*?^```[ \t]*$", re.MULTILINE | re.DOTALL)
_IMAGE = re.compile(r"!\[[^\]]*\]\([^)]*\)")
_LINK = re.compile(r"\[([^\]]+)\]\([^)]*\)")
_WIKI = re.compile(r"\[\[(?:[^\]|]*\|)?([^\]]+)\]\]")
_BLOCK_ID = re.compile(r"\s\^[\w-]+$")
_URL = re.compile(r"https?://\S+")
_EMPHASIS = re.compile(r"(\*\*|__|\*|_|~~|`)(?=\S)(.+?)(?<=\S)\1")
_TASK = re.compile(r"^(\s*)[-*+]\s+\[[ xX]\]\s+")
_BULLET = re.compile(r"^(\s*)([-*+]|\d+[.)])\s+")
_HEADING = re.compile(r"^#{1,6}\s+")
_FRONTMATTER = re.compile(r"\A---\n.*?\n---\n", re.DOTALL)
_TABLE_RULE = re.compile(r"^\|?\s*:?-{3,}:?\s*(\|\s*:?-{3,}:?\s*)*\|?\s*$")
_SENTENCE_END = re.compile(r"(?<=[.!?…])\s+(?=[\"'(\[]?[A-Z0-9])")
_ENDS = (".", "!", "?", ":", ";", "…")


def _sentence(line: str) -> str:
    line = line.strip()
    if not line:
        return ""
    return line if line.endswith(_ENDS) else line + "."


def speakable(markdown: str) -> str:
    """Plain spoken text from markdown, one paragraph per line."""
    text = _FRONTMATTER.sub("", (markdown or "").replace("\r\n", "\n"))
    text = _FENCE.sub("", text)
    out: list[str] = []
    for raw in text.split("\n"):
        line = raw.rstrip()
        if not line.strip() or _TABLE_RULE.match(line.strip()):
            continue
        line = _IMAGE.sub("", line)
        line = _WIKI.sub(r"\1", line)
        line = _LINK.sub(r"\1", line)
        line = _URL.sub("", line)
        line = _BLOCK_ID.sub("", line)
        if line.lstrip().startswith(">"):
            line = line.lstrip().lstrip(">").strip()
        if line.strip().startswith("|"):
            cells = [c.strip() for c in line.strip().strip("|").split("|")]
            line = ", ".join(c for c in cells if c)
        line = _TASK.sub(r"\1", line)
        line = _BULLET.sub(r"\1", line)
        line = _HEADING.sub("", line)
        for _ in range(3):  # nested emphasis
            line = _EMPHASIS.sub(r"\2", line)
        line = re.sub(r"\s+", " ", line).strip(" -*_#")
        if line:
            out.append(_sentence(line))
    return "\n".join(out)


@dataclass(frozen=True)
class Respelling:
    pattern: re.Pattern
    spoken: str


def parse_pronunciations(text: str) -> list[Respelling]:
    """`written -> spoken` lines (case-insensitive, whole words); `#` starts a comment."""
    rules = []
    for raw in (text or "").splitlines():
        line = raw.split("#", 1)[0].strip()
        if "->" not in line:
            continue
        written, spoken = (p.strip() for p in line.split("->", 1))
        if written and spoken:
            rules.append(
                Respelling(re.compile(rf"(?<!\w){re.escape(written)}(?!\w)", re.IGNORECASE), spoken)
            )
    # Longest first, so "Corey Petty" wins over "Corey".
    rules.sort(key=lambda r: -len(r.pattern.pattern))
    return rules


def respell(text: str, rules: list[Respelling]) -> str:
    for rule in rules:
        text = rule.pattern.sub(rule.spoken, text)
    return text


def _split_long(sentence: str, limit: int) -> list[str]:
    if len(sentence) <= limit:
        return [sentence]
    pieces, current = [], ""
    for part in re.split(r"(?<=[,;:])\s+", sentence):
        if current and len(current) + 1 + len(part) > limit:
            pieces.append(current)
            current = part
        else:
            current = f"{current} {part}".strip()
    if current:
        pieces.append(current)
    out = []
    for piece in pieces:
        while len(piece) > limit:
            cut = piece.rfind(" ", 0, limit)
            cut = cut if cut > limit // 2 else limit
            out.append(piece[:cut].strip())
            piece = piece[cut:].strip()
        if piece:
            out.append(piece)
    return out


def chunks(text: str, limit: int = CHUNK_CHARS) -> list[str]:
    """Pieces of whole sentences, each about `limit` characters at most. A paragraph break
    ends a piece, except that a piece under MIN_CHARS (a heading, "Decisions.") is joined to
    the next: it still ends in a full stop, so it is read as its own sentence."""
    result: list[str] = []
    for paragraph in (p.strip() for p in text.split("\n")):
        if not paragraph:
            continue
        current = ""
        for sentence in _SENTENCE_END.split(paragraph):
            for piece in _split_long(sentence.strip(), limit):
                if current and len(current) + 1 + len(piece) > limit:
                    result.append(current)
                    current = piece
                else:
                    current = f"{current} {piece}".strip()
        if current:
            result.append(current)
    return _merge_fragments(result, limit)


def _merge_fragments(pieces: list[str], limit: int) -> list[str]:
    """Join pieces shorter than MIN_CHARS onto the next one when both fit: models say very
    short inputs badly (a heading alone often comes out rushed or clipped)."""
    out: list[str] = []
    carry = ""
    for piece in pieces:
        piece = f"{carry} {piece}".strip() if carry else piece
        carry = ""
        if len(piece) < MIN_CHARS:
            carry = piece
            continue
        out.append(piece)
    if carry:
        if out and len(out[-1]) + 1 + len(carry) <= limit:
            out[-1] = f"{out[-1]} {carry}"
        else:
            out.append(carry)
    return out
