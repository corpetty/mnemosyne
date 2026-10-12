"""What the user's vault already has: the names of its notes, scanned once and cached.

Used by person notes (no note for someone who has one) and by `obsidian_link_notes` (words that
are the name of a note become [[links]], see links.py). Walking a large vault on every export
(auto-export runs after each summary) is slow, so the scan is cached for a few minutes."""

from __future__ import annotations

import json
import re
import time
from collections.abc import Iterable
from pathlib import Path

_SCAN_TTL = 600.0
# vault -> (when, [(note name, the folder it is in, relative to the vault, as path parts)])
_scans: dict[Path, tuple[float, list[tuple[str, tuple[str, ...]]]]] = {}


def _scan(vault: Path) -> list[tuple[str, tuple[str, ...]]]:
    hit = _scans.get(vault)
    if hit is not None and time.monotonic() - hit[0] < _SCAN_TTL:
        return hit[1]
    notes = []
    for p in vault.rglob("*.md"):
        rel = p.relative_to(vault).parent.parts
        if ".obsidian" not in rel:
            notes.append((p.stem, rel))
    _scans[vault] = (time.monotonic(), notes)
    return notes


def _parts(folder: Path | str, vault: Path) -> tuple[str, ...]:
    p = Path(folder)
    if p.is_absolute():
        try:
            p = p.relative_to(vault)
        except ValueError:
            return ()
    return tuple(x for x in p.parts if x not in ("", "."))


def vault_stems(vault: Path, exclude: Iterable[Path | str] = ()) -> set[str]:
    """Names of every note in the vault (`.obsidian` aside), except those in the `exclude`
    folders (and below them; relative to the vault, or absolute)."""
    skip = [x for x in (_parts(f, vault) for f in exclude) if x]
    return {stem for stem, rel in _scan(vault) if not any(rel[: len(s)] == s for s in skip)}


# --- notes worth linking to -----------------------------------------------------------------

_MIN_LENGTH = 3
_UNSAFE = re.compile(r"[\[\]|#^\n]")
_DATED = re.compile(r"^\d{4}-\d{2}-\d{2}")
# Single words that name a note in nearly every vault (Home, Index, Inbox) and appear in
# nearly every sentence; a link on each would be noise.
COMMON = frozenset(
    """about archive area areas article articles book books clippings daily dashboard draft
    drafts home ideas inbox index journal list lists meeting meetings monthly note notes
    overview people project projects readme reading resources review summary task tasks template
    templates test todo today tomorrow untitled weekly welcome work yesterday""".split()
)

_MOMENT = {
    "YYYY": r"\d{4}", "YY": r"\d{2}", "MMMM": r"[^\W\d_]+", "MMM": r"[^\W\d_]+",
    "MM": r"\d{2}", "M": r"\d{1,2}", "DD": r"\d{2}", "D": r"\d{1,2}", "Do": r"\d{1,2}\w+",
    "dddd": r"[^\W\d_]+", "ddd": r"[^\W\d_]+", "WW": r"\d{2}", "W": r"\d{1,2}",
    "gggg": r"\d{4}", "GGGG": r"\d{4}", "Q": r"\d",
}  # fmt: skip


def format_regex(fmt: str) -> re.Pattern[str]:
    """A pattern for the names a Moment.js date format (Obsidian's) makes; text in [brackets]
    is literal."""
    out = []
    pieces = re.findall(
        r"\[([^\]]*)\]|(YYYY|YY|MMMM|MMM|MM|M|Do|DD|D|dddd|ddd|WW|W|gggg|GGGG|Q|.)", fmt
    )
    for literal, token in pieces:
        out.append(re.escape(literal) if literal else _MOMENT.get(token) or re.escape(token))
    return re.compile("".join(out))


def daily_pattern(vault: Path) -> re.Pattern[str] | None:
    """The names of daily notes by the vault's Daily Notes format, when it has one."""
    try:
        fmt = json.loads((vault / ".obsidian" / "daily-notes.json").read_text()).get("format")
        return format_regex(fmt) if fmt else None
    except (OSError, ValueError, AttributeError, re.error):
        return None


def linkable(name: str, daily: re.Pattern[str] | None = None) -> bool:
    """May a word that is this note's name become a link? Not the short ones, date-named
    notes, names that are a single lowercase word or a very common one."""
    if len(name) < _MIN_LENGTH or name != name.strip() or _UNSAFE.search(name):
        return False
    if not any(c.isalpha() for c in name) or _DATED.match(name):
        return False
    if daily is not None and daily.fullmatch(name):
        return False
    if name.isalpha() and name.islower():
        return False
    return name.casefold() not in COMMON


def linkable_notes(vault: Path, own_folders: Iterable[str]) -> set[str]:
    """Names of the vault's notes that a meeting note may link to: not those in `own_folders`
    (where we write: meetings, people, topics) and not those `linkable` refuses."""
    daily = daily_pattern(vault)
    return {n for n in vault_stems(vault, own_folders) if linkable(n, daily)}
