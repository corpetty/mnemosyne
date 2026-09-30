"""Live speaker-label benchmark: replay a recording through the live transcript as if it were
being recorded, and score the speaker of every live line.

    mnemosyne-bench --live --audio ES2004a.wav --reference ES2004a.turns.json
    mnemosyne-bench --live --session a1b2c3d4 --modes clustering,rediarize

The audio is written into a growing WAV five seconds at a time and `LiveTranscriber.tick()` runs
after each step, with no waiting, so an hour replays in minutes. Modes:

- clustering: voice embeddings + online clustering (live_speakers.py);
- rediarize: clustering, corrected by re-diarizing with Nemotron every 30 s of audio
  (live_rediarize.py), today's default on NVIDIA;
- streaming: Nemotron streaming (diarizers/nemotron_stream.py).

Each line is scored against the reference speaker it overlaps most, weighted by its words, under
the best one-to-one mapping of labels to reference speakers. "shown" is the label a line had
when it appeared, followed through renames (`live_relabel`, which the app applies to lines on
screen) but not through corrections (`live_labels`); "final" is its label when the recording
ends. Voice profiles are not used. Transcriptions are cached per audio file, so every mode sees
the same lines and only the first run of a file pays for them.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import struct
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

import numpy as np

logger = logging.getLogger(__name__)

MODES = ("clustering", "rediarize", "streaming")
RATE = 16000
STEP = 5.0
REDIARIZE_EVERY = 30.0
WORK_DIR = Path.home() / ".cache/mnemosyne-trials/live-bench"


class CachingTranscriber:
    """Wraps the live transcriber: the same audio is transcribed once, across modes and runs."""

    def __init__(self, inner, cache_file: Path):
        self.inner = inner
        self.name = inner.name
        self.cache_file = cache_file
        self.cache: dict[str, list[dict]] = {}
        if cache_file.exists():
            self.cache = json.loads(cache_file.read_text())
        self.misses = 0

    def is_loaded(self) -> bool:
        return True  # the inner transcriber loads on the first cache miss

    async def load(self) -> None:
        pass

    async def transcribe(self, path: str, language: str | None = None):
        from .models.transcript import TranscriptSegment

        key = hashlib.sha1(Path(path).read_bytes() + (language or "").encode()).hexdigest()
        if key not in self.cache:
            if not self.inner.is_loaded():
                await self.inner.load()
            self.misses += 1
            segs = await self.inner.transcribe(path, language=language)
            self.cache[key] = [s.model_dump() for s in segs]
        return [TranscriptSegment(**s) for s in self.cache[key]]

    def save(self) -> None:
        if self.misses:
            self.cache_file.parent.mkdir(parents=True, exist_ok=True)
            self.cache_file.write_text(json.dumps(self.cache))


def wav_header(rate: int) -> bytes:
    """A 16-bit mono WAV header for a file still being written (sizes unknown)."""
    return (
        b"RIFF"
        + struct.pack("<I", 0xFFFFFFFF)
        + b"WAVEfmt "
        + struct.pack("<IHHIIHH", 16, 1, 1, rate, rate * 2, 2, 16)
        + b"data"
        + struct.pack("<I", 0xFFFFFFFF)
    )


# ---- scoring ---------------------------------------------------------------------------


def reference_speaker(start: float, end: float, turns: list[dict]) -> str | None:
    """The reference speaker overlapping [start, end] most, or None when nobody does."""
    best, best_overlap = None, 0.0
    for t in turns:
        o = min(end, t["end"]) - max(start, t["start"])
        if o > best_overlap:
            best, best_overlap = t["speaker"], o
    return best


def label_accuracy(pairs: list[tuple[str, str, float]]) -> tuple[float, dict[str, str]]:
    """Weighted share of (reference, hypothesis, weight) pairs that agree under the best
    one-to-one mapping of hypothesis labels to reference labels."""
    from .bench import best_mapping

    counts: dict[tuple[str, str], float] = {}
    for r, h, w in pairs:
        counts[(r, h)] = counts.get((r, h), 0.0) + w
    total = sum(counts.values())
    if not total:
        return 0.0, {}
    score, mapping = best_mapping(counts)
    return score / total, mapping


@dataclass
class Line:
    start: float
    end: float
    text: str
    shown: str
    final: str = ""


@dataclass
class ModeResult:
    mode: str
    lines: int
    scored_words: float
    shown_accuracy: float
    final_accuracy: float
    labels: int
    diarize_seconds: float
    replay_seconds: float
    notes: dict = field(default_factory=dict)


def score_lines(mode: str, lines: list[Line], turns: list[dict], **extra) -> ModeResult:
    from .bench import normalize

    shown, final = [], []
    for ln in lines:
        ref = reference_speaker(ln.start, ln.end, turns)
        if ref is None:
            continue
        w = float(len(normalize(ln.text)) or 1)
        shown.append((ref, ln.shown, w))
        final.append((ref, ln.final, w))
    s_acc, _ = label_accuracy(shown)
    f_acc, _ = label_accuracy(final)
    return ModeResult(
        mode=mode,
        lines=len(lines),
        scored_words=sum(w for _, _, w in shown),
        shown_accuracy=round(s_acc, 4),
        final_accuracy=round(f_acc, 4),
        labels=len({ln.final for ln in lines}),
        **extra,
    )


# ---- replay ----------------------------------------------------------------------------


class Recorder:
    """Collects live events the way the app shows them."""

    def __init__(self):
        self.lines: list[Line] = []

    def __call__(self, event: dict) -> None:
        kind = event.get("type")
        if kind == "live_segment":
            seg = event["segment"]
            self.lines.append(Line(seg["start"], seg["end"], seg["text"], seg["speaker"]))
        elif kind == "live_relabel":
            for ln in self.lines:
                if ln.shown == event["old"]:
                    ln.shown = event["new"]


async def replay(
    settings, pcm: np.ndarray, turns: list[dict], mode: str, transcriber, models: dict
) -> ModeResult:
    """Replay `pcm` (float32 mono at RATE) through a LiveTranscriber in `mode`."""
    from .transcription.live import LiveSource, LiveTranscriber
    from .transcription.live_speakers import OnlineClusterer

    WORK_DIR.mkdir(parents=True, exist_ok=True)
    path = WORK_DIR / f"replay-{mode}.wav"
    path.write_bytes(wav_header(RATE))
    stream = models["stream"].stream() if mode == "streaming" else None
    source = LiveSource(path=path, speaker="Speaker", kind="mixed", diarize=True, stream=stream)
    recorder = Recorder()
    embedder = models.get("embedder") if stream is None else None
    clusterer = (
        OnlineClusterer(
            threshold=settings.live_speaker_threshold,
            known_threshold=settings.speaker_match_threshold,
        )
        if embedder is not None
        else None
    )
    live = LiveTranscriber(
        transcriber=transcriber,
        sources=[source],
        emit=recorder,
        session_id="bench",
        interval=STEP,
        language=settings.language or None,
        embedder=embedder,
        clusterer=clusterer,
        silence_db=settings.live_silence_db,
        adaptive=False,
    )
    rediarizer = None
    if mode == "rediarize":
        from .transcription.live_rediarize import LiveRediarizer

        rediarizer = LiveRediarizer(models["rediarizer"], live, recorder, "bench")

    diarize_seconds = 0.0
    started = time.perf_counter()
    step = int(STEP * RATE)
    pcm16 = (np.clip(pcm, -1, 1) * 32767).astype("<i2")
    next_pass = REDIARIZE_EVERY
    with path.open("ab") as f:
        for i in range(0, pcm16.size, step):
            f.write(pcm16[i : i + step].tobytes())
            f.flush()
            await live.tick()
            if rediarizer is not None and (i + step) / RATE >= next_pass:
                t0 = time.perf_counter()
                await rediarizer.pass_once(source)
                diarize_seconds += time.perf_counter() - t0
                next_pass += REDIARIZE_EVERY
    await live.tick(flush=True)
    replay_seconds = time.perf_counter() - started
    for ln, seg in zip(recorder.lines, live.committed, strict=True):
        ln.final = seg.speaker
    path.unlink(missing_ok=True)
    notes = {}
    if rediarizer is not None:
        notes["passes"] = rediarizer.passes
    if stream is not None:
        notes.update(stream.stats())
        diarize_seconds = stream.stats().get("seconds", 0.0)
    return score_lines(
        mode,
        recorder.lines,
        turns,
        diarize_seconds=round(diarize_seconds, 2),
        replay_seconds=round(replay_seconds, 2),
        notes=notes,
    )


def turns_from_reference(reference: list[dict]) -> list[dict]:
    turns = [
        {"speaker": s["speaker"], "start": float(s["start"]), "end": float(s["end"])}
        for s in reference
        if s.get("speaker") and s.get("start") is not None and s.get("end") is not None
    ]
    if not turns:
        raise SystemExit("--live needs a reference with speakers and start/end times")
    return turns


async def run_live(settings, audio: Path, reference: list[dict], modes: list[str]) -> list[dict]:
    from .audio.mixer import decode_audio
    from .transcription.registry import build_live_embedder, build_live_transcriber

    turns = turns_from_reference(reference)
    pcm = await asyncio.to_thread(decode_audio, audio, RATE)
    try:  # NeMo logs model loading at INFO level to stdout, into --json output
        from nemo.utils import logging as nemo_logging

        nemo_logging.setLevel(logging.WARNING)
    except ImportError:
        pass
    transcriber = CachingTranscriber(
        build_live_transcriber(settings),
        WORK_DIR / f"{audio.stem}-{settings.live_transcriber}.json",
    )
    models: dict = {"embedder": build_live_embedder(settings)}
    if models["embedder"] is not None:
        await models["embedder"].load()
    if "rediarize" in modes:
        from .transcription.diarizers.nemotron import NemotronDiarizer

        models["rediarizer"] = NemotronDiarizer()
        await models["rediarizer"].load()
    if "streaming" in modes:
        from .transcription.diarizers.nemotron_stream import NemotronStreamModel

        models["stream"] = NemotronStreamModel()
        await models["stream"].load()
    results = []
    try:
        for mode in modes:
            try:
                r = await replay(settings, pcm, turns, mode, transcriber, models)
                results.append(asdict(r))
            except Exception as e:
                logger.warning("mode %s failed", mode, exc_info=True)
                results.append({"mode": mode, "error": str(e)})
            finally:
                transcriber.save()
    finally:
        for m in models.values():
            if m is not None:
                await m.unload()
    return [{"audio": audio.name, "seconds": round(pcm.size / RATE, 1), **r} for r in results]


def format_live(results: list[dict]) -> str:
    head = f"{'audio':<14} {'mode':<11} {'lines':>5} {'shown':>7} {'final':>7} {'labels':>6} "
    head += f"{'diarize s':>9} {'replay s':>8}"
    rows = [head]
    for r in results:
        if "error" in r:
            rows.append(f"{r['audio']:<14} {r['mode']:<11} error: {r['error']}")
            continue
        rows.append(
            f"{r['audio']:<14} {r['mode']:<11} {r['lines']:>5} "
            f"{100 * r['shown_accuracy']:>6.1f}% {100 * r['final_accuracy']:>6.1f}% "
            f"{r['labels']:>6} {r['diarize_seconds']:>9.1f} {r['replay_seconds']:>8.1f}"
        )
    return "\n".join(rows)
