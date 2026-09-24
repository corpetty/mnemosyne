"""Attach speaker labels to transcript segments from diarization turns."""

from __future__ import annotations

from ..models.transcript import TranscriptSegment
from .engine import SpeakerTurn

UNKNOWN = "UNKNOWN"


def _overlap(a0: float, a1: float, b0: float, b1: float) -> float:
    return max(0.0, min(a1, b1) - max(a0, b0))


def _best_speaker(start: float, end: float, turns: list[SpeakerTurn], fill_nearest: bool) -> str:
    best, best_overlap = None, 0.0
    for t in turns:
        o = _overlap(start, end, t.start, t.end)
        if o > best_overlap:
            best, best_overlap = t.speaker, o
    if best is not None:
        return best
    if fill_nearest and turns:
        mid = (start + end) / 2
        nearest = min(turns, key=lambda t: min(abs(t.start - mid), abs(t.end - mid)))
        return nearest.speaker
    return UNKNOWN


def assign_speakers(
    segments: list[TranscriptSegment],
    turns: list[SpeakerTurn],
    fill_nearest: bool = True,
) -> list[TranscriptSegment]:
    """Return new segments with `speaker` set from the turns.

    A segment's speaker is the turn with the largest time overlap. When the
    segment has word timings, each word is scored and the segment takes the
    majority speaker, which handles a speaker change mid-segment better than
    the segment-level overlap alone. Segments that overlap no turn take the
    nearest one when `fill_nearest` is set.
    """
    if not turns:
        return [s.model_copy(update={"speaker": UNKNOWN}) for s in segments]

    out = []
    for seg in segments:
        if seg.words:
            votes: dict[str, float] = {}
            for w in seg.words:
                spk = _best_speaker(w.start, w.end, turns, fill_nearest)
                votes[spk] = votes.get(spk, 0.0) + max(w.end - w.start, 0.01)
            speaker = max(votes.items(), key=lambda kv: kv[1])[0]
        else:
            speaker = _best_speaker(seg.start, seg.end, turns, fill_nearest)
        out.append(seg.model_copy(update={"speaker": speaker}))
    return out


def relabel(segments: list[TranscriptSegment], speaker: str) -> list[TranscriptSegment]:
    return [s.model_copy(update={"speaker": speaker}) for s in segments]
