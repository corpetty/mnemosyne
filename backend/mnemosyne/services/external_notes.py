"""Notes other assistants wrote about a meeting: Gemini in Google Meet ("Notes by Gemini"),
Zoom AI Companion, Otter, Microsoft Teams Copilot, Fireflies, Fathom and the like.

They reach the summary as extra context: the transcript wins where they disagree, and they
fill in what the recording missed (before it started, a dropped call). A meeting with such
notes and no recording is summarized from the notes alone.
"""

from __future__ import annotations

import re

from ..models.session import ExternalNotes
from ..models.transcript import TranscriptSegment

MAX_TEXT = 100_000
# (source, words that give it away near the top of the notes)
SOURCES = [
    ("Gemini", ("notes by gemini", "gemini")),
    ("Zoom", ("zoom ai companion", "quick recap", "meeting summary for")),
    ("Otter", ("otter.ai", "otter")),
    ("Teams Copilot", ("microsoft teams", "copilot")),
    ("Fireflies", ("fireflies",)),
    ("Fathom", ("fathom",)),
    ("Granola", ("granola",)),
    ("Read AI", ("read.ai", "read ai")),
    ("tl;dv", ("tl;dv", "tldv")),
]


def detect_source(text: str) -> str:
    head = text[:600].lower()
    for source, words in SOURCES:
        if any(w in head for w in words):
            return source
    return "Other"


def clean(text: str) -> str:
    text = text.replace("\r\n", "\n")
    return re.sub(r"\n{3,}", "\n\n", text).strip()[:MAX_TEXT]


def notes_hint(notes: list[ExternalNotes], budget: int = 12000) -> str:
    """The notes as extra instructions for a summary made from the transcript."""
    if not notes:
        return ""
    parts, left = [], budget
    for n in notes:
        excerpt = n.text[: max(0, left)]
        if not excerpt:
            break
        parts.append(f"--- Notes by {n.source} ---\n{excerpt}")
        left -= len(excerpt)
    return (
        "Another assistant also took notes of this meeting (below). They may be wrong or "
        "incomplete: where they disagree with the transcript, the transcript wins. Use them "
        "to fill in what the transcript misses (for example before the recording started).\n"
        + "\n".join(parts)
    )


def notes_as_transcript(notes: list[ExternalNotes]) -> list[TranscriptSegment]:
    """For a meeting with no recording: the notes, paragraph by paragraph, in the shape the
    summarizer reads (the instructions say they are notes, not speech)."""
    out: list[TranscriptSegment] = []
    for n in notes:
        for para in re.split(r"\n\s*\n", n.text):
            if para.strip():
                i = len(out)
                out.append(
                    TranscriptSegment(
                        text=para.strip(), speaker=f"{n.source} notes", start=i, end=i + 1
                    )
                )
    return out


NOTES_ONLY = (
    "This meeting has no recording. What follows as the transcript is notes another assistant "
    "wrote about it (each line a paragraph of those notes, not something said). Summarize the "
    "meeting from them. Times in the notes are not transcript times: leave every `at` empty."
)
