"""NVIDIA Parakeet TDT via onnx-asr (ONNX Runtime). No torch; fast on CPU.

Long audio is split with Silero VAD into utterances, each recognized with
token timestamps, which we merge into word timings for speaker assignment.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from ...audio.mixer import decode_audio
from ...models.transcript import TranscriptSegment, WordSegment

logger = logging.getLogger(__name__)

SAMPLE_RATE = 16000


class ParakeetTranscriber:
    name = "parakeet"

    def __init__(
        self,
        model: str = "nemo-parakeet-tdt-0.6b-v3",
        provider: str = "cpu",
        quantization: str | None = None,
    ):
        self.model = model
        self.provider = provider
        self.quantization = quantization
        self._asr: Any = None

    def is_loaded(self) -> bool:
        return self._asr is not None

    async def load(self) -> None:
        if self._asr is not None:
            return
        logger.info(
            "Loading %s (onnx, provider=%s, quant=%s)", self.model, self.provider, self.quantization
        )

        def _load():
            import onnx_asr

            providers = None
            if self.provider == "cuda":
                providers = ["CUDAExecutionProvider", "CPUExecutionProvider"]
            model = onnx_asr.load_model(
                self.model, quantization=self.quantization, providers=providers
            )
            vad = onnx_asr.load_vad("silero", providers=providers)
            return model.with_vad(vad).with_timestamps()

        self._asr = await asyncio.to_thread(_load)

    async def unload(self) -> None:
        self._asr = None

    async def transcribe(
        self, audio_path: str, language: str | None = None, progress=None
    ) -> list[TranscriptSegment]:
        if not self.is_loaded():
            await self.load()

        def _run():
            pcm = decode_audio(audio_path, sample_rate=SAMPLE_RATE)
            duration = max(pcm.size / SAMPLE_RATE, 1e-6)
            out = []
            # The VAD adapter yields utterances in order: report how far through the audio we are.
            for r in self._asr.recognize(pcm, sample_rate=SAMPLE_RATE):
                out.append(_to_segment(r))
                if progress is not None:
                    progress(min(float(r.end) / duration, 1.0))
            if progress is not None:
                progress(1.0)
            return out

        segments = await asyncio.to_thread(_run)
        return [s for s in segments if s.text]


def _to_segment(r: Any) -> TranscriptSegment:
    start, end = float(r.start), float(r.end)
    text = (r.text or "").strip()
    words = _words_from_tokens(r.tokens, r.timestamps, start, end)
    return TranscriptSegment(text=text, speaker="UNKNOWN", start=start, end=end, words=words)


def _words_from_tokens(
    tokens: list[str] | None, timestamps: list[float] | None, seg_start: float, seg_end: float
) -> list[WordSegment] | None:
    """Merge sentencepiece tokens ("▁hello", "wor", "ld") into words with timings.

    onnx-asr token timestamps are relative to the start of the VAD segment they
    came from. A word starts at its first token and ends where the next word
    starts (or at the segment end).
    """
    if not tokens or not timestamps or len(tokens) != len(timestamps):
        return None
    words: list[tuple[str, float]] = []
    for tok, ts in zip(tokens, timestamps, strict=True):
        piece = tok.replace("▁", " ")
        if piece.startswith(" ") or not words:
            words.append((piece.strip(), seg_start + float(ts)))
        else:
            text, t0 = words[-1]
            words[-1] = (text + piece, t0)
    out = []
    for i, (text, t0) in enumerate(words):
        if not text:
            continue
        t1 = words[i + 1][1] if i + 1 < len(words) else seg_end
        t0 = min(max(t0, seg_start), seg_end)
        out.append(WordSegment(word=text, start=t0, end=max(min(t1, seg_end), t0), score=0.0))
    return out or None
