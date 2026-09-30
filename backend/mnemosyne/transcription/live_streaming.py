"""Live speaker labels from Nemotron streaming (diarizers/nemotron_stream.py).

Each diarized source keeps a `SpeakerTimeline`: for every 10 ms of the recording, the streaming
speaker index most active there (or nobody). A live line takes the speaker most active over its
span, or, when the transcriber gave word times, each word takes its own and the line is split
where the speaker changes. `LiveSpeakerNames` turns a source's speaker index into the label
people see ("Speaker 3"); streaming indices are stable for the whole recording, so labels never
need clustering or renaming to stay consistent. `VoiceNamer` then renames them once a speaker
has said enough: to the label they had in an earlier part of the same meeting (each part is a
new recording, so a new stream), or to a voice-profile name.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

import numpy as np

from ..models.transcript import TranscriptSegment, WordSegment
from .diarizers.nemotron_stream import FRAME_SECONDS, SpeakerFrames
from .engine import SpeakerTurn

if TYPE_CHECKING:
    from .live import LiveSource, LiveTranscriber

logger = logging.getLogger(__name__)

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


MIN_NAMING_SECONDS = 3.0  # clean speech before a speaker is first embedded
RETRY_SECONDS = 60.0  # more clean speech before trying a profile match again

MatchNames = Callable[[dict[str, list[float]]], dict[str, str]]


def _unit(v) -> np.ndarray:
    v = np.asarray(v, dtype=np.float64)
    return v / (np.linalg.norm(v) or 1.0)


class VoiceNamer:
    """Names streamed speakers by voice: first as the same speaker of an earlier part of this
    meeting (`voices`: label -> embedding, shared across the meeting's recordings and updated
    here), then by voice profile (`match_names`, e.g. SpeakerService.match). Embeds each
    speaker's longest clean turns with the pyannote embedder, at most one speaker per tick."""

    def __init__(
        self,
        embedder: Any,
        match_names: MatchNames | None = None,
        voices: dict[str, list[float]] | None = None,
        threshold: float = 0.6,
    ):
        self.embedder = embedder
        self.match_names = match_names
        self.voices = voices if voices is not None else {}
        self.threshold = threshold
        self.tried: dict[tuple[str, int], float] = {}  # clean seconds at the last attempt
        self.named: set[tuple[str, int]] = set()  # matched a voice profile: nothing left to do
        self.failed = False

    async def update(self, live: LiveTranscriber, source: LiveSource) -> None:
        if self.embedder is None or self.failed or source.timeline is None:
            return
        try:
            await self._update(live, source)
        except Exception:
            logger.warning("Live speaker naming failed; labels stay numbered", exc_info=True)
            self.failed = True

    async def _update(self, live: LiveTranscriber, source: LiveSource) -> None:
        from .diarizers.nemotron import embedding_spans

        timeline = source.timeline
        for index in timeline.speakers():
            key = (source.kind, index)
            if key in self.named or key not in live.names.labels:
                continue  # done, or no line of theirs shown yet
            seconds = timeline.seconds(index)
            last = self.tried.get(key)
            if seconds < MIN_NAMING_SECONDS:
                continue
            if last is not None and seconds - last < RETRY_SECONDS:
                continue
            self.tried[key] = seconds
            turns = [t for t in timeline.turns(clean=True) if t.speaker == str(index)]
            vecs = []
            for start, end in embedding_spans(turns).get(str(index), []):
                pcm = source.tail.read_span(start, end)
                vec = await self.embedder.embed(pcm, source.tail.sample_rate or 48000)
                if vec is not None:
                    vecs.append(_unit(vec))
            if not vecs:
                continue
            vec = _unit(np.mean(vecs, axis=0)).tolist()
            label = live.names.labels[key]
            if last is None:  # first look: the same person as in an earlier part?
                label = self._continue(live, label, vec)
            name = (self.match_names({label: vec}) if self.match_names else {}).get(label)
            if name and name != label:
                live.rename(label, name)
                label = live.names.labels[key]
            if name and label == name:
                self.named.add(key)
            self.voices[label] = vec
            return  # one embedding pass per tick

    def _continue(self, live: LiveTranscriber, label: str, vec: list[float]) -> str:
        """The label of the earlier part's speaker this voice matches, taken over if free."""
        in_use = set(live.names.labels.values())
        best, best_score = None, self.threshold
        for other, emb in self.voices.items():
            if other == label or other in in_use:
                continue
            score = float(np.dot(_unit(emb), _unit(vec)))
            if score >= best_score:
                best, best_score = other, score
        if best is None:
            return label
        live.rename(label, best)
        return best
