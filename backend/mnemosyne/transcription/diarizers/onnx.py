"""Speaker diarization on the CPU with ONNX models, for machines without an NVIDIA GPU: no torch,
no Hugging Face token. sherpa-onnx (Apache-2.0, the `onnx` extra) runs pyannote's
segmentation-3.0 (MIT) and NVIDIA's TitaNet-small speaker embeddings (CC-BY-4.0), both as ONNX
from k2-fsa's releases, downloaded on first use (services/downloads.py).

Clustering a whole meeting at one threshold leaves a few small clusters for one person (a cough, a
laugh, a short "yes"); those join the closest real speaker by their mean voice, so a meeting of four
reads as four. Speaker embeddings are not returned: voice profiles live in pyannote's space.
"""

from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path
from typing import Any

from ...audio.mixer import decode_audio
from ...services.downloads import Download, extract, fetch
from ..engine import DiarizationResult, SpeakerTurn

logger = logging.getLogger(__name__)

SEGMENTATION = Download(
    url="https://github.com/k2-fsa/sherpa-onnx/releases/download/speaker-segmentation-models/"
    "sherpa-onnx-pyannote-segmentation-3-0.tar.bz2",
    sha256="24615ee884c897d9d2ba09bb4d30da6bb1b15e685065962db5b02e76e4996488",
    size=6_958_444,
    name="pyannote-segmentation-3-0.tar.bz2",
)
SEGMENTATION_MEMBER = "sherpa-onnx-pyannote-segmentation-3-0/model.onnx"
EMBEDDING = Download(
    url="https://github.com/k2-fsa/sherpa-onnx/releases/download/speaker-recongition-models/"
    "nemo_en_titanet_small.onnx",
    sha256="ad4a1802485d8b34c722d2a9d04249662f2ece5d28a7a039063ca22f515a789e",
    size=40_257_283,
    name="titanet_small.onnx",
)
# Clustering threshold (higher: fewer speakers). Short recordings need a lower one to tell their
# few voices apart; small clusters are merged after either way. On AMI ES2004a-d, with merging,
# 86-97% of single-speaker speech gets the right label (docs/plans/2026-10-01-start-anywhere.md).
THRESHOLD = 1.0
SHORT_THRESHOLD = 0.9
SHORT_SECONDS = 300
SAMPLE_RATE = 16000


def model_files(folder: Path, progress=None) -> tuple[Path, Path]:
    """The segmentation and embedding models, downloaded the first time (blocking)."""
    seg = folder / "segmentation.onnx"
    if not seg.is_file():
        archive = fetch(folder, SEGMENTATION, progress)
        extract(archive, {SEGMENTATION_MEMBER: seg.name}, folder)
        archive.unlink(missing_ok=True)
    return seg, fetch(folder, EMBEDDING, progress)


def merge_small_clusters(turns, centroid, min_share: float = 0.05, min_seconds: float = 3.0):
    """Turns (start, end, cluster) with clusters holding little speech relabelled to the closest
    big cluster. `centroid(cluster)` is a unit voice vector (numpy) or None."""
    speech: dict[int, float] = {}
    for s, e, k in turns:
        speech[k] = speech.get(k, 0.0) + e - s
    total = sum(speech.values())
    big = {k for k, d in speech.items() if d >= max(min_seconds, min_share * total)}
    if not big or len(big) == len(speech):
        return turns
    centers = {k: centroid(k) for k in speech}
    anchors = {k: v for k, v in centers.items() if k in big and v is not None}
    if not anchors:
        return turns
    remap = {
        k: max(anchors, key=lambda b: float(centers[k] @ anchors[b]))
        for k in speech
        if k not in big and centers[k] is not None
    }
    return [(s, e, remap.get(k, k)) for s, e, k in turns]


class OnnxDiarizer:
    name = "onnx"

    def __init__(self, models_dir: Path, threads: int | None = None):
        self.models_dir = Path(models_dir)
        self.threads = threads or max(1, min(8, (os.cpu_count() or 2) - 1))
        self._files: tuple[Path, Path] | None = None
        self._extractor: Any = None

    def is_loaded(self) -> bool:
        return self._files is not None

    async def load(self) -> None:
        if self._files is not None:
            return

        def _load():
            import sherpa_onnx

            files = model_files(self.models_dir)
            extractor = sherpa_onnx.SpeakerEmbeddingExtractor(
                sherpa_onnx.SpeakerEmbeddingExtractorConfig(
                    model=str(files[1]), num_threads=self.threads
                )
            )
            return files, extractor

        self._files, self._extractor = await asyncio.to_thread(_load)

    async def unload(self) -> None:
        self._files = None
        self._extractor = None

    def _config(self, clusters: int, threshold: float = THRESHOLD):
        import sherpa_onnx

        seg, emb = self._files  # type: ignore[misc]
        return sherpa_onnx.OfflineSpeakerDiarizationConfig(
            segmentation=sherpa_onnx.OfflineSpeakerSegmentationModelConfig(
                pyannote=sherpa_onnx.OfflineSpeakerSegmentationPyannoteModelConfig(model=str(seg)),
                num_threads=self.threads,
            ),
            embedding=sherpa_onnx.SpeakerEmbeddingExtractorConfig(
                model=str(emb), num_threads=self.threads
            ),
            clustering=sherpa_onnx.FastClusteringConfig(num_clusters=clusters, threshold=threshold),
            min_duration_on=0.3,
            min_duration_off=0.5,
        )

    def _centroid(self, samples, turns, cluster: int, longest: int = 12):
        import numpy as np

        mine = sorted((t for t in turns if t[2] == cluster), key=lambda t: t[0] - t[1])[:longest]
        vectors = []
        for s, e, _ in mine:
            if e - s < 0.5:
                continue
            stream = self._extractor.create_stream()
            stream.accept_waveform(
                SAMPLE_RATE, samples[int(s * SAMPLE_RATE) : int(e * SAMPLE_RATE)]
            )
            stream.input_finished()
            v = np.asarray(self._extractor.compute(stream))
            vectors.append(v / (np.linalg.norm(v) or 1.0))
        if not vectors:
            return None
        m = np.mean(vectors, axis=0)
        return m / (np.linalg.norm(m) or 1.0)

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
            import sherpa_onnx

            samples = decode_audio(audio_path, sample_rate=SAMPLE_RATE)

            short = len(samples) < SHORT_SECONDS * SAMPLE_RATE
            threshold = SHORT_THRESHOLD if short else THRESHOLD

            def run(clusters: int):
                sd = sherpa_onnx.OfflineSpeakerDiarization(self._config(clusters, threshold))

                def report(done: int, total: int) -> int:
                    if progress is not None and total:
                        progress(0.9 * done / total)
                    return 0

                result = sd.process(samples, callback=report).sort_by_start_time()
                return [(r.start, r.end, r.speaker) for r in result]

            fixed = min_speakers if min_speakers and min_speakers == max_speakers else -1
            turns = run(fixed)
            if fixed < 0:
                turns = merge_small_clusters(turns, lambda k: self._centroid(samples, turns, k))
                if max_speakers and len({k for *_, k in turns}) > max_speakers:
                    turns = run(max_speakers)
            if progress is not None:
                progress(1.0)
            return turns

        turns = await asyncio.to_thread(_run)
        # Labels in order of first appearance, as the other diarizers number them.
        order: dict[int, str] = {}
        for _, _, k in turns:
            order.setdefault(k, f"SPEAKER_{len(order):02d}")
        return DiarizationResult(
            turns=[SpeakerTurn(start=s, end=e, speaker=order[k]) for s, e, k in turns]
        )
