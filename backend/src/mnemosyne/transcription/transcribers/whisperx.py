"""WhisperX transcriber: faster-whisper + wav2vec2 word alignment. No diarization."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from ...models.transcript import TranscriptSegment, WordSegment

logger = logging.getLogger(__name__)


class WhisperXTranscriber:
    name = "whisperx"

    def __init__(
        self,
        model_size: str = "medium.en",
        device: str = "cuda",
        compute_type: str = "float16",
        batch_size: int = 8,
        vad_method: str = "silero",
    ):
        import torch  # heavy; only when this transcriber is chosen

        self.model_size = model_size
        self.device = device if torch.cuda.is_available() else "cpu"
        self.compute_type = compute_type if self.device == "cuda" else "int8"
        self.batch_size = batch_size
        self.vad_method = vad_method
        self._model: Any = None
        self._align: dict[str, tuple[Any, Any]] = {}

    def is_loaded(self) -> bool:
        return self._model is not None

    async def load(self) -> None:
        if self._model is not None:
            return
        logger.info(
            "Loading WhisperX model=%s device=%s compute=%s vad=%s",
            self.model_size,
            self.device,
            self.compute_type,
            self.vad_method,
        )

        def _load():
            import whisperx

            return whisperx.load_model(
                self.model_size,
                self.device,
                compute_type=self.compute_type,
                vad_method=self.vad_method,
            )

        self._model = await asyncio.to_thread(_load)

    async def unload(self) -> None:
        self._model = None
        self._align.clear()
        import torch

        if torch.cuda.is_available():
            torch.cuda.empty_cache()

    async def transcribe(
        self, audio_path: str, language: str | None = None
    ) -> list[TranscriptSegment]:
        if not self.is_loaded():
            await self.load()

        def _run():
            import whisperx

            audio = whisperx.load_audio(audio_path)
            result = self._model.transcribe(
                audio, batch_size=self.batch_size, language=language or None
            )
            lang = result.get("language", language or "en")
            if lang not in self._align:
                self._align[lang] = whisperx.load_align_model(
                    language_code=lang, device=self.device
                )
            align_model, metadata = self._align[lang]
            aligned = whisperx.align(
                result["segments"],
                align_model,
                metadata,
                audio,
                self.device,
                return_char_alignments=False,
            )
            return aligned["segments"]

        raw = await asyncio.to_thread(_run)
        return [_to_segment(s) for s in raw if s.get("text", "").strip()]


def _to_segment(seg: dict) -> TranscriptSegment:
    words = None
    if seg.get("words"):
        words = [
            WordSegment(
                word=w.get("word", ""),
                start=float(w.get("start", seg.get("start", 0.0))),
                end=float(w.get("end", seg.get("end", 0.0))),
                score=float(w.get("score", 0.0)),
            )
            for w in seg["words"]
            if "word" in w
        ]
    return TranscriptSegment(
        text=seg.get("text", "").strip(),
        speaker="UNKNOWN",
        start=float(seg.get("start", 0.0)),
        end=float(seg.get("end", 0.0)),
        words=words,
    )
