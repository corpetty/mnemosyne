"""Spot keywords (usually your own name) in live transcript lines."""

from __future__ import annotations

import re


def parse_keywords(text: str) -> list[str]:
    """Comma- or newline-separated keywords; blanks and duplicates dropped."""
    out: list[str] = []
    for part in re.split(r"[,\n]", text or ""):
        word = " ".join(part.split())
        if word and word.casefold() not in {w.casefold() for w in out}:
            out.append(word)
    return out


def keyword_pattern(keyword: str) -> re.Pattern:
    """Whole words, any case, any run of spaces between words, either apostrophe."""
    body = re.escape(keyword).replace(r"\ ", r"\s+").replace("'", "['\u2019]")
    return re.compile(r"(?<!\w)" + body + r"(?!\w)", re.I)


class MentionSpotter:
    """Finds the first keyword in a line, whole words only, case-insensitive, and stays
    quiet about a keyword for `cooldown` seconds of recording after it fired."""

    def __init__(self, keywords: list[str], cooldown: float = 20.0):
        self.keywords = keywords
        self.cooldown = cooldown
        self._patterns = [(k, keyword_pattern(k)) for k in keywords]
        self._last: dict[str, float] = {}

    def __bool__(self) -> bool:
        return bool(self._patterns)

    def find(self, text: str, at: float) -> str | None:
        for keyword, pattern in self._patterns:
            if pattern.search(text):
                last = self._last.get(keyword)
                if last is not None and at - last < self.cooldown:
                    return None
                self._last[keyword] = at
                return keyword
        return None
