"""Live speaker labels from Nemotron streaming (diarizers/nemotron_stream.py).

Each diarized source keeps a `SpeakerTimeline`: for every 10 ms of the recording, the streaming
speaker index most active there (or nobody). A live line takes the speaker most active over its
span, or, when the transcriber gave word times, each word takes its own and the line is split
where the speaker changes. `LiveSpeakerNames` turns a source's speaker index into the label
people see ("Speaker 3"); streaming indices are stable for the whole recording, so labels never
need clustering or renaming to stay consistent.
"""

from __future__ import annotations

import numpy as np

from ..models.transcript import TranscriptSegment, WordSegment
from .diarizers.nemotron_stream import FRAME_SECONDS, SpeakerFrames
from .engine import SpeakerTurn

ACTIVE = 0.5  # a speaker is talking in a frame when their probability reaches this
NOBODY = 255
MIN_RUN_WORDS = 2  # a line is split only into runs of at least this many words


class SpeakerTimeline:
    """Who is speaking in each 10 ms frame of one source, as far as the stream has got."""

    def __init__(self):
        self._speaker = np.full(4096, NOBODY, dtype=np.uint8)
        self._overlap = np.zeros(4096, dtype=bool)
        self.frames = 0

    @property
    def frontier(self) -> float:
        """Seconds from the start of the source up to which the timeline is known."""
        return self.frames * FRAME_SECONDS

    def add(self, frames: SpeakerFrames) -> None:
        if frames.start != self.frames:
            raise ValueError(f"timeline at frame {self.frames}, got frames from {frames.start}")
        n = len(frames.probs)
        while self.frames + n > self._speaker.size:
            self._speaker = np.concatenate([self._speaker, np.full_like(self._speaker, NOBODY)])
            self._overlap = np.concatenate([self._overlap, np.zeros_like(self._overlap)])
        active = frames.probs >= ACTIVE
        best = frames.probs.argmax(axis=1).astype(np.uint8)
        best[~active.any(axis=1)] = NOBODY
        self._speaker[self.frames : self.frames + n] = best
        self._overlap[self.frames : self.frames + n] = active.sum(axis=1) >= 2
        self.frames += n

    def _slice(self, start: float, end: float) -> np.ndarray:
        a = max(0, int(round(start / FRAME_SECONDS)))
        b = min(self.frames, max(a + 1, int(round(end / FRAME_SECONDS))))
        return self._speaker[a:b]

    def speaker_between(self, start: float, end: float) -> int | None:
        """The speaker most active in [start, end), or None when nobody known is."""
        s = self._slice(start, end)
        s = s[s != NOBODY]
        if s.size == 0:
            return None
        return int(np.bincount(s).argmax())

    def speakers(self) -> list[int]:
        s = self._speaker[: self.frames]
        return sorted(int(x) for x in np.unique(s[s != NOBODY]))

    def turns(
        self, start: float = 0.0, end: float | None = None, clean: bool = False
    ) -> list[SpeakerTurn]:
        """Runs of one speaker in [start, end) as turns labelled by speaker index; with
        `clean`, only frames where nobody else talks."""
        a = max(0, int(round(start / FRAME_SECONDS)))
        b = self.frames if end is None else min(self.frames, int(round(end / FRAME_SECONDS)))
        if b <= a:
            return []
        s = self._speaker[a:b].copy()
        if clean:
            s[self._overlap[a:b]] = NOBODY
        change = np.flatnonzero(np.diff(s.astype(np.int16))) + 1
        bounds = np.concatenate([[0], change, [s.size]])
        out = []
        for lo, hi in zip(bounds[:-1], bounds[1:], strict=True):
            if s[lo] != NOBODY:
                out.append(
                    SpeakerTurn(
                        start=(a + lo) * FRAME_SECONDS,
                        end=(a + hi) * FRAME_SECONDS,
                        speaker=str(int(s[lo])),
                    )
                )
        return out

    def seconds(self, index: int, clean: bool = True) -> float:
        """How long `index` has spoken (alone, with `clean`)."""
        s = self._speaker[: self.frames] == index
        if clean:
            s &= ~self._overlap[: self.frames]
        return float(s.sum()) * FRAME_SECONDS


def word_speakers(words: list[WordSegment], timeline: SpeakerTimeline) -> list[int | None]:
    """Each word's speaker: the one most active under it, else its neighbours'; runs shorter
    than MIN_RUN_WORDS join the run before them (or after, at the start)."""
    labels = [timeline.speaker_between(w.start, w.end) for w in words]
    known = [x for x in labels if x is not None]
    if not known:
        return labels
    filled, last = [], known[0]
    for x in labels:
        last = x if x is not None else last
        filled.append(last)
    # Collapse short runs until none are left (each pass removes at least one run).
    while True:
        runs: list[list[int]] = []  # [speaker, first, end]
        for i, x in enumerate(filled):
            if runs and runs[-1][0] == x:
                runs[-1][2] = i + 1
            else:
                runs.append([x, i, i + 1])
        short = next(
            (k for k, r in enumerate(runs) if r[2] - r[1] < MIN_RUN_WORDS and len(runs) > 1),
            None,
        )
        if short is None:
            return filled
        spk = runs[short - 1][0] if short > 0 else runs[short + 1][0]
        for i in range(runs[short][1], runs[short][2]):
            filled[i] = spk


def split_line(
    seg: TranscriptSegment, timeline: SpeakerTimeline
) -> list[tuple[TranscriptSegment, int | None]]:
    """A committed line (absolute times) as one or more lines with their speaker index."""
    if seg.words and len(seg.words) >= 2 * MIN_RUN_WORDS:
        from .assign import split_by_labels

        labels = word_speakers(seg.words, timeline)
        if all(x is not None for x in labels):
            if len(set(labels)) == 1:
                return [(seg, labels[0])]
            parts = split_by_labels(seg, [str(x) for x in labels])
            return [(p, int(p.speaker)) for p in parts]
    return [(seg, timeline.speaker_between(seg.start, seg.end))]


class LiveSpeakerNames:
    """Display labels for streaming speakers: (source kind, index) -> "Speaker n"."""

    def __init__(self):
        self.labels: dict[tuple[str, int], str] = {}

    def label(self, source: str, index: int, taken: set[str] = frozenset()) -> str:
        key = (source, index)
        if key not in self.labels:
            used = set(self.labels.values()) | set(taken)
            n = 1
            while f"Speaker {n}" in used:
                n += 1
            self.labels[key] = f"Speaker {n}"
        return self.labels[key]

    def rename(self, old: str, new: str) -> bool:
        """Show `new` wherever `old` was shown, unless `new` is already someone else's."""
        if old == new or new in self.labels.values():
            return False
        hit = False
        for key, label in self.labels.items():
            if label == old:
                self.labels[key] = new
                hit = True
        return hit
