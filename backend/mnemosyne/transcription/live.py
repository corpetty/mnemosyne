"""Live transcription of a recording in progress.

`WavTail` reads new PCM from a WAV file that pw-record is still writing.
`LiveTranscriber` keeps a per-source buffer of unprocessed audio, runs a
Transcriber over it every few seconds, and commits the segments that end
safely before the buffer edge (so a word cut by the chunk boundary is retried
on the next tick). Committed segments stream out as provisional events; the
uncommitted tail is sent as a replaceable partial. The full pipeline after
stop produces the authoritative transcript.
"""

from __future__ import annotations

import asyncio
import logging
import struct
import tempfile
import wave
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

from ..models.transcript import TranscriptSegment
from .engine import Transcriber
from .live_speakers import OnlineClusterer, SpeakerEmbedder
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
        if usable == 0:
            return np.zeros(0, dtype=np.int16)
        if self.sample_width != 2:
            raise ValueError(f"Unsupported sample width {self.sample_width}")
        pcm = np.frombuffer(raw[:usable], dtype=np.int16)
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

    def __post_init__(self):
        self.tail = WavTail(self.path)


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
        self.committed: list[TranscriptSegment] = []

    async def run(self) -> None:
        """Tick until cancelled. A final tick flushes what is left."""
        if not self.transcriber.is_loaded():
            self.emit(self._status("Loading live transcriber..."))
            await self.transcriber.load()
        status = "Live"
        if self.embedder is not None and any(src.diarize for src in self.sources):
            try:
                if not self.embedder.is_loaded():
                    self.emit(self._status("Loading speaker detection..."))
                    await self.embedder.load()
                status = "Live · speaker detection on"
            except Exception as e:
                logger.warning("Live speaker detection unavailable: %s", e)
                self.embedder = None
                status = "Live (speaker detection unavailable)"
        self.emit(self._status(status))
        try:
            while True:
                await asyncio.sleep(self.interval)
                await self.tick()
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
            except Exception:
                logger.exception("Live tick failed for %s", source.path)

    async def _tick_source(self, source: LiveSource, flush: bool) -> None:
        new = source.tail.read_new()
        if new.size:
            source.buffer = np.concatenate([source.buffer, new])
        rate = source.tail.sample_rate or 48000
        duration = source.buffer.size / rate
        if duration < self.min_buffer:
            return

        segments = await self._transcribe_buffer(source, rate)
        segments.sort(key=lambda s: s.start)

        cutoff = duration if flush else duration - self.commit_margin
        commit = [s for s in segments if s.end <= cutoff]
        if not commit and duration >= self.max_buffer:
            commit = segments
        rest = [s for s in segments if s not in commit]

        for seg in commit:
            speaker = await self._label(source, seg, rate)
            absolute = seg.model_copy(
                update={
                    "speaker": speaker,
                    "start": round(source.buffer_start + seg.start, 3),
                    "end": round(source.buffer_start + seg.end, 3),
                    "words": None,
                }
            )
            self.committed.append(absolute)
            self.emit(
                {
                    "type": "live_segment",
                    "session_id": self.session_id,
                    "source": source.kind,
                    "segment": absolute.model_dump(),
                }
            )
            self._check_mention(source, absolute)

        if commit:
            cut_seconds = max(s.end for s in commit)
            cut = min(int(cut_seconds * rate), source.buffer.size)
            source.buffer = source.buffer[cut:]
            source.buffer_start += cut / rate
        elif duration >= self.max_buffer and not segments:
            # Silence: drop it rather than growing forever.
            source.buffer = source.buffer[-int(self.commit_margin * rate) :]
            source.buffer_start += duration - source.buffer.size / rate

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

    def _status(self, message: str) -> dict:
        return {"type": "live_status", "session_id": self.session_id, "message": message}
