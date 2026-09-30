"""Re-diarize a recording in progress and correct the live transcript's speaker labels.

Live lines get a speaker at once from online clustering of voice embeddings
(live_speakers.py), which is quick but coarse. With a fast diarizer (Nemotron on a GPU
diarizes a 38-minute meeting in about 4 s) we can do better: every `interval` seconds, diarize
everything a source has recorded so far with the same model as the final transcript, give each
live line the speaker it overlaps most, and send the lines whose speaker changed as a
`live_labels` event. Labels stay stable between passes: each diarized speaker takes the label
most of its lines already have (a voice-profile name when one matches), and the clusterer is
renamed to agree, so new lines continue with the same names until the next pass.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections import Counter, defaultdict
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from .engine import SpeakerTurn
from .live import WavTail

if TYPE_CHECKING:
    from .live import LiveSource, LiveTranscriber

logger = logging.getLogger(__name__)

Emit = Callable[[dict], None]
MatchNames = Callable[[dict[str, list[float]]], dict[str, str]]


def _overlap(a0: float, a1: float, b0: float, b1: float) -> float:
    return max(0.0, min(a1, b1) - max(a0, b0))


def speaker_of(start: float, end: float, turns: list[SpeakerTurn]) -> str | None:
    """The turn speaker overlapping [start, end] most, or the nearest one."""
    best, best_overlap = None, 0.0
    for t in turns:
        o = _overlap(start, end, t.start, t.end)
        if o > best_overlap:
            best, best_overlap = t.speaker, o
    if best is None and turns:
        mid = (start + end) / 2
        best = min(turns, key=lambda t: min(abs(t.start - mid), abs(t.end - mid))).speaker
    return best


class LiveRediarizer:
    def __init__(
        self,
        diarizer: Any,  # has diarize_pcm(pcm, sample_rate) -> DiarizationResult
        live: LiveTranscriber,
        emit: Emit,
        session_id: str,
        interval: float = 30.0,
        min_seconds: float = 20.0,
        window: float = 20 * 60,
        match_names: MatchNames | None = None,
    ):
        self.diarizer = diarizer
        self.live = live
        self.emit = emit
        self.session_id = session_id
        self.interval = interval
        self.min_seconds = min_seconds
        # Each pass re-diarizes the last `window` seconds: the cost of a pass stays flat
        # however long the meeting runs (the whole recording, every 30 s, grew without bound).
        self.window = window
        self.match_names = match_names
        self.passes = 0
        self.last_seconds = 0.0  # how long the last pass took

    @property
    def sources(self) -> list[LiveSource]:
        # Sources with a Nemotron stream already have its speakers (live_streaming.py).
        return [s for s in self.live.sources if s.diarize and s.stream is None]

    async def run(self) -> None:
        """Pass after pass until cancelled. A failing diarizer stops the loop (the live
        labels from clustering carry on)."""
        try:
            await self.diarizer.load()
        except Exception:
            logger.warning("Live re-diarization unavailable", exc_info=True)
            return
        while True:
            # Never spend more than about a third of the time diarizing.
            await asyncio.sleep(max(self.interval, self.last_seconds * 3))
            started = time.monotonic()
            try:
                for source in self.sources:
                    await self.pass_once(source)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.warning("Live re-diarization failed; stopping it", exc_info=True)
                return
            self.last_seconds = time.monotonic() - started

    async def pass_once(self, source: LiveSource) -> int:
        """Re-diarize one source's audio so far. Returns how many lines changed speaker."""
        tail = WavTail(source.path)
        pcm, offset = await asyncio.to_thread(tail.read_last, self.window)
        rate = tail.sample_rate or 48000
        if pcm.size / rate < self.min_seconds:
            return 0
        end = offset + pcm.size / rate
        result = await self.diarizer.diarize_pcm(pcm, rate)
        self.passes += 1
        lines = [
            i
            for i, kind in enumerate(self.live.committed_kind)
            if kind == source.kind
            and self.live.committed[i].start >= offset
            and self.live.committed[i].end <= end
        ]
        if not result.turns or not lines:
            return 0
        raw = {
            i: speaker_of(
                self.live.committed[i].start - offset,
                self.live.committed[i].end - offset,
                result.turns,
            )
            for i in lines
        }
        names = self.match_names(dict(result.embeddings)) if self.match_names else {}
        display = self._display_labels(raw, names, source)
        changes = []
        moved: dict[str, Counter] = defaultdict(Counter)
        for i in lines:
            seg = self.live.committed[i]
            new = display.get(raw[i] or "", seg.speaker)
            moved[seg.speaker][new] += 1
            if new != seg.speaker:
                changes.append({"start": seg.start, "old": seg.speaker, "new": new})
                seg.speaker = new
        self._teach_clusterer(moved)
        if changes:
            self.emit(
                {
                    "type": "live_labels",
                    "session_id": self.session_id,
                    "source": source.kind,
                    "labels": changes,
                }
            )
        logger.info(
            "Live re-diarization of %.0f s (%s): %d speakers, %d of %d lines relabelled",
            end - offset,
            source.kind,
            len(set(display.values())),
            len(changes),
            len(lines),
        )
        return len(changes)

    def _display_labels(
        self, raw: dict[int, str | None], names: dict[str, str], source: LiveSource
    ) -> dict[str, str]:
        """Raw diarizer label -> the label shown. Voice-profile names first; then, from the
        speaker with the most lines down, the label most of its lines already carry; else a
        new "Speaker N"."""
        current: dict[str, Counter] = defaultdict(Counter)
        for i, r in raw.items():
            if r is not None:
                seg = self.live.committed[i]
                current[r][seg.speaker] += seg.end - seg.start
        display: dict[str, str] = {}
        used: set[str] = set()
        for r, name in names.items():
            if r in current and name not in used:
                display[r] = name
                used.add(name)
        for r in sorted(current, key=lambda k: -sum(current[k].values())):
            if r in display:
                continue
            for label, _ in current[r].most_common():
                if label not in used and label != source.speaker:
                    display[r] = label
                    used.add(label)
                    break
        taken = used | {s.speaker for s in self.live.committed}
        n = 1
        for r in current:
            if r in display:
                continue
            while f"Speaker {n}" in taken:
                n += 1
            display[r] = f"Speaker {n}"
            taken.add(display[r])
        return display

    def _teach_clusterer(self, moved: dict[str, Counter]) -> None:
        """Rename clusters whose lines mostly moved to another label, so lines committed
        before the next pass already get it."""
        clusterer = getattr(self.live, "clusterer", None)
        if clusterer is None:
            return
        for old, targets in moved.items():
            new, count = targets.most_common(1)[0]
            if new != old and count > sum(targets.values()) / 2:
                clusterer.rename(old, new)
