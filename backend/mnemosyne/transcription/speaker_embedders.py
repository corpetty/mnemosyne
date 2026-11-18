"""Speaker embedders that run without torch, and the thresholds that go with each voice space.

`OnnxEmbedder` is WeSpeaker's ResNet34-LM (CC-BY-4.0) as ONNX from k2-fsa's sherpa-onnx release,
run by sherpa-onnx on the CPU (the `onnx` extra). Its voices are in their own space,
`wespeaker-r34`, until docs/plans/2026-11-17-speakers-without-torch.md task 0 shows that they
equal pyannote's (then `space` becomes "pyannote" and one migration retags the stored voices).

Vectors are only compared inside one space. The cosine thresholds differ per space: the two
settings (`live_speaker_threshold`, `speaker_match_threshold`) were set for pyannote's model and
keep meaning that; other spaces have constants here.
"""

from __future__ import annotations

import asyncio
import logging
import os
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

import numpy as np

from ..models.speaker import WESPEAKER_SPACE
from ..services.downloads import Download, fetch

if TYPE_CHECKING:
    from ..config import Settings

logger = logging.getLogger(__name__)

MODEL = Download(
    url="https://github.com/k2-fsa/sherpa-onnx/releases/download/speaker-recongition-models/"
    "wespeaker_en_voxceleb_resnet34_LM.onnx",
    sha256="e9848563da86f263117134dfd7ad63c92355b37de492b55e325400c9d9c39012",
    size=26_530_550,
    name="wespeaker_en_voxceleb_resnet34_LM.onnx",
)
MIN_SECONDS = 1.0  # shorter audio carries too little voice (same rule as live_speakers)
MAX_SECONDS = 60.0  # a minute says all there is to say about a voice


@dataclass(frozen=True)
class Thresholds:
    """Cosine thresholds of one voice space: `cluster` joins live segments into one speaker,
    `match` names a speaker after a saved voice profile."""

    cluster: float
    match: float


# Spaces other than pyannote. wespeaker-r34 starts at pyannote's defaults (0.45, 0.6); the
# calibration run on AMI (plan task 7) may change them.
SPACE_THRESHOLDS: dict[str, Thresholds] = {WESPEAKER_SPACE: Thresholds(cluster=0.45, match=0.6)}


def thresholds(space: str, settings: Settings) -> Thresholds:
    """The cosine thresholds for voices of `space`. pyannote (and a space we know nothing about,
    which is how voices have always been treated) uses the two settings."""
    if space in SPACE_THRESHOLDS:
        return SPACE_THRESHOLDS[space]
    return Thresholds(
        cluster=settings.live_speaker_threshold, match=settings.speaker_match_threshold
    )


def default_threads() -> int:
    return max(1, min(4, (os.cpu_count() or 2) - 1))


class OnnxEmbedder:
    """A `SpeakerEmbedder` (live_speakers.py) on sherpa-onnx's speaker embedding extractor."""

    name = "wespeaker-resnet34"
    space = WESPEAKER_SPACE

    def __init__(self, models_dir: Path, threads: int | None = None):
        self.models_dir = Path(models_dir)
        self.threads = threads or default_threads()
        self._extractor: Any = None
        self._compute = threading.Lock()  # one extractor, several callers (live sources)

    def is_loaded(self) -> bool:
        return self._extractor is not None

    async def load(self) -> None:
        if self._extractor is not None:
            return

        def _load():
            import sherpa_onnx

            model = fetch(self.models_dir, MODEL)
            return sherpa_onnx.SpeakerEmbeddingExtractor(
                sherpa_onnx.SpeakerEmbeddingExtractorConfig(
                    model=str(model), num_threads=self.threads
                )
            )

        self._extractor = await asyncio.to_thread(_load)
        logger.info("Speaker embedder ready (%s, %d thread(s))", MODEL.name, self.threads)

    async def unload(self) -> None:
        self._extractor = None

    async def embed(self, pcm: np.ndarray, sample_rate: int) -> list[float] | None:
        """The voice in mono int16 or float PCM, or None when it is under a second long."""
        if self._extractor is None:
            await self.load()
        if pcm.size < MIN_SECONDS * sample_rate:
            return None
        samples = pcm[: int(MAX_SECONDS * sample_rate)]
        extractor = self._extractor

        def _run():
            x = samples.astype(np.float32)
            if samples.dtype == np.int16:
                x = x / 32768.0
            with self._compute:
                stream = extractor.create_stream()
                stream.accept_waveform(sample_rate, x)  # resamples inside when needed
                stream.input_finished()
                if not extractor.is_ready(stream):
                    return None
                vec = np.asarray(extractor.compute(stream), dtype=np.float64)
            return None if not np.isfinite(vec).all() else [float(v) for v in vec]

        return await asyncio.to_thread(_run)
