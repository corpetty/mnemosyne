"""Remove speaker bleed: mic segments that merely repeat what the speakers played.

When mic and system audio are captured separately without echo cancellation,
the mic hears the room, including the remote participants coming out of the
speakers. Those mic segments overlap the system segments in time and carry
(nearly) the same words. Drop them; keep everything the local user actually said.
"""

from __future__ import annotations

import re
from difflib import SequenceMatcher

from ..models.transcript import TranscriptSegment

_PUNCT = re.compile(r"[^\w\s']", re.UNICODE)


def normalize(text: str) -> str:
    return " ".join(_PUNCT.sub(" ", text.lower()).split())


def _contains_run(needle: list[str], haystack: list[str]) -> bool:
    n = len(needle)
    return any(haystack[i : i + n] == needle for i in range(len(haystack) - n + 1))


def similarity(a: str, b: str) -> float:
    """1.0 when the words of `a` appear as a contiguous run in the words of `b`;
    otherwise the longest common run of words, as a fraction of `a`'s words, or the
    word-level SequenceMatcher ratio if that is higher (robust to partial overlaps).
    Works on whole words, so "so" is not found inside "also" and "no" not in "know"."""
    wa, wb = normalize(a).split(), normalize(b).split()
    if not wa or not wb:
        return 0.0
    if _contains_run(wa, wb):
        return 1.0
    m = SequenceMatcher(None, wa, wb, autojunk=False)
    longest = m.find_longest_match(0, len(wa), 0, len(wb)).size
    return max(longest / len(wa), m.ratio())


def remove_echo(
    mic: list[TranscriptSegment],
    reference: list[TranscriptSegment],
    threshold: float = 0.8,
    tolerance: float = 1.0,
) -> tuple[list[TranscriptSegment], list[TranscriptSegment]]:
    """Return (kept, dropped) mic segments.

    A mic segment is dropped when the reference segments overlapping it in time
    (widened by `tolerance` seconds, since the two ASR passes and the acoustic
    delay shift timestamps) contain its words with similarity >= threshold.
    """
    if not mic or not reference:
        return list(mic), []
    ref_sorted = sorted(reference, key=lambda s: s.start)
    kept, dropped = [], []
    for seg in mic:
        lo, hi = seg.start - tolerance, seg.end + tolerance
        window = [r for r in ref_sorted if r.end >= lo and r.start <= hi]
        if window and similarity(seg.text, " ".join(r.text for r in window)) >= threshold:
            dropped.append(seg)
        else:
            kept.append(seg)
    return kept, dropped
