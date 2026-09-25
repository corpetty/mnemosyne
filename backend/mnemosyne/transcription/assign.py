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
    split: bool = True,
) -> list[TranscriptSegment]:
    """Return new segments with `speaker` set from the turns.

    Without word timings a segment takes the turn it overlaps most. With word timings each
    word takes its own speaker, and a segment in which the speaker changes is split at the
    change (`split`): transcribers cut segments at pauses, not at speaker changes, so one
    speaker per segment caps speaker accuracy at about 95% even with perfect diarization.
    Per word it measured 98.4% with pyannote and 99.6% with Nemotron on four AMI meetings
    (94.1% / 94.3% per segment). Words that overlap no turn take the nearest one when
    `fill_nearest` is set; otherwise they join the neighbouring run.
    """
    if not turns:
        return [s.model_copy(update={"speaker": UNKNOWN}) for s in segments]

    out: list[TranscriptSegment] = []
    for seg in segments:
        if not seg.words:
            speaker = _best_speaker(seg.start, seg.end, turns, fill_nearest)
            out.append(seg.model_copy(update={"speaker": speaker}))
            continue
        labels = _fill_unknown(
            [_best_speaker(w.start, w.end, turns, fill_nearest) for w in seg.words]
        )
        if not split or len(set(labels)) == 1:
            out.append(seg.model_copy(update={"speaker": _majority(seg, labels)}))
            continue
        out.extend(_split(seg, labels))
    return out


def _fill_unknown(labels: list[str]) -> list[str]:
    """Words no turn covers take the previous word's speaker (or the next one's)."""
    known = [x for x in labels if x != UNKNOWN]
    if not known:
        return labels
    out, last = [], known[0]
    for x in labels:
        last = x if x != UNKNOWN else last
        out.append(last)
    return out


def _majority(seg: TranscriptSegment, labels: list[str]) -> str:
    votes: dict[str, float] = {}
    for w, spk in zip(seg.words or [], labels, strict=True):
        votes[spk] = votes.get(spk, 0.0) + max(w.end - w.start, 0.01)
    return max(votes.items(), key=lambda kv: kv[1])[0]


def _split(seg: TranscriptSegment, labels: list[str]) -> list[TranscriptSegment]:
    """One segment per run of consecutive words with the same speaker."""
    words = seg.words or []
    runs: list[tuple[str, int, int]] = []  # speaker, first word, last word + 1
    for i, spk in enumerate(labels):
        if runs and runs[-1][0] == spk:
            runs[-1] = (spk, runs[-1][1], i + 1)
        else:
            runs.append((spk, i, i + 1))
    parts = []
    for n, (spk, a, b) in enumerate(runs):
        chunk = words[a:b]
        parts.append(
            TranscriptSegment(
                text=" ".join(w.word.strip() for w in chunk if w.word.strip()),
                speaker=spk,
                start=seg.start if n == 0 else chunk[0].start,
                end=seg.end if n == len(runs) - 1 else chunk[-1].end,
                words=chunk,
            )
        )
    return parts


def relabel(segments: list[TranscriptSegment], speaker: str) -> list[TranscriptSegment]:
    return [s.model_copy(update={"speaker": speaker}) for s in segments]
