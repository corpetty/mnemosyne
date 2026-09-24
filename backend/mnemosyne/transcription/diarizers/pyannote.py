"""pyannote.audio diarizer (community-1 by default). Needs torch and an HF token
with the model's license accepted."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from ...audio.mixer import decode_audio
from ..engine import DiarizationResult, SpeakerTurn

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
        progress=None,
    ) -> DiarizationResult:
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
            if progress is not None:
                kwargs["hook"] = _progress_hook(progress)
            output = self._pipeline({"waveform": waveform, "sample_rate": 16000}, **kwargs)
            if progress is not None:
                progress(1.0)
            return _result_from_output(output)

        return await asyncio.to_thread(_run)


# Share of pyannote's work per step (segmentation and embeddings dominate).
_STEPS = {"segmentation": (0.0, 0.35), "speaker_counting": (0.35, 0.4), "embeddings": (0.4, 0.95)}


def _progress_hook(progress):
    def hook(step_name, step_artifact, file=None, total=None, completed=None):
        lo, hi = _STEPS.get(step_name, (0.95, 1.0))
        frac = (completed / total) if (total and completed is not None) else 1.0
        progress(float(lo + (hi - lo) * min(max(frac, 0.0), 1.0)))

    return hook


def _result_from_output(output: Any) -> DiarizationResult:
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

    embeddings: dict[str, list[float]] = {}
    raw = getattr(output, "speaker_embeddings", None)
    labels = list(annotation.labels()) if hasattr(annotation, "labels") else []
    if raw is not None and labels:
        try:
            import numpy as np

            arr = np.asarray(raw, dtype=float)
            if arr.ndim == 2 and arr.shape[0] == len(labels):
                for label, row in zip(labels, arr, strict=True):
                    if not np.isnan(row).any():
                        embeddings[label] = row.tolist()
        except Exception:
            logger.debug("Could not read speaker embeddings", exc_info=True)
    return DiarizationResult(turns=turns, embeddings=embeddings)
