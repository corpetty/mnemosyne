"""Nemotron streaming driver: chunking, alignment and resampling with a fake model.

The equivalence check against NeMo's own chunked pass loads the real model on the GPU, so it
only runs when asked: MNEMOSYNE_GPU_TESTS=1 uv run pytest tests/test_nemotron_stream.py -s
(needs NeMo, CUDA and an AMI file from scripts/fetch-ami.py)."""

import asyncio
import os
import threading
from pathlib import Path

import numpy as np
import pytest

from mnemosyne.transcription.diarizers.nemotron_stream import (
    HOP,
    PAD,
    NemotronStream,
    Resampler,
)

RATE = 16000


class FakeBackend:
    """Checks each chunk's audio lines up with the stream, and returns probabilities that
    encode the frame index (column 0) so the tests can check what came back."""

    chunk_frames = 72
    right_frames = 32

    def __init__(self, signal: np.ndarray | None = None):
        self.lock = threading.Lock()
        self.signal = signal
        self.calls = []

    def init_state(self):
        return {"chunks": 0}

    def run_chunk(self, state, audio, valid, frames, right, start):
        self.calls.append((start, frames, right, valid, audio.size))
        assert audio.size == (frames + right) * HOP + 2 * PAD
        if self.signal is not None:
            # audio[PAD + k] is stream sample start * HOP + k; before the stream: silence.
            for k in (-PAD, -1, 0, 1, frames * HOP - 1, (frames + right) * HOP - 1):
                i = start * HOP + k
                want = self.signal[i] if 0 <= i < self.signal.size else 0.0
                assert audio[PAD + k] == pytest.approx(want), (start, k)
            assert valid == min(audio.size, self.signal.size - (start * HOP - PAD))
        probs = np.zeros((frames, 8), dtype=np.float32)
        probs[:, 0] = np.arange(start, start + frames)
        return {"chunks": state["chunks"] + 1}, probs


def run(coro):
    return asyncio.run(coro)


def push_in_pieces(stream, signal, rate, seed=0):
    rng = np.random.default_rng(seed)
    got, i = [], 0
    while i < signal.size:
        n = int(rng.integers(1, 4000))
        out = run(stream.push(signal[i : i + n], rate))
        if out is not None:
            got.append(out)
        i += n
    return got


def test_chunks_follow_the_audio_and_flush_scores_the_rest():
    signal = np.random.default_rng(1).uniform(-0.5, 0.5, 5 * RATE + 123).astype(np.float32)
    backend = FakeBackend(signal)
    stream = NemotronStream(backend)
    got = push_in_pieces(stream, signal, RATE)
    # Only whole chunks with their look-ahead and the audio under their windows.
    assert all(frames == 72 and right == 32 for _, frames, right, _, _ in backend.calls)
    assert stream.frames_done % 72 == 0
    assert (stream.frames_done + 32) * HOP + PAD <= signal.size
    assert stream.frontier == pytest.approx(stream.frames_done / 100)
    last = run(stream.flush())
    got.append(last)
    total = signal.size // HOP
    assert stream.frames_done == total
    frames = np.concatenate([g.probs[:, 0] for g in got])
    assert frames.tolist() == list(range(total))
    assert [g.start for g in got] == [0] + [g.end for g in got[:-1]]
    # The last chunks shrink their look-ahead to what the recording has.
    start, frames_, right, _, _ = backend.calls[-1]
    assert start + frames_ == total and right == 0
    assert run(stream.push(signal[:100], RATE)) is None  # nothing after the end
    assert stream.stats()["steps"] == len(backend.calls)


def test_int16_audio_is_scaled():
    signal = (np.random.default_rng(2).uniform(-0.5, 0.5, 2 * RATE) * 32767).astype(np.int16)
    backend = FakeBackend(signal.astype(np.float32) / 32768.0)
    stream = NemotronStream(backend)
    push_in_pieces(stream, signal, RATE)
    run(stream.flush())
    assert backend.calls


def test_short_stream_only_scores_at_flush():
    backend = FakeBackend()
    stream = NemotronStream(backend)
    assert run(stream.push(np.zeros(RATE // 2, dtype=np.float32), RATE)) is None
    out = run(stream.flush())
    assert out.start == 0 and len(out.probs) == RATE // 2 // HOP


def test_rate_change_is_refused():
    stream = NemotronStream(FakeBackend())
    run(stream.push(np.zeros(100, dtype=np.float32), 48000))
    with pytest.raises(ValueError):
        run(stream.push(np.zeros(100, dtype=np.float32), 44100))


@pytest.mark.parametrize("rate", [48000, 44100, 22050])
def test_streaming_resampler_matches_whole_file(rate):
    from scipy.signal import resample_poly

    t = np.arange(3 * rate) / rate
    x = (0.3 * np.sin(2 * np.pi * 440 * t) + 0.1 * np.sin(2 * np.pi * 3100 * t)).astype(np.float32)
    r = Resampler(rate)
    rng = np.random.default_rng(3)
    parts, i = [], 0
    while i < x.size:
        n = int(rng.integers(1, 5000))
        parts.append(r.push(x[i : i + n]))
        i += n
    parts.append(r.flush())
    y = np.concatenate(parts)
    whole = resample_poly(x, r.up, r.down)
    assert y.size == whole.size
    np.testing.assert_allclose(y, whole, atol=1e-5)
    assert r.buf.size < 2 * r.reach + r.down + 5000  # the buffer stays small


def test_resampler_passes_16k_through():
    r = Resampler(16000)
    x = np.arange(10, dtype=np.float32)
    assert r.push(x).tolist() == x.tolist()
    assert r.flush().size == 0


def test_model_knows_when_a_recording_still_streams():
    from mnemosyne.transcription.diarizers.nemotron_stream import NemotronStreamModel

    model = NemotronStreamModel()
    model._model = object()  # "loaded"
    assert not model.in_use()
    stream = model.stream()
    assert model.in_use()
    stream.flushed = True
    assert not model.in_use()
    other = model.stream()
    del other  # a live job that ended without flushing (its stream failed)
    assert not model.in_use()


# ---- the real model -------------------------------------------------------------------

AMI = Path(
    os.environ.get(
        "MNEMOSYNE_STREAM_AUDIO", Path.home() / ".cache/mnemosyne-trials/ami/ES2004a.wav"
    )
)
SECONDS = float(os.environ.get("MNEMOSYNE_STREAM_SECONDS", "300"))


def _cuda_nemo() -> bool:
    try:
        import importlib.util

        import torch

        return importlib.util.find_spec("nemo") is not None and torch.cuda.is_available()
    except ImportError:
        return False


@pytest.mark.skipif(
    not (os.environ.get("MNEMOSYNE_GPU_TESTS") and _cuda_nemo() and AMI.exists()),
    reason="set MNEMOSYNE_GPU_TESTS=1; needs NeMo, CUDA and AMI audio",
)
def test_streaming_matches_nemo_chunked_pass():
    """Streamed in random pieces, the speakers per 10 ms frame agree with NeMo's own
    streaming loop over the whole file's features (same model, same settings)."""
    import time
    import wave

    import torch

    from mnemosyne.transcription.diarizers.nemotron_stream import (
        NemotronStreamModel,
        sdpa_attention,
    )

    with wave.open(str(AMI)) as w:
        assert w.getframerate() == RATE and w.getnchannels() == 1
        pcm = np.frombuffer(w.readframes(int(SECONDS * RATE)), dtype=np.int16)
    audio = pcm.astype(np.float32) / 32768.0

    model = NemotronStreamModel()
    run(model.load())
    torch.cuda.reset_peak_memory_stats()
    stream = model.stream()
    started = time.perf_counter()
    got = push_in_pieces(stream, pcm, RATE, seed=4)
    got.append(run(stream.flush()))
    elapsed = time.perf_counter() - started
    stream_peak = torch.cuda.max_memory_allocated() / 2**20
    streamed = np.concatenate([g.probs for g in got if g is not None])

    m = model._model
    with torch.inference_mode(), sdpa_attention():
        signal = torch.from_numpy(audio).to(m.device).unsqueeze(0)
        reference = m.forward(signal, torch.tensor([signal.shape[1]], device=m.device))
    reference = reference[0].float().cpu().numpy()

    assert streamed.shape == reference.shape
    active = (reference.max(axis=1) > 0.5) | (streamed.max(axis=1) > 0.5)
    agree = (reference.argmax(axis=1) == streamed.argmax(axis=1))[active].mean()
    stats = stream.stats()
    print(
        f"\n{SECONDS:.0f} s: argmax agreement {100 * agree:.2f}% of {active.sum()} active frames, "
        f"max |diff| {np.abs(reference - streamed).max():.4f}, {stats['steps']} steps, "
        f"mean {stats['mean_ms']} ms, p95 {stats['p95_ms']} ms, total {elapsed:.1f} s, "
        f"peak VRAM while streaming {stream_peak:.0f} MiB"
    )
    assert agree >= 0.99
    run(model.unload())
