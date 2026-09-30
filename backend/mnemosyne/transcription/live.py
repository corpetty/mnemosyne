"""Live transcription of a recording in progress.

`WavTail` reads new PCM from a WAV file that pw-record is still writing.
`LiveTranscriber` keeps a per-source buffer of unprocessed audio, runs a
Transcriber over it every few seconds, and commits the segments that end
safely before the buffer edge (so a word cut by the chunk boundary is retried
on the next tick). Committed segments stream out as provisional events; the
uncommitted tail is sent as a replaceable partial. The full pipeline after
stop produces the authoritative transcript.

Speakers come from one of two places. With a Nemotron stream on a source (live_streaming.py)
every new sample is also pushed to the stream, and lines take the speaker the stream heard
over them; lines that end past what the stream has scored are checked again on the next tick
and corrected with `live_labels`. Otherwise each line is embedded and clustered
(live_speakers.py). A stream that fails hands its source over to clustering.
"""

from __future__ import annotations

import asyncio
import logging
import struct
import tempfile
import time
import wave
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np

from ..models.transcript import TranscriptSegment
from .engine import Transcriber
from .live_speakers import OnlineClusterer, SpeakerEmbedder
from .live_streaming import LiveSpeakerNames, SpeakerTimeline, VoiceNamer, split_line
from .mentions import MentionSpotter

logger = logging.getLogger(__name__)

Emit = Callable[[dict], None]


class WavTail:
    """Incrementally read samples from a growing PCM WAV file."""

    def __init__(self, path: Path):
        self.path = Path(path)
        self.sample_rate = 0
        self.channels = 0
        self.sample_width = 0
        self._data_offset: int | None = None
        self._pos = 0

    def _parse_header(self) -> bool:
        if not self.path.exists():
            return False
        with self.path.open("rb") as f:
            head = f.read(1024)
        if len(head) < 44 or head[:4] != b"RIFF" or head[8:12] != b"WAVE":
            return False
        fmt = head.find(b"fmt ")
        data = head.find(b"data")
        if fmt < 0 or data < 0:
            return False
        _tag, channels, rate, _byte_rate, _align, bits = struct.unpack(
            "<HHIIHH", head[fmt + 8 : fmt + 24]
        )
        self.channels, self.sample_rate, self.sample_width = channels, rate, bits // 8
        self._data_offset = data + 8
        self._pos = self._data_offset
        return True

    def read_last(self, seconds: float) -> tuple[np.ndarray, float]:
        """The last `seconds` of the file as int16 mono, and where they start (seconds from
        the beginning). Reads only that stretch, however long the file is."""
        if self._data_offset is None and not self._parse_header():
            return np.zeros(0, dtype=np.int16), 0.0
        frame = self.sample_width * self.channels
        size = self.path.stat().st_size - self._data_offset
        size -= size % frame
        want = int(seconds * self.sample_rate) * frame
        skip = max(0, size - want)
        self._pos = self._data_offset + skip
        return self.read_new(), skip / frame / self.sample_rate

    def read_new(self) -> np.ndarray:
        """Return new samples as int16 mono (channels averaged). Empty if none."""
        if self._data_offset is None and not self._parse_header():
            return np.zeros(0, dtype=np.int16)
        frame = self.sample_width * self.channels
        with self.path.open("rb") as f:
            f.seek(self._pos)
            raw = f.read()
        usable = len(raw) - (len(raw) % frame)
        self._pos += usable
        return self._mono(raw[:usable])

    def read_span(self, start: float, end: float) -> np.ndarray:
        """Samples between two times (seconds) as int16 mono, without moving the tail."""
        if self._data_offset is None and not self._parse_header():
            return np.zeros(0, dtype=np.int16)
        frame = self.sample_width * self.channels
        a = int(max(start, 0.0) * self.sample_rate)
        b = int(max(end, start) * self.sample_rate)
        with self.path.open("rb") as f:
            f.seek(self._data_offset + a * frame)
            raw = f.read((b - a) * frame)
        return self._mono(raw[: len(raw) - len(raw) % frame])

    def _mono(self, raw: bytes) -> np.ndarray:
        if not raw:
            return np.zeros(0, dtype=np.int16)
        if self.sample_width != 2:
            raise ValueError(f"Unsupported sample width {self.sample_width}")
        pcm = np.frombuffer(raw, dtype=np.int16)
        if self.channels > 1:
            pcm = pcm.reshape(-1, self.channels).mean(axis=1).astype(np.int16)
        return pcm


@dataclass
class LiveSource:
    path: Path
    speaker: str
    kind: str = "mixed"
    diarize: bool = False  # label segments by voice (needs a clusterer + embedder)
    last_speaker: str | None = None
    tail: WavTail = field(init=False)
    buffer: np.ndarray = field(default_factory=lambda: np.zeros(0, dtype=np.int16))
    buffer_start: float = 0.0  # seconds into the recording where `buffer` begins
    partial: str = ""
    failures: int = 0  # ticks failed in a row
    stream: Any = None  # a NemotronStream: speakers from Nemotron streaming instead of clusters
    timeline: SpeakerTimeline | None = None

    def __post_init__(self):
        self.tail = WavTail(self.path)
        if self.stream is not None and self.timeline is None:
            self.timeline = SpeakerTimeline()


def cpu_pressure(path: Path = Path("/proc/pressure/cpu")) -> float | None:
    """Share of the last 10 s in which some task waited for a CPU (Linux PSI), in percent."""
    try:
        for line in path.read_text().splitlines():
            if line.startswith("some "):
                for field_ in line.split()[1:]:
                    key, _, value = field_.partition("=")
                    if key == "avg10":
                        return float(value)
    except (OSError, ValueError):
        pass
    return None


def _rms_db(pcm: np.ndarray) -> float:
    """Loudness of int16 samples in dBFS (-inf-safe: at most -120)."""
    if pcm.size == 0:
        return -120.0
    x = pcm.astype(np.float32) / 32768.0
    rms = float(np.sqrt(np.mean(x * x)))
    return 20 * np.log10(rms) if rms > 1e-6 else -120.0


class LiveTranscriber:
    def __init__(
        self,
        transcriber: Transcriber,
        sources: list[LiveSource],
        emit: Emit,
        session_id: str,
        interval: float = 5.0,
        commit_margin: float = 1.0,
        min_buffer: float = 1.0,
        max_buffer: float = 30.0,
        language: str | None = None,
        embedder: SpeakerEmbedder | None = None,
        clusterer: OnlineClusterer | None = None,
        mentions: MentionSpotter | None = None,
        silence_db: float = -55.0,
        adaptive: bool = True,
        pressure: Callable[[], float | None] = cpu_pressure,
        names: LiveSpeakerNames | None = None,
        namer: VoiceNamer | None = None,
    ):
        self.transcriber = transcriber
        self.sources = sources
        self.emit = emit
        self.session_id = session_id
        self.interval = interval
        self.commit_margin = commit_margin
        self.min_buffer = min_buffer
        self.max_buffer = max_buffer
        self.language = language
        self.embedder = embedder
        self.clusterer = clusterer
        self.mentions = mentions
        self.silence_db = silence_db
        # Back off when ticks cannot keep up or the machine is busy (see _adapt).
        self.adaptive = adaptive
        self.pressure = pressure
        self.base_interval = interval
        self.max_interval = max(interval, min(interval * 4, max_buffer / 2))
        self.slowed_by: str | None = None
        self._fast_ticks = 0
        self._ready_status = "Live"
        self.committed: list[TranscriptSegment] = []
        self.committed_kind: list[str] = []  # source kind of each committed segment
        self.names = names or LiveSpeakerNames()
        self.namer = namer  # voice profiles and earlier parts' speakers, for streamed sources
        self.pending: list[int] = []  # committed lines that end past their stream's frontier

    async def run(self) -> None:
        """Tick until cancelled. A final tick flushes what is left."""
        if not self.transcriber.is_loaded():
            self.emit(self._status("Loading live transcriber..."))
            await self.transcriber.load()
        status = "Live"
        if any(src.stream is not None for src in self.sources):
            status = "Live · speakers by Nemotron"
        elif self.embedder is not None and any(src.diarize for src in self.sources):
            try:
                if not self.embedder.is_loaded():
                    self.emit(self._status("Loading speaker detection..."))
                    await self.embedder.load()
                status = "Live · speaker detection on"
            except Exception as e:
                logger.warning("Live speaker detection unavailable: %s", e)
                self.embedder = None
                status = "Live (speaker detection unavailable)"
        self._ready_status = status
        self.emit(self._status(status))
        try:
            while True:
                await asyncio.sleep(self.interval)
                started = time.monotonic()
                await self.tick()
                self._adapt(time.monotonic() - started)
        except asyncio.CancelledError:
            try:
                await self.tick(flush=True)
            except Exception:
                logger.debug("Final live tick failed", exc_info=True)
            raise

    async def tick(self, flush: bool = False) -> None:
        for source in self.sources:
            try:
                await self._tick_source(source, flush)
                source.failures = 0
            except Exception:
                source.failures += 1
                if source.failures in (1, 10, 100, 1000):  # not a traceback every tick
                    logger.exception(
                        "Live tick failed for %s (%d in a row)", source.path, source.failures
                    )
                # What could not be transcribed is dropped beyond max_buffer: a transcriber
                # that keeps failing (out of GPU memory) must not grow the buffer forever.
                rate = source.tail.sample_rate or 48000
                keep = int(self.max_buffer * rate)
                if source.buffer.size > keep:
                    source.buffer_start += (source.buffer.size - keep) / rate
                    source.buffer = source.buffer[-keep:]

    async def _tick_source(self, source: LiveSource, flush: bool) -> None:
        new = source.tail.read_new()
        if new.size:
            source.buffer = np.concatenate([source.buffer, new])
        rate = source.tail.sample_rate or 48000
        # Every sample goes to the stream, silence included, so its timeline stays aligned.
        await self._push_stream(source, new, rate, flush)
        duration = source.buffer.size / rate
        if duration < self.min_buffer:
            return
        if not flush and _rms_db(source.buffer) < self.silence_db:
            # Nobody is talking (EasyEffects' noise suppression makes the mic exact digital
            # silence): skip the transcriber and keep only a short tail for the next words.
            keep = int(self.commit_margin * rate)
            if source.buffer.size > keep:
                source.buffer_start += (source.buffer.size - keep) / rate
                source.buffer = source.buffer[-keep:]
            if source.partial:
                source.partial = ""
                self.emit(
                    {
                        "type": "live_partial",
                        "session_id": self.session_id,
                        "source": source.kind,
                        "speaker": source.last_speaker or source.speaker,
                        "text": "",
                    }
                )
            return

        segments = await self._transcribe_buffer(source, rate)
        segments.sort(key=lambda s: s.start)

        cutoff = duration if flush else duration - self.commit_margin
        commit = [s for s in segments if s.end <= cutoff]
        if not commit and duration >= self.max_buffer:
            commit = segments
        rest = [s for s in segments if s not in commit]

        for seg in commit:
            if source.stream is not None:
                lines = self._stream_lines(source, seg)
            else:
                speaker = await self._label(source, seg, rate)
                lines = [self._absolute(source, seg).model_copy(update={"speaker": speaker})]
            for line in lines:
                line = line.model_copy(update={"words": None})
                self.committed.append(line)
                self.committed_kind.append(source.kind)
                if source.timeline is not None and line.end > source.timeline.frontier:
                    self.pending.append(len(self.committed) - 1)
                self.emit(
                    {
                        "type": "live_segment",
                        "session_id": self.session_id,
                        "source": source.kind,
                        "segment": line.model_dump(),
                    }
                )
                self._check_mention(source, line)

        if commit:
            cut_seconds = max(s.end for s in commit)
            cut = min(int(cut_seconds * rate), source.buffer.size)
            source.buffer = source.buffer[cut:]
            source.buffer_start += cut / rate
        elif duration >= self.max_buffer and not segments:
            # Silence: drop it rather than growing forever.
            source.buffer = source.buffer[-int(self.commit_margin * rate) :]
            source.buffer_start += duration - source.buffer.size / rate

        if source.stream is not None and self.namer is not None:
            await self.namer.update(self, source)

        partial = " ".join(s.text for s in rest).strip()
        if partial != source.partial:
            source.partial = partial
            self.emit(
                {
                    "type": "live_partial",
                    "session_id": self.session_id,
                    "source": source.kind,
                    "speaker": source.last_speaker or source.speaker,
                    "text": partial,
                }
            )

    def _absolute(self, source: LiveSource, seg: TranscriptSegment) -> TranscriptSegment:
        """A buffer-relative segment (and its words) on the recording's timeline."""
        off = source.buffer_start
        words = None
        if seg.words:
            words = [
                w.model_copy(
                    update={"start": round(off + w.start, 3), "end": round(off + w.end, 3)}
                )
                for w in seg.words
            ]
        return seg.model_copy(
            update={
                "start": round(off + seg.start, 3),
                "end": round(off + seg.end, 3),
                "words": words,
            }
        )

    async def _push_stream(self, source: LiveSource, new: np.ndarray, rate: int, flush: bool):
        if source.stream is None:
            return
        try:
            frames = await source.stream.push(new, rate) if new.size else None
            if frames is not None:
                source.timeline.add(frames)
            if flush:
                frames = await source.stream.flush()
                if frames is not None:
                    source.timeline.add(frames)
        except Exception:
            logger.warning(
                "Nemotron streaming failed for %s; labelling speakers by voice instead",
                source.kind,
                exc_info=True,
            )
            source.stream = None
            self.pending = [i for i in self.pending if self.committed_kind[i] != source.kind]
            fallback = "voice clustering" if self.embedder and self.clusterer else "none"
            self._ready_status = f"Live · speaker detection: {fallback} (Nemotron failed)"
            self.emit(self._status(self._ready_status))
            return
        self._recheck_pending(source, final=flush)

    def _speaker_label(self, source: LiveSource, index: int | None) -> str:
        if index is None:  # nobody the stream knows: whoever spoke before
            return source.last_speaker or source.speaker
        taken = {source.speaker} | {s.speaker for s in self.sources}
        if self.namer is not None:  # earlier parts' speakers keep their labels for themselves
            taken |= set(self.namer.voices)
        return self.names.label(source.kind, index, taken)

    def _stream_lines(self, source: LiveSource, seg: TranscriptSegment) -> list[TranscriptSegment]:
        lines = []
        for line, index in split_line(self._absolute(source, seg), source.timeline):
            label = self._speaker_label(source, index)
            source.last_speaker = label
            lines.append(line.model_copy(update={"speaker": label}))
        return lines

    def _recheck_pending(self, source: LiveSource, final: bool) -> None:
        """Lines that ended past the stream's frontier when shown, now that it has passed
        them: give them the speaker the stream heard, and send the changes."""
        timeline = source.timeline
        changes, keep = [], []
        for i in self.pending:
            seg = self.committed[i]
            if self.committed_kind[i] != source.kind:
                keep.append(i)
                continue
            if seg.end > timeline.frontier and not final:
                keep.append(i)
                continue
            index = timeline.speaker_between(seg.start, seg.end)
            if index is None:
                continue
            label = self._speaker_label(source, index)
            if label != seg.speaker:
                changes.append({"start": seg.start, "old": seg.speaker, "new": label})
                seg.speaker = label
        self.pending = keep
        if changes:
            self.emit(
                {
                    "type": "live_labels",
                    "session_id": self.session_id,
                    "source": source.kind,
                    "labels": changes,
                }
            )

    def rename(self, old: str, new: str) -> None:
        """Show `new` for every line and speaker labelled `old` (a voice-profile match)."""
        if not self.names.rename(old, new):
            return
        for s in self.committed:
            if s.speaker == old:
                s.speaker = new
        for src in self.sources:
            if src.last_speaker == old:
                src.last_speaker = new
        self.emit({"type": "live_relabel", "session_id": self.session_id, "old": old, "new": new})

    def stream_stats(self) -> dict[str, dict]:
        return {s.kind: s.stream.stats() for s in self.sources if s.stream is not None}

    def _check_mention(self, source: LiveSource, seg: TranscriptSegment) -> None:
        # Your own mic, when it is recorded separately, is you saying your own name.
        if not self.mentions or (source.kind == "mic" and len(self.sources) > 1):
            return
        keyword = self.mentions.find(seg.text, seg.start)
        if keyword:
            self.emit(
                {
                    "type": "mention",
                    "session_id": self.session_id,
                    "keyword": keyword,
                    "speaker": seg.speaker,
                    "text": seg.text,
                    "start": seg.start,
                }
            )

    async def _label(self, source: LiveSource, seg: TranscriptSegment, rate: int) -> str:
        """Speaker label for a committed segment (segment times are buffer-relative)."""
        if not (source.diarize and self.embedder is not None and self.clusterer is not None):
            return source.speaker
        lo = max(int(seg.start * rate), 0)
        hi = min(int(seg.end * rate), source.buffer.size)
        vec = None
        try:
            vec = await self.embedder.embed(source.buffer[lo:hi], rate)
        except Exception:
            logger.debug("Live embedding failed", exc_info=True)
        if vec is None:  # too short or failed: same speaker as the previous line
            return source.last_speaker or source.speaker
        label, renames = self.clusterer.assign(vec)
        for old, new in renames:
            for s in self.committed:
                if s.speaker == old:
                    s.speaker = new
            for src in self.sources:
                if src.last_speaker == old:
                    src.last_speaker = new
            self.emit(
                {"type": "live_relabel", "session_id": self.session_id, "old": old, "new": new}
            )
        source.last_speaker = label
        return label

    async def _transcribe_buffer(self, source: LiveSource, rate: int) -> list[TranscriptSegment]:
        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tmp:
            path = tmp.name
        try:
            with wave.open(path, "wb") as w:
                w.setnchannels(1)
                w.setsampwidth(2)
                w.setframerate(rate)
                w.writeframes(source.buffer.tobytes())
            return await self.transcriber.transcribe(path, language=self.language)
        finally:
            Path(path).unlink(missing_ok=True)

    # CPU pressure (percent of time something waited for a CPU) above which we back off.
    PRESSURE_LIMIT = 40.0

    def _adapt(self, elapsed: float) -> None:
        """Stretch the tick interval (up to 4x) while transcription cannot keep up with the
        audio or the CPU is contended, and shrink it back after five easy ticks. Nothing is
        dropped: a longer interval just transcribes more audio per tick, and the final job
        covers everything anyway."""
        if not self.adaptive:
            return
        pressure = self.pressure()
        busy = pressure is not None and pressure > self.PRESSURE_LIMIT
        if elapsed > self.interval or busy:
            self._fast_ticks = 0
            reason = "CPU busy" if busy else "transcription is slow"
            wider = min(self.interval * 2, self.max_interval)
            if wider != self.interval:
                self.interval = wider
                logger.info("Live transcription every %.0f s (%s)", wider, reason)
            if reason != self.slowed_by:
                self.slowed_by = reason
                self.emit(self._status(f"{self._ready_status} · slowed down ({reason})"))
            return
        if self.interval == self.base_interval or elapsed >= self.interval / 2:
            self._fast_ticks = 0
            return
        self._fast_ticks += 1
        if self._fast_ticks < 5:
            return
        self._fast_ticks = 0
        self.interval = max(self.interval / 2, self.base_interval)
        if self.interval == self.base_interval:
            self.slowed_by = None
            self.emit(self._status(self._ready_status))

    def _status(self, message: str) -> dict:
        return {"type": "live_status", "session_id": self.session_id, "message": message}
