"""WhisperX transcription + diarization engine."""

import asyncio
import logging
import os
from collections.abc import AsyncIterator

import torch

from ..models.transcript import TranscriptSegment, WordSegment

logger = logging.getLogger(__name__)


class WhisperXEngine:
    """Transcription engine using WhisperX (faster-whisper + pyannote 3.1)."""

    def __init__(
        self,
        model_size: str = "large-v2",
        device: str = "cuda",
        compute_type: str = "float16",
        batch_size: int = 16,
        hf_token: str | None = None,
    ):
        self.model_size = model_size
        self.device = device if torch.cuda.is_available() else "cpu"
        self.compute_type = compute_type if self.device == "cuda" else "int8"
        self.batch_size = batch_size
        self.hf_token = hf_token or os.environ.get("HF_TOKEN", "")

        self._whisper_model = None
        self._diarize_model = None

    def is_loaded(self) -> bool:
        return self._whisper_model is not None

    async def load(self) -> None:
        """Load WhisperX and diarization models into GPU memory."""
        if self._whisper_model is not None:
            return

        logger.info(
            "Loading WhisperX model=%s device=%s compute=%s",
            self.model_size, self.device, self.compute_type,
        )

        def _load():
            import whisperx

            model = whisperx.load_model(
                self.model_size,
                self.device,
                compute_type=self.compute_type,
            )
            diarize = whisperx.diarize.DiarizationPipeline(
                model_name="pyannote/speaker-diarization-3.1",
                token=self.hf_token,
                device=self.device,
            )
            return model, diarize

        self._whisper_model, self._diarize_model = await asyncio.to_thread(_load)
        logger.info("WhisperX models loaded successfully")

    async def unload(self) -> None:
        """Free GPU memory."""
        self._whisper_model = None
        self._diarize_model = None
        if torch.cuda.is_available():
            torch.cuda.empty_cache()
        logger.info("WhisperX models unloaded")

    async def transcribe(self, audio_path: str) -> AsyncIterator[TranscriptSegment]:
        """Transcribe and diarize an audio file.

        Runs the full WhisperX pipeline:
        1. Transcribe with faster-whisper
        2. Align timestamps with wav2vec2
        3. Diarize with pyannote 3.1
        4. Assign speakers to segments
        """
        if not self.is_loaded():
            await self.load()

        def _process():
            import whisperx

            logger.info("Loading audio: %s", audio_path)
            audio = whisperx.load_audio(audio_path)

            # Step 1: Transcribe
            logger.info("Transcribing...")
            result = self._whisper_model.transcribe(
                audio, batch_size=self.batch_size
            )
            language = result.get("language", "en")
            logger.info("Detected language: %s", language)

            # Step 2: Align timestamps
            logger.info("Aligning timestamps...")
            align_model, metadata = whisperx.load_align_model(
                language_code=language, device=self.device
            )
            result = whisperx.align(
                result["segments"],
                align_model,
                metadata,
                audio,
                self.device,
                return_char_alignments=False,
            )
            # Free alignment model
            del align_model

            # Step 3: Diarize
            logger.info("Diarizing...")
            diarize_segments = self._diarize_model(audio, min_speakers=1, max_speakers=10)
            logger.info(
                "Diarization found %d segments, speakers: %s",
                len(diarize_segments),
                diarize_segments['speaker'].unique().tolist() if len(diarize_segments) > 0 else [],
            )

            # Step 4: Assign speakers
            result = whisperx.assign_word_speakers(diarize_segments, result, fill_nearest=True)

            return result["segments"]

        segments = await asyncio.to_thread(_process)

        for seg in segments:
            words = None
            if "words" in seg:
                words = [
                    WordSegment(
                        word=w.get("word", ""),
                        start=w.get("start", 0.0),
                        end=w.get("end", 0.0),
                        score=w.get("score", 0.0),
                    )
                    for w in seg["words"]
                    if "word" in w
                ]

            yield TranscriptSegment(
                text=seg.get("text", "").strip(),
                speaker=seg.get("speaker", "UNKNOWN"),
                start=seg.get("start", 0.0),
                end=seg.get("end", 0.0),
                words=words,
            )
