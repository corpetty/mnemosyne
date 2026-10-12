"""Drop mic lines that were never speech.

Phonon-2 has no VAD: keyboard and desk clicks, or near-silence, come out of it as short
phantom lines ("Yeah.", "Okay.") attributed to the person at the mic. Silero VAD tells them
apart (investigation on a real meeting, 2026-10-12: 34 phantom vs 40 real mic lines; not speaker
bleed, echo cancellation was on). The rule: a mic line is dropped when its word span, padded by
PAD on both sides, has fewer than MIN_FRAMES speech frames. Phantoms had 0-7 such frames (29 of
34 had none), real short lines had 9 or more, so no real line was lost, but the margin is thin.
Live, a whole buffer with fewer than LIVE_MIN_FRAMES speech frames is not given to the transcriber
at all: no-speech windows reaching it fell from 73 to 5 of 394, speech windows kept 25 of 26.

Silero is the model Parakeet's VAD uses (onnx-asr, `istupakov/silero-vad-onnx`), so it is in the
Hugging Face cache once Parakeet has run, and in the offline bundle (`mnemosyne-backend
prefetch`). It is run here directly with onnxruntime, one 32 ms frame at a time, to get the
probabilities (~400x real time on a CPU thread). When it cannot be had (the onnx extra is not
installed, no network and not cached) this logs once and keeps everything: a transcription never
fails because of it.
"""

from __future__ import annotations

import logging
import threading
import time
from pathlib import Path

import numpy as np

from ..models.transcript import TranscriptSegment

logger = logging.getLogger(__name__)

RATE = 16000
FRAME = 512  # samples per Silero frame at 16 kHz: 32 ms
CONTEXT = 64
THRESHOLD = 0.5  # a frame is speech above this probability
PAD = 0.3  # seconds around a line's first and last word
MIN_FRAMES = 8  # a line with fewer speech frames than this is noise
LIVE_MIN_FRAMES = 3  # a live buffer with fewer than this is not transcribed
RETRY_AFTER = 300.0  # seconds before trying to load an unavailable model again

REPO = "istupakov/silero-vad-onnx"
MODEL_FILE = "silero_vad.onnx"

_lock = threading.Lock()
_session = None
_failed_at: float | None = None
_logged = False


def _open_session():
    """The Silero ONNX session, from the Hugging Face cache or (once) the network."""
    import onnxruntime as ort
    from huggingface_hub import hf_hub_download

    try:
        path = hf_hub_download(REPO, MODEL_FILE, local_files_only=True)
    except Exception:
        path = hf_hub_download(REPO, MODEL_FILE)
    options = ort.SessionOptions()
    options.intra_op_num_threads = 1
    options.inter_op_num_threads = 1
    return ort.InferenceSession(path, options, providers=["CPUExecutionProvider"])


def _get_session():
    global _session, _failed_at, _logged
    with _lock:
        if _session is not None:
            return _session
        if _failed_at is not None and time.monotonic() - _failed_at < RETRY_AFTER:
            return None
        try:
            _session = _open_session()
            _failed_at, _logged = None, False
        except Exception as e:
            _failed_at = time.monotonic()
            if not _logged:
                _logged = True
                logger.warning("Speech detection unavailable, keeping every mic line: %s", e)
        return _session


def available() -> bool:
    """False while Silero could not be loaded (and until RETRY_AFTER has passed)."""
    return _get_session() is not None


def _to_16k(samples: np.ndarray, rate: int) -> np.ndarray:
    x = np.asarray(samples)
    if x.dtype.kind in "iu":
        x = x.astype(np.float32) / 32768.0
    else:
        x = x.astype(np.float32, copy=False)
    if rate == RATE or x.size == 0:
        return x
    if rate > RATE:  # low-pass below the new Nyquist, then pick samples
        taps = max(int(8 * rate / RATE) | 1, 9)
        n = np.arange(taps) - (taps - 1) / 2
        h = np.sinc(2 * 7500.0 / rate * n) * np.hamming(taps)
        x = np.convolve(x, h / h.sum(), mode="same").astype(np.float32)
    count = int(x.size * RATE / rate)
    at = np.arange(count) * (rate / RATE)
    return np.interp(at, np.arange(x.size), x).astype(np.float32)


def speech_frames(samples: np.ndarray, rate: int) -> np.ndarray | None:
    """Silero's speech probability for each 32 ms frame of mono `samples` (int16 or float32 in
    -1..1) at `rate`, or None when Silero is unavailable. Frame i covers seconds i*0.032 on."""
    session = _get_session()
    if session is None:
        return None
    x = _to_16k(samples, rate)
    count = x.size // FRAME
    out = np.empty(count, dtype=np.float32)
    state = np.zeros((2, 1, 128), np.float32)
    prev = np.zeros(CONTEXT, np.float32)
    sr = np.array(RATE, np.int64)
    for i in range(count):
        frame = x[i * FRAME : (i + 1) * FRAME]
        p, state = session.run(
            ["output", "stateN"],
            {"input": np.concatenate([prev, frame])[None, :], "state": state, "sr": sr},
        )
        out[i] = p[0, 0]
        prev = frame[-CONTEXT:]
    return out


def count_speech(
    seg: TranscriptSegment, probs: np.ndarray, pad: float = PAD, threshold: float = THRESHOLD
) -> int | None:
    """Speech frames around a line: from its first word's start to its last word's end (a
    single-word segment's own end is padded to a second), `pad` on both sides. None when the
    line lies beyond the audio the probabilities cover (cannot be judged)."""
    if seg.words:
        start = min(w.start for w in seg.words)
        end = max(w.end for w in seg.words)
    else:
        start, end = seg.start, seg.end
    lo = max(int((start - pad) * RATE / FRAME), 0)
    hi = int((end + pad) * RATE / FRAME) + 1
    if lo >= probs.size:
        return None
    return int((probs[lo:hi] > threshold).sum())


def split_noise(
    segments: list[TranscriptSegment], probs: np.ndarray | None, min_frames: int = MIN_FRAMES
) -> tuple[list[TranscriptSegment], list[TranscriptSegment]]:
    """(kept, dropped): lines on a timeline that `probs` covers, same objects."""
    if probs is None or probs.size == 0:
        return list(segments), []
    kept, dropped = [], []
    for seg in segments:
        n = count_speech(seg, probs)
        (dropped if n is not None and n < min_frames else kept).append(seg)
    return kept, dropped


def drop_noise(
    segments: list[TranscriptSegment], audio: str | Path | np.ndarray, rate: int = RATE
) -> tuple[list[TranscriptSegment], list[TranscriptSegment]]:
    """(kept, dropped) mic lines. `audio` is the mic recording (a path, decoded here) or its
    mono samples at `rate`. Keeps everything when speech detection or the audio is unavailable."""
    if not segments:
        return [], []
    try:
        if isinstance(audio, str | Path):
            if not available():  # do not decode a meeting for nothing
                return list(segments), []
            from ..audio.mixer import decode_audio

            samples, rate = decode_audio(Path(audio), sample_rate=RATE), RATE
        else:
            samples = audio
        probs = speech_frames(samples, rate)
    except Exception:
        logger.warning("Speech detection failed, keeping every mic line", exc_info=True)
        return list(segments), []
    return split_noise(segments, probs)
