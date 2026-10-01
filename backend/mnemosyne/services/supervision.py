"""Supervision (advisory pilot, item 10): lines of a meeting's final transcript that use a
compliance phrase ("guarantee", "can't lose", "you should buy", `compliance_phrases`) are
flagged, and a reviewer (compliance) works through the flagged meetings, marking each reviewed
with a note. A flag is a line to look at, not a finding: "I can't guarantee returns" is flagged
too, and the reviewer decides.

Flags follow the transcript: a new transcription replaces them (it comes from the audio); an
edit or a change of phrases only adds, so editing a line never hides what was said. A review
covers the flags found before it; a flag found later puts the meeting back in the queue.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from ..transcription.mentions import keyword_pattern, parse_keywords

if TYPE_CHECKING:
    from ..api.context import AppContext
    from ..config import Settings
    from ..models.transcript import TranscriptSegment

logger = logging.getLogger(__name__)


def enabled(settings: Settings) -> bool:
    return settings.supervision


def find(segments: list[TranscriptSegment], phrases: list[str]) -> list[dict]:
    """Every line with a phrase, once per phrase it uses."""
    patterns = [(p, keyword_pattern(p)) for p in phrases]
    return [
        {"idx": i, "start": s.start, "speaker": s.speaker or "", "phrase": p, "text": s.text}
        for i, s in enumerate(segments)
        for p, pattern in patterns
        if pattern.search(s.text)
    ]


def scan(app: AppContext, session_id: str, replace: bool = False) -> int:
    """Flag the meeting's transcript as it is now; how many flags are new. Never raises:
    supervision must not fail the transcription or edit it follows."""
    if not enabled(app.settings):
        return 0
    try:
        session = app.repo.get(session_id)
        if session is None:
            return 0
        phrases = parse_keywords(app.settings.compliance_phrases)
        return app.repo.set_flags(session_id, find(session.transcript, phrases), replace)
    except Exception:
        logger.warning("Could not flag session %s", session_id, exc_info=True)
        return 0


def scan_all(app: AppContext, progress=None) -> dict:
    """Every meeting against the current phrases (after they change): adds, never removes."""
    ids = app.repo.session_ids()
    new = 0
    for n, sid in enumerate(ids, 1):
        new += scan(app, sid)
        if progress:
            progress(n / max(len(ids), 1))
    return {"meetings": len(ids), "new_flags": new}
