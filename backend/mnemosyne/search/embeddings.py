"""Text embedders. The default is a model2vec static model: CPU only, no torch, a few
milliseconds per batch. Loaded lazily; tests and demo mode use hashed embeddings."""

from __future__ import annotations

import hashlib
import re
from typing import Protocol

import numpy as np


class Embedder(Protocol):
    name: str
    min_score: float  # cosine below which a match is noise, for this model

    def embed(self, texts: list[str]) -> np.ndarray:
        """Unit-length float32 vectors, one row per text."""
        ...


def _normalize(m: np.ndarray) -> np.ndarray:
    m = np.asarray(m, dtype=np.float32)
    norms = np.linalg.norm(m, axis=1, keepdims=True)
    norms[norms == 0] = 1.0
    return m / norms


class Model2VecEmbedder:
    def __init__(self, model: str):
        from model2vec import StaticModel

        self.name = model
        # Measured on real meetings: right answers 0.15-0.35, unrelated text 0.12 or less.
        self.min_score = 0.14
        self._model = StaticModel.from_pretrained(model)

    def embed(self, texts: list[str]) -> np.ndarray:
        if not texts:
            return np.zeros((0, 1), dtype=np.float32)
        return _normalize(self._model.encode(texts))


_STOP = set(
    "the and for with that this what when where who why how are was were will would can "
    "could should have has had not but our you your they them their its from about into "
    "all any did does done been being there here then than just also".split()
)


class HashEmbedder:
    """Bag of words hashed into `dim` buckets, with optional synonym groups mapped to one
    bucket. Deterministic and dependency-free: for tests and demo mode."""

    def __init__(self, dim: int = 256, synonyms: list[set[str]] | None = None):
        self.name = f"hash-{dim}"
        self.min_score = 0.15
        self.dim = dim
        self._canon: dict[str, str] = {}
        for group in synonyms or []:
            head = sorted(group)[0]
            for w in group:
                self._canon[w] = head

    def _bucket(self, word: str) -> int:
        word = self._canon.get(word, word)
        return (
            int.from_bytes(hashlib.blake2b(word.encode(), digest_size=4).digest(), "little")
            % self.dim
        )

    def embed(self, texts: list[str]) -> np.ndarray:
        m = np.zeros((len(texts), self.dim), dtype=np.float32)
        for i, t in enumerate(texts):
            for w in re.findall(r"\w+", t.lower()):
                if len(w) > 2 and w not in _STOP:
                    m[i, self._bucket(w)] += 1.0
        return _normalize(m)


def build_embedder(settings) -> Embedder:
    from .. import demo

    if demo.enabled():
        return HashEmbedder(synonyms=demo.SYNONYMS)
    return Model2VecEmbedder(settings.embedding_model)
