"""Nemotron 3 Diarization in streaming mode: who is speaking, chunk by chunk, while recording.

Streaming Sortformer keeps an arrival-order speaker cache and a FIFO of recent frames between
chunks, so speaker index k is the same person for the whole stream (up to 8 speakers) with no
clustering. `NemotronStreamModel` holds the model with the model card's low-latency settings (a
0.72 s chunk plus 0.32 s of look-ahead); each recording source gets its own `NemotronStream`.

The model is a separate instance from the offline diarizer's (diarizers/nemotron.py): the
streaming settings live on the model, and a final transcription during a recording would
otherwise change them under the live stream. The checkpoint is 200 MB.

Features are computed per chunk from the audio around it, so that they are the same as the mel
frames of the whole recording: frame i is centred on sample 160 i, and a chunk's audio starts
PAD samples before its first frame (the stream starts with PAD samples of silence, as the whole-
file STFT pads with zeros). tests/test_nemotron_stream.py checks the result against NeMo's own
chunked pass over the whole file.

Attention runs through PyTorch's SDPA instead of NeMo's compiled FlexAttention in the streaming
threads (`sdpa_attention`): the speaker cache and FIFO grow chunk by chunk, so every early step
has a new sequence length to compile for, and on Turing (RTX 20xx) Triton picks a kernel that
needs more shared memory than the GPU has. Nemotron uses RoPE (no score_mod), so SDPA computes
the same thing: 22 ms per 0.72 s chunk on an RTX 2080 Ti, against 73 ms for eager FlexAttention.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import math
import threading
import time
import weakref
from dataclasses import dataclass
from typing import Any, Protocol

import numpy as np

from .nemotron import DEFAULT_MODEL, MAX_SPEAKERS, RATE

logger = logging.getLogger(__name__)

HOP = 160  # samples per 10 ms feature frame at 16 kHz
PAD = 3 * HOP  # audio before/after a chunk's frames: covers the 25 ms window (n_fft 512)
FRAME_SECONDS = HOP / RATE

# The model card's low-latency configuration, in 80 ms frames: input latency 1.04 s.
LOW_LATENCY = {
    "spkcache_len": 264,
    "fifo_len": 264,
    "chunk_len": 9,
    "chunk_right_context": 4,
    "spkcache_update_period": 222,
}


@dataclass
class SpeakerFrames:
    """Speaker activity for 10 ms frames `start` .. `start + len(probs)` of a stream."""

    start: int
    probs: np.ndarray  # (frames, MAX_SPEAKERS), float32 in [0, 1]

    @property
    def end(self) -> int:
        return self.start + len(self.probs)


class StreamBackend(Protocol):
    """What a stream needs from the model (a fake in tests)."""

    chunk_frames: int  # 10 ms frames scored per step
    right_frames: int  # look-ahead frames per step
    lock: threading.Lock

    def init_state(self) -> Any: ...

    def run_chunk(
        self, state: Any, audio: np.ndarray, valid: int, frames: int, right: int, start: int
    ) -> tuple[Any, np.ndarray]:
        """Score frames `start .. start + frames` given `audio` (16 kHz float32) that begins
        PAD samples before frame `start` and covers `frames + right` frames plus PAD after; only
        its first `valid` samples are real (the rest is the zeros after the end of a stream).
        Returns the new state and (frames, MAX_SPEAKERS) probabilities."""
        ...


class Resampler:
    """Streaming polyphase resampling to 16 kHz with no seams: each output sample is computed
    only once enough input around it has arrived (about 1 ms)."""

    def __init__(self, rate_in: int, rate_out: int = RATE):
        g = math.gcd(rate_in, rate_out)
        self.up, self.down = rate_out // g, rate_in // g
        # scipy's default filter reaches 10 * max(up, down) upsampled samples each way.
        self.reach = math.ceil(10 * max(self.up, self.down) / self.up) + 1
        self.buf = np.zeros(0, dtype=np.float32)
        self.buf_start = 0  # input index of buf[0], a multiple of `down`
        self.next_out = 0  # next output index to emit

    def _run(self, final: bool) -> np.ndarray:
        from scipy.signal import resample_poly

        if self.up == self.down:
            out, self.buf = self.buf, self.buf[:0]
            self.buf_start += len(out)
            self.next_out += len(out)
            return out
        buf_end = self.buf_start + len(self.buf)
        if final:
            last = math.ceil(buf_end * self.up / self.down)
        else:
            last = max(0, math.floor((buf_end - self.reach) * self.up / self.down) + 1)
        if last <= self.next_out:
            return np.zeros(0, dtype=np.float32)
        y = resample_poly(self.buf, self.up, self.down).astype(np.float32)
        first_local = self.buf_start * self.up // self.down
        out = y[self.next_out - first_local : last - first_local]
        self.next_out = last
        keep_from = max(self.buf_start, self.next_out * self.down // self.up - self.reach)
        keep_from -= keep_from % self.down
        self.buf = self.buf[keep_from - self.buf_start :]
        self.buf_start = keep_from
        return out

    def push(self, pcm: np.ndarray) -> np.ndarray:
        self.buf = np.concatenate([self.buf, pcm.astype(np.float32, copy=False)])
        return self._run(final=False)

    def flush(self) -> np.ndarray:
        return self._run(final=True)


def _as_float(pcm: np.ndarray) -> np.ndarray:
    if pcm.dtype == np.int16:
        return pcm.astype(np.float32) / 32768.0
    return pcm.astype(np.float32, copy=False)


class NemotronStream:
    """One source's stream: push audio as it is recorded, get speaker activity back."""

    def __init__(self, backend: StreamBackend):
        self.backend = backend
        self.state: Any = None
        self._resampler: Resampler | None = None
        self._rate: int | None = None
        # 16 kHz audio from sample `_audio_start - PAD` of the stream (silence before 0).
        self._audio = np.zeros(PAD, dtype=np.float32)
        self._audio_start = 0
        self._samples = 0  # 16 kHz samples received
        self.frames_done = 0  # 10 ms frames scored so far
        self.steps = 0
        self.step_seconds: list[float] = []
        self.flushed = False

    @property
    def frontier(self) -> float:
        """Seconds from the start of the stream up to which speaker activity is known."""
        return self.frames_done * FRAME_SECONDS

    async def push(self, pcm: np.ndarray, rate: int) -> SpeakerFrames | None:
        """Add audio (int16 or float32 mono) and score every chunk that is complete."""
        if pcm.size == 0 or self.flushed:
            return None
        return await asyncio.to_thread(self._push, pcm, rate)

    async def flush(self) -> SpeakerFrames | None:
        """Score what is left at the end of the recording (the last chunk has no look-ahead)."""
        if self.flushed:
            return None
        return await asyncio.to_thread(self._flush)

    def _push(self, pcm: np.ndarray, rate: int) -> SpeakerFrames | None:
        if self._rate is None:
            self._rate = rate
            self._resampler = Resampler(rate) if rate != RATE else None
        elif rate != self._rate:
            raise ValueError(f"sample rate changed from {self._rate} to {rate}")
        audio = _as_float(pcm)
        if self._resampler is not None:
            audio = self._resampler.push(audio)
        self._append(audio)
        return self._run(final=False)

    def _flush(self) -> SpeakerFrames | None:
        if self._resampler is not None:
            self._append(self._resampler.flush())
        self.flushed = True
        return self._run(final=True)

    def _append(self, audio: np.ndarray) -> None:
        if audio.size:
            self._audio = np.concatenate([self._audio, audio])
            self._samples += audio.size

    def _run(self, final: bool) -> SpeakerFrames | None:
        b = self.backend
        total = self._samples // HOP  # feature frames the whole recording so far would have
        out = []
        start = self.frames_done
        while True:
            first = self.frames_done
            if final:
                if first >= total:
                    break
                frames = min(b.chunk_frames, total - first)
                right = min(b.right_frames, total - first - frames)
            else:
                frames, right = b.chunk_frames, b.right_frames
                # This chunk's frames, its look-ahead, and the audio under their windows.
                if self._samples < (first + frames + right) * HOP + PAD:
                    break
            lo = first * HOP - self._audio_start  # index in _audio of (frame `first` - PAD)
            hi = lo + (frames + right) * HOP + 2 * PAD
            audio = self._audio[lo:hi]
            valid = audio.size
            if valid < hi - lo:  # only at the very end: the zeros a whole-file STFT pads with
                audio = np.concatenate([audio, np.zeros(hi - lo - valid, dtype=np.float32)])
            if self.state is None:
                self.state = b.init_state()
            t0 = time.perf_counter()
            with b.lock:
                self.state, probs = b.run_chunk(self.state, audio, valid, frames, right, first)
            self.step_seconds.append(time.perf_counter() - t0)
            self.steps += 1
            out.append(np.asarray(probs, dtype=np.float32)[:frames])
            self.frames_done += frames
        # Keep only the audio the next chunk needs.
        drop = self.frames_done * HOP - self._audio_start
        if drop > 0:
            self._audio = self._audio[drop:]
            self._audio_start += drop
        if not out:
            return None
        return SpeakerFrames(start=start, probs=np.concatenate(out))

    def stats(self) -> dict:
        s = sorted(self.step_seconds)
        if not s:
            return {"steps": 0}
        return {
            "steps": len(s),
            "mean_ms": round(1000 * sum(s) / len(s), 2),
            "p95_ms": round(1000 * s[min(len(s) - 1, int(0.95 * len(s)))], 2),
            "seconds": round(sum(s), 2),
            "audio_seconds": round(self.frontier, 2),
        }


_local = threading.local()
_sdpa_installed = False


def _sdpa(q, k, v, block_mask=None, score_mod=None):
    """FlexAttention's signature on scaled_dot_product_attention (no score_mod), with the
    block mask made dense once per forward pass (all layers share it) and dropped when it
    masks nothing."""
    import torch.nn.functional as F
    from torch.nn.attention.flex_attention import create_mask, flex_attention

    if score_mod is not None:
        return flex_attention(q, k, v, block_mask=block_mask, score_mod=score_mod)
    mask = None
    if block_mask is not None:
        cached = getattr(_local, "mask", None)
        if cached is None or cached[0] is not block_mask:
            dense = create_mask(
                block_mask.mask_mod,
                block_mask.kv_num_blocks.shape[0],
                1,
                q.shape[-2],
                k.shape[-2],
                device=q.device,
            )
            cached = (block_mask, None if bool(dense.all()) else dense)
            _local.mask = cached
        mask = cached[1]
    return F.scaled_dot_product_attention(q, k, v, attn_mask=mask)


def _install_sdpa() -> None:
    """Route NeMo's attention to `_sdpa` in threads inside `sdpa_attention()` only."""
    global _sdpa_installed
    if _sdpa_installed:
        return
    from nemo.collections.asr.modules import transformer_encoder_utils as tu

    original = tu._get_flex_attention

    def pick(x):
        return _sdpa if getattr(_local, "sdpa", False) else original(x)

    tu._get_flex_attention = pick
    _sdpa_installed = True


@contextlib.contextmanager
def sdpa_attention():
    _install_sdpa()
    _local.sdpa = True
    try:
        yield
    finally:
        _local.sdpa = False
        _local.mask = None


class NemotronStreamModel:
    """The streaming model, shared by every source's stream (one GPU queue: `lock`)."""

    name = "nemotron-stream"

    def __init__(self, model: str = DEFAULT_MODEL, device: str = "cuda", config=None):
        self.model = model
        self.device = device
        self.config = dict(config or LOW_LATENCY)
        self.lock = threading.Lock()
        self._model: Any = None
        self._streams: weakref.WeakSet[NemotronStream] = weakref.WeakSet()
        self.chunk_frames = 0
        self.right_frames = 0

    def is_loaded(self) -> bool:
        return self._model is not None

    async def load(self) -> None:
        if self._model is not None:
            return
        logger.info("Loading streaming diarization model %s", self.model)

        def _load():
            import torch
            from nemo.collections.asr.models import SortformerEncLabelModel

            if self.device == "cuda" and not torch.cuda.is_available():
                raise RuntimeError("Nemotron streaming needs a CUDA GPU")
            model = SortformerEncLabelModel.from_pretrained(self.model, map_location=self.device)
            model.eval()
            for key, value in self.config.items():
                setattr(model.sortformer_modules, key, value)
            model.sortformer_modules.chunk_left_context = 0
            model._check_streaming_parameters()
            if not model.high_resolution or model.output_subsampling_factor != 1:
                raise RuntimeError("expected 10 ms speaker predictions from Nemotron")
            if model.async_streaming:
                raise RuntimeError("asynchronous Sortformer streaming is not supported")
            return model

        model = await asyncio.to_thread(_load)
        sub = model.sortformer_modules.subsampling_factor
        self.chunk_frames = model.sortformer_modules.chunk_len * sub
        self.right_frames = model.sortformer_modules.chunk_right_context * sub
        self._model = model

    async def unload(self) -> None:
        self._model = None
        import torch

        if torch.cuda.is_available():
            torch.cuda.empty_cache()

    def stream(self) -> NemotronStream:
        if self._model is None:
            raise RuntimeError("load() the streaming model first")
        stream = NemotronStream(self)
        self._streams.add(stream)
        return stream

    def in_use(self) -> bool:
        """A recording still streams through this model (not flushed, not garbage)."""
        return any(not s.flushed for s in self._streams)

    def init_state(self):
        m = self._model
        return m.sortformer_modules.init_streaming_state(
            batch_size=1, async_streaming=False, device=m.device
        )

    def run_chunk(self, state, audio: np.ndarray, valid: int, frames: int, right: int, start: int):
        import torch

        m = self._model
        with torch.inference_mode(), sdpa_attention():
            signal = torch.from_numpy(audio).to(m.device).unsqueeze(0)
            # The length zeroes the pre-emphasised samples past the end, as for a whole file.
            length = torch.tensor([valid], device=m.device)
            feats, _ = m.preprocessor(input_signal=signal, length=length)
            # Segment frame j is centred on chunk frame j - PAD / HOP.
            skip = PAD // HOP
            feats = feats[:, :, skip : skip + frames + right].transpose(1, 2)
            feat_len = torch.tensor([frames + right], device=m.device)
            empty = torch.zeros((1, 0, MAX_SPEAKERS), device=m.device)
            state, preds = m.forward_streaming_step(
                processed_signal=feats,
                processed_signal_length=feat_len,
                streaming_state=state,
                total_preds=empty,
                left_offset=0,
                right_offset=right,
            )
        return state, preds[0, :frames].float().cpu().numpy()
