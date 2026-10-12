"""Words that are the name of a note in the user's vault become [[links]] (`obsidian_link_notes`).

Pure text processing: `link_known_notes(text, names)`. Which names qualify is decided by
vault_index.linkable_notes."""

from __future__ import annotations

import re
from collections.abc import Collection, Iterable

# What is never linked inside: fenced and inline code, [[wikilinks]] (and ![[embeds]]),
# [markdown](links) and images, <autolinks> and bare URLs.
_SKIP = (
    r"```.*?```|~~~.*?~~~"
    r"|`[^`\n]+`"
    r"|\[\[.*?\]\]"
    r"|!?\[[^\]\n]*\]\([^)\n]*\)"
    r"|<[A-Za-z/][^<>\n]*>"
    r"|(?:https?|ftp|file|obsidian)://[^\s<>)\]]+"
    r"|www\.[^\s<>)\]]+"
)
# A whole word, whatever the name is made of (spaces, hyphens, dots): not part of a longer word
# ("Status" in "Statuses", "v0.3" in "v0.35" or "v0.3.1", "App" in "Status-App"), nor a #tag,
# @mention or part of a path.
_BEFORE = r"(?<![\w#@/\\-])(?<!\w\.)"
_AFTER = r"(?!\w|[.-]\w)"


def _compile(names: Iterable[str]) -> re.Pattern[str]:
    ordered = sorted(
        names, key=lambda n: (-len(n), n)
    )  # longest first: "Status App" beats "Status"
    alternatives = "|".join(re.escape(n) for n in ordered)
    return re.compile(rf"(?P<skip>{_SKIP})|{_BEFORE}(?P<name>{alternatives}){_AFTER}", re.DOTALL)


def link_known_notes(text: str, names: Collection[str], seen: set[str] | None = None) -> str:
    """`text` with the first occurrence of each of `names` made a [[link]]. Case-sensitive,
    whole words, longest names first, never inside code, URLs, markdown links or existing
    [[links]]. `seen` collects the names already linked: pass the same set for the parts of one
    section so each name is linked once per section."""
    if not text or not names:
        return text
    seen = set() if seen is None else seen
    # Names already linked stay in the pattern: their later occurrences must not link the
    # shorter name inside them ("Status App" seen, "Status" must not link in the next one).
    present = [n for n in names if n in text]  # cheap: most names are absent
    if not present or all(n in seen for n in present):
        return text
    pattern = _compile(present)

    def sub(m: re.Match[str]) -> str:
        name = m.group("name")
        if name is None:  # code, a URL or a link already
            return m.group(0)
        if name in seen:
            return name
        seen.add(name)
        return f"[[{name}]]"

    return pattern.sub(sub, text)


def link_topic(topic: str, names: Collection[str]) -> str:
    """A frontmatter topic as `[[Topic]]` when a note has exactly that name, else as it is."""
    return f"[[{topic}]]" if topic in names else topic
