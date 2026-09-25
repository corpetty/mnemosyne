"""Quality benchmark: transcribe audio with the configured engine and score it.

    mnemosyne-bench --synthetic                   # two espeak-ng voices, known transcript
    mnemosyne-bench --session a1b2c3d4            # a meeting you corrected in the app
    mnemosyne-bench --audio x.wav --reference x.json
    mnemosyne-bench --synthetic --transcriber parakeet --diarizer pyannote --json

Scores: word error rate (substitutions + deletions + insertions over reference words) and
speaker accuracy (share of aligned words attributed to the right speaker, under the best
one-to-one mapping of the engine's labels to the reference's), plus runtime and real-time
factor. A session's transcript as edited in the app is its reference, so fixing a transcript
turns it into a test case.
"""

from __future__ import annotations

import argparse
import asyncio
import itertools
import json
import re
import shutil
import subprocess
import sys
import tempfile
import time
import wave
from dataclasses import asdict, dataclass
from pathlib import Path

# ---- scoring -----------------------------------------------------------------------


def normalize(text: str) -> list[str]:
    """Lowercase words without punctuation ("Don't!" -> "don't")."""
    return re.findall(r"[\w']+", text.lower().replace("’", "'"))


def words_with_speakers(segments: list[dict]) -> list[tuple[str, str]]:
    out = []
    for s in segments:
        out += [(w, s.get("speaker", "")) for w in normalize(s.get("text", ""))]
    return out


def align(ref: list[str], hyp: list[str]) -> list[tuple[int | None, int | None]]:
    """Levenshtein alignment: (ref index, hyp index) pairs; None marks a deletion or an
    insertion. Substitutions pair two different words."""
    n, m = len(ref), len(hyp)
    d = [[0] * (m + 1) for _ in range(n + 1)]
    for i in range(n + 1):
        d[i][0] = i
    for j in range(m + 1):
        d[0][j] = j
    for i in range(1, n + 1):
        for j in range(1, m + 1):
            cost = 0 if ref[i - 1] == hyp[j - 1] else 1
            d[i][j] = min(d[i - 1][j] + 1, d[i][j - 1] + 1, d[i - 1][j - 1] + cost)
    pairs: list[tuple[int | None, int | None]] = []
    i, j = n, m
    while i > 0 or j > 0:
        if i > 0 and j > 0 and d[i][j] == d[i - 1][j - 1] + (ref[i - 1] != hyp[j - 1]):
            pairs.append((i - 1, j - 1))
            i, j = i - 1, j - 1
        elif i > 0 and d[i][j] == d[i - 1][j] + 1:
            pairs.append((i - 1, None))
            i -= 1
        else:
            pairs.append((None, j - 1))
            j -= 1
    return pairs[::-1]


@dataclass
class WordErrors:
    reference_words: int
    substitutions: int
    deletions: int
    insertions: int

    @property
    def wer(self) -> float:
        errors = self.substitutions + self.deletions + self.insertions
        return errors / self.reference_words if self.reference_words else 0.0


def word_errors(ref: list[str], hyp: list[str]) -> WordErrors:
    pairs = align(ref, hyp)
    return WordErrors(
        reference_words=len(ref),
        substitutions=sum(
            1 for i, j in pairs if i is not None and j is not None and ref[i] != hyp[j]
        ),
        deletions=sum(1 for i, j in pairs if j is None),
        insertions=sum(1 for i, j in pairs if i is None),
    )


def speaker_accuracy(
    ref: list[tuple[str, str]], hyp: list[tuple[str, str]]
) -> tuple[float, dict[str, str]]:
    """Share of aligned words whose hypothesis speaker maps to the reference speaker, under
    the best one-to-one mapping of hypothesis labels to reference labels."""
    pairs = [
        (ref[i][1], hyp[j][1])
        for i, j in align([w for w, _ in ref], [w for w, _ in hyp])
        if i is not None and j is not None
    ]
    if not pairs:
        return 0.0, {}
    counts: dict[tuple[str, str], int] = {}
    for r, h in pairs:
        counts[(r, h)] = counts.get((r, h), 0) + 1
    ref_labels = sorted({r for r, _ in pairs})
    hyp_labels = sorted({h for _, h in pairs})
    best, best_map = -1, {}
    if len(hyp_labels) <= 7:
        pad = ref_labels + [None] * max(0, len(hyp_labels) - len(ref_labels))
        for perm in itertools.permutations(pad, len(hyp_labels)):
            score = sum(counts.get((r, h), 0) for h, r in zip(hyp_labels, perm, strict=True) if r)
            if score > best:
                best, best_map = score, {h: r for h, r in zip(hyp_labels, perm, strict=True) if r}
    else:  # greedy for many labels
        used: set[str] = set()
        for (r, h), _ in sorted(counts.items(), key=lambda kv: -kv[1]):
            if h not in best_map and r not in used:
                best_map[h] = r
                used.add(r)
        best = sum(counts.get((r, h), 0) for h, r in best_map.items())
    return best / len(pairs), best_map


# ---- references and audio ------------------------------------------------------------


def load_reference(path: Path) -> list[dict]:
    """JSON: a list of {speaker, text[, start, end]} (a session export's `transcript`
    works) or {"transcript": [...]}. Plain text: `Speaker: text` per line, or just text."""
    if path.suffix == ".json":
        data = json.loads(path.read_text())
        return data["transcript"] if isinstance(data, dict) else data
    segs = []
    for line in path.read_text().splitlines():
        if not line.strip():
            continue
        m = re.match(r"^\s*([^:]{1,40}):\s+(.*)$", line)
        segs.append(
            {"speaker": m.group(1), "text": m.group(2)} if m else {"speaker": "", "text": line}
        )
    return segs


SCRIPT = [
    ("Alice", "en-us+f3", "Good morning everyone, thanks for joining the release planning call."),
    (
        "Bob",
        "en-gb+m3",
        "Morning. The migration is code complete, but the documentation is behind.",
    ),
    ("Alice", "en-us+f3", "Then let's ship in October and give the docs two more weeks."),
    ("Bob", "en-gb+m3", "Agreed. I will update the release notes and tag the first candidate."),
    ("Alice", "en-us+f3", "Who is looking at the mobile regression reported on Tuesday?"),
    ("Bob", "en-gb+m3", "Nobody yet. We should decide that on Friday at the latest."),
]


def synthesize(out_dir: Path) -> tuple[Path, list[dict]]:
    """A two-voice conversation with espeak-ng, 16 kHz mono, with its exact transcript."""
    if not shutil.which("espeak-ng"):
        raise SystemExit("--synthetic needs espeak-ng (dnf install espeak-ng)")
    frames = b""
    rate = None
    reference, t = [], 0.0
    for n, (speaker, voice, text) in enumerate(SCRIPT):
        part = out_dir / f"line{n}.wav"
        subprocess.run(["espeak-ng", "-v", voice, "-s", "150", "-w", str(part), text], check=True)
        with wave.open(str(part)) as w:
            rate = rate or w.getframerate()
            data = w.readframes(w.getnframes())
            seconds = w.getnframes() / w.getframerate()
        gap = b"\x00\x00" * int(rate * 0.6)
        frames += data + gap
        reference.append({"speaker": speaker, "text": text, "start": t, "end": t + seconds})
        t += seconds + 0.6
    raw = out_dir / "raw.wav"
    with wave.open(str(raw), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(frames)
    audio = out_dir / "synthetic.wav"
    subprocess.run(
        ["ffmpeg", "-y", "-loglevel", "error", "-i", str(raw), "-ar", "16000", str(audio)],
        check=True,
    )
    return audio, reference


# ---- running ---------------------------------------------------------------------------


@dataclass
class BenchResult:
    engine: str
    audio_seconds: float
    runtime_seconds: float
    real_time_factor: float
    wer: float
    reference_words: int
    substitutions: int
    deletions: int
    insertions: int
    speaker_accuracy: float | None
    speaker_map: dict[str, str]


def score(reference: list[dict], hypothesis: list[dict]) -> tuple[WordErrors, float | None, dict]:
    ref_ws, hyp_ws = words_with_speakers(reference), words_with_speakers(hypothesis)
    errors = word_errors([w for w, _ in ref_ws], [w for w, _ in hyp_ws])
    labelled = any(s.get("speaker") for s in reference)
    acc, mapping = speaker_accuracy(ref_ws, hyp_ws) if labelled else (None, {})
    return errors, acc, mapping


async def transcribe(settings, audio: Path) -> tuple[list[dict], float]:
    from .transcription.engine import AudioSource
    from .transcription.registry import build_engine

    engine = build_engine(settings)
    await engine.load()
    start = time.perf_counter()
    segs = [s.model_dump() async for s in engine.transcribe_sources([AudioSource(path=str(audio))])]
    return segs, time.perf_counter() - start


def audio_seconds(path: Path) -> float:
    from .transcription.composed import audio_duration

    return audio_duration(str(path)) or 0.0


def run(settings, audio: Path, reference: list[dict]) -> BenchResult:
    from .transcription.registry import resolve_diarizer

    segs, runtime = asyncio.run(transcribe(settings, audio))
    errors, acc, mapping = score(reference, segs)
    seconds = audio_seconds(audio)
    return BenchResult(
        engine=f"{settings.transcriber}+{resolve_diarizer(settings)}",
        audio_seconds=round(seconds, 1),
        runtime_seconds=round(runtime, 2),
        real_time_factor=round(runtime / seconds, 3) if seconds else 0.0,
        wer=round(errors.wer, 4),
        reference_words=errors.reference_words,
        substitutions=errors.substitutions,
        deletions=errors.deletions,
        insertions=errors.insertions,
        speaker_accuracy=round(acc, 4) if acc is not None else None,
        speaker_map=mapping,
    )


def format_table(r: BenchResult) -> str:
    rows = [
        ("engine", r.engine),
        ("audio", f"{r.audio_seconds:.1f} s"),
        ("runtime", f"{r.runtime_seconds:.2f} s (real-time factor {r.real_time_factor})"),
        (
            "word error rate",
            f"{100 * r.wer:.1f}%  ({r.substitutions} sub, {r.deletions} del, "
            f"{r.insertions} ins over {r.reference_words} words)",
        ),
        (
            "speaker accuracy",
            f"{100 * r.speaker_accuracy:.1f}%" if r.speaker_accuracy is not None else "n/a",
        ),
        ("speaker map", ", ".join(f"{h} -> {s}" for h, s in r.speaker_map.items()) or "n/a"),
    ]
    width = max(len(k) for k, _ in rows)
    return "\n".join(f"{k:<{width}}  {v}" for k, v in rows)


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(prog="mnemosyne-bench", description=__doc__.split("\n\n")[0])
    src = p.add_mutually_exclusive_group(required=True)
    src.add_argument("--synthetic", action="store_true", help="two espeak-ng voices")
    src.add_argument("--session", help="a session id: its audio, and its edited transcript")
    src.add_argument("--audio", type=Path, help="an audio file (needs --reference)")
    p.add_argument("--reference", type=Path, help="JSON transcript or 'Speaker: text' lines")
    p.add_argument("--transcriber", help="override the configured transcriber")
    p.add_argument("--diarizer", help="override the configured diarizer")
    p.add_argument("--json", action="store_true", help="print JSON instead of a table")
    args = p.parse_args(argv)

    from .config import load_settings

    settings = load_settings()
    overrides = {
        k: v for k, v in (("transcriber", args.transcriber), ("diarizer", args.diarizer)) if v
    }
    if overrides:
        settings = settings.model_copy(update=overrides)

    with tempfile.TemporaryDirectory(dir=Path.home() / ".cache") as tmp:
        if args.synthetic:
            audio, reference = synthesize(Path(tmp))
        elif args.session:
            from .storage.sqlite import SessionRepository

            session = SessionRepository(settings.db_path).get(args.session)
            if session is None or not session.audio_file or not session.transcript:
                sys.exit(f"Session {args.session} not found, or it has no audio or transcript")
            audio = Path(session.audio_file)
            reference = [s.model_dump() for s in session.transcript]
        else:
            if not args.reference:
                p.error("--audio needs --reference")
            audio, reference = args.audio, load_reference(args.reference)
        result = run(settings, audio, reference)
    print(json.dumps(asdict(result), indent=2) if args.json else format_table(result))


if __name__ == "__main__":
    main()
