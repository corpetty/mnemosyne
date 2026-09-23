"""Speaker labels for the live transcript.

Each committed live segment from a diarized source is embedded with the same
speaker-embedding model pyannote's diarization pipeline uses, then assigned to a
speaker by online clustering. Clusters that match a saved voice profile are named
after that person, possibly retroactively (reported as relabel events). The final
diarized transcript after stop is still authoritative.
"""

from __future__ import annotations

import asyncio
import logging
import math
from dataclasses import dataclass, field
from typing import Any, Protocol

import numpy as np

logger = logging.getLogger(__name__)

MIN_SECONDS = 1.0  # shorter segments carry too little voice to embed reliably


class SpeakerEmbedder(Protocol):
    name: str

    def is_loaded(self) -> bool: ...

    async def load(self) -> None: ...

    async def unload(self) -> None: ...

    async def embed(self, pcm: np.ndarray, sample_rate: int) -> list[float] | None:
        """Embedding for mono int16/float PCM, or None when too short."""
        ...


class PyannoteEmbedder:
    """The embedding model inside a pyannote diarization pipeline (community-1 by
    default), so live labels share a space with saved voice profiles."""

    name = "pyannote"

    def __init__(self, pipeline_model: str, hf_token: str = "", device: str = "auto"):
        self.pipeline_model = pipeline_model
        self.hf_token = hf_token
        self.device = device
        self._embedding: Any = None
        self._rate = 16000

    def is_loaded(self) -> bool:
        return self._embedding is not None

    async def load(self) -> None:
        if self._embedding is not None:
            return

        def _load():
            import torch
            from pyannote.audio import Pipeline

            pipeline = Pipeline.from_pretrained(self.pipeline_model, token=self.hf_token or None)
            if pipeline is None or not hasattr(pipeline, "_embedding"):
                raise RuntimeError(f"No embedding model in {self.pipeline_model}")
            emb = pipeline._embedding
            use_cuda = self.device == "cuda" or (
                self.device == "auto" and torch.cuda.is_available()
            )
            emb.to(torch.device("cuda" if use_cuda else "cpu"))
            return emb

        self._embedding = await asyncio.to_thread(_load)
        self._rate = int(self._embedding.sample_rate)
        logger.info("Live speaker embedder ready (%s, %d Hz)", self.pipeline_model, self._rate)

    async def unload(self) -> None:
        self._embedding = None

    async def embed(self, pcm: np.ndarray, sample_rate: int) -> list[float] | None:
        if self._embedding is None:
            await self.load()
        if pcm.size < MIN_SECONDS * sample_rate:
            return None

        def _run():
            import torch
            import torchaudio.functional as F

            x = pcm.astype(np.float32)
            if pcm.dtype == np.int16:
                x = x / 32768.0
            wav = torch.from_numpy(x).unsqueeze(0)
            if sample_rate != self._rate:
                wav = F.resample(wav, sample_rate, self._rate)
            vec = self._embedding(wav.unsqueeze(0))[0]
            return None if np.isnan(vec).any() else [float(v) for v in vec]

        return await asyncio.to_thread(_run)


def _norm(v: list[float] | np.ndarray) -> np.ndarray:
    a = np.asarray(v, dtype=np.float64)
    n = float(np.linalg.norm(a))
    return a / n if n > 0 else a


def cosine(a, b) -> float:
    a, b = _norm(a), _norm(b)
    if a.shape != b.shape or not a.size:
        return -1.0
    return float(np.dot(a, b))


@dataclass
class Cluster:
    number: int
    total: np.ndarray  # sum of normalized embeddings
    count: int = 1
    name: str | None = None

    @property
    def centroid(self) -> np.ndarray:
        return _norm(self.total)

    @property
    def label(self) -> str:
        return self.name or f"Speaker {self.number}"


@dataclass
class OnlineClusterer:
    """Greedy online clustering by cosine similarity to running centroids.

    `known` maps person name -> voice-profile embedding. A cluster is named once its
    centroid matches a profile above `known_threshold` (one cluster per name).
    """

    threshold: float = 0.45
    known_threshold: float = 0.6
    # Two clusters whose centroids converge this far are the same person, split
    # early by short or noisy segments.
    merge_margin: float = 0.1
    max_speakers: int = 12
    known: dict[str, list[float]] = field(default_factory=dict)
    clusters: list[Cluster] = field(default_factory=list)
    _merged_into: dict[int, Cluster] = field(default_factory=dict, repr=False)

    def assign(self, embedding: list[float]) -> tuple[str, list[tuple[str, str]]]:
        """Return (label for this segment, [(old_label, new_label)] renames)."""
        v = _norm(embedding)
        best, best_score = None, -1.0
        for c in self.clusters:
            s = float(np.dot(v, c.centroid))
            if s > best_score:
                best, best_score = c, s
        if best is not None and (
            best_score >= self.threshold or len(self.clusters) >= self.max_speakers
        ):
            best.total = best.total + v
            best.count += 1
            cluster = best
        else:
            cluster = Cluster(number=len(self.clusters) + 1, total=v.copy())
            self.clusters.append(cluster)
        renames = self._merge()
        renames += self._name_all()
        final = next((c for c in self.clusters if c is cluster), None)
        if final is None:  # merged away: follow it to the cluster it joined
            final = self._merged_into[id(cluster)]
        return final.label, renames

    def _merge(self) -> list[tuple[str, str]]:
        renames = []
        merged = True
        while merged:
            merged = False
            for a in self.clusters:
                for b in self.clusters:
                    if a is b or a.number > b.number:
                        continue
                    if float(np.dot(a.centroid, b.centroid)) >= self.threshold + self.merge_margin:
                        # keep the older cluster (and a known name if either has one)
                        keep, drop = (a, b) if (a.name or not b.name) else (b, a)
                        old_label = drop.label
                        keep.total = keep.total + drop.total
                        keep.count += drop.count
                        self.clusters.remove(drop)
                        self._merged_into[id(drop)] = keep
                        renames.append((old_label, keep.label))
                        merged = True
                        break
                if merged:
                    break
        return renames

    def _name_all(self) -> list[tuple[str, str]]:
        renames = []
        for c in self.clusters:
            renames += self._name(c)
        return renames

    def _name(self, cluster: Cluster) -> list[tuple[str, str]]:
        if cluster.name is not None or not self.known:
            return []
        taken = {c.name for c in self.clusters if c.name}
        best_name, best_score = None, -math.inf
        for name, vec in self.known.items():
            if name in taken:
                continue
            s = cosine(cluster.centroid, vec)
            if s > best_score:
                best_name, best_score = name, s
        if best_name is None or best_score < self.known_threshold:
            return []
        old = cluster.label
        cluster.name = best_name
        return [(old, best_name)]
