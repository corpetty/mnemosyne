"""pyannote.audio diarizer (community-1 by default). Needs torch and an HF token
with the model's license accepted."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from ...audio.mixer import decode_audio
from ..engine import SpeakerTurn

logger = logging.getLogger(__name__)

DEFAULT_MODEL = "pyannote/speaker-diarization-community-1"


class PyannoteDiarizer:
    name = "pyannote"

    def __init__(self, model: str = DEFAULT_MODEL, hf_token: str = "", device: str = "cuda"):
        self.model = model
        self.hf_token = hf_token
        self.device = device
        self._pipeline: Any = None

    def is_loaded(self) -> bool:
        return self._pipeline is not None

    async def load(self) -> None:
        if self._pipeline is not None:
            return
        logger.info("Loading diarization pipeline %s", self.model)

        def _load():
            import torch
            from pyannote.audio import Pipeline

            pipeline = Pipeline.from_pretrained(self.model, token=self.hf_token or None)
            if pipeline is None:
                raise RuntimeError(
                    f"Could not load {self.model}. Check HF_TOKEN and that the model "
                    "license is accepted on huggingface.co."
                )
            if self.device == "cuda" and torch.cuda.is_available():
                pipeline.to(torch.device("cuda"))
            return pipeline

        self._pipeline = await asyncio.to_thread(_load)

    async def unload(self) -> None:
        self._pipeline = None
        import torch

        if torch.cuda.is_available():
            torch.cuda.empty_cache()

    async def diarize(
        self,
        audio_path: str,
        min_speakers: int | None = None,
        max_speakers: int | None = None,
    ) -> list[SpeakerTurn]:
        if not self.is_loaded():
            await self.load()

        def _run():
            import torch

            # Decode with ffmpeg ourselves so Opus/OGG never depends on torchcodec.
            pcm = decode_audio(audio_path, sample_rate=16000)
            waveform = torch.from_numpy(pcm).unsqueeze(0)
            kwargs = {}
            if min_speakers is not None:
                kwargs["min_speakers"] = min_speakers
            if max_speakers is not None:
                kwargs["max_speakers"] = max_speakers
            output = self._pipeline({"waveform": waveform, "sample_rate": 16000}, **kwargs)
            return _turns_from_output(output)

        return await asyncio.to_thread(_run)


def _turns_from_output(output: Any) -> list[SpeakerTurn]:
    """Handle pyannote 4.x (DiarizeOutput) and 3.x (Annotation) return types."""
    annotation = getattr(output, "speaker_diarization", output)
    turns = []
    if hasattr(annotation, "itertracks"):
        for segment, _track, speaker in annotation.itertracks(yield_label=True):
            turns.append(
                SpeakerTurn(start=float(segment.start), end=float(segment.end), speaker=speaker)
            )
    else:
        for segment, speaker in annotation:
            turns.append(
                SpeakerTurn(start=float(segment.start), end=float(segment.end), speaker=speaker)
            )
    turns.sort(key=lambda t: t.start)
    return turns
