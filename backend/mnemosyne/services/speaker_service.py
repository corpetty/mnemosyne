"""Voice profiles: match diarized speakers to known people, enroll on rename."""

from __future__ import annotations

import logging
import math

from ..models.session import Session
from ..models.speaker import SpeakerProfile
from ..storage.sqlite import SessionRepository

logger = logging.getLogger(__name__)


def cosine(a: list[float], b: list[float]) -> float:
    if len(a) != len(b) or not a:
        return -1.0
    dot = sum(x * y for x, y in zip(a, b, strict=True))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    if na == 0 or nb == 0:
        return -1.0
    return dot / (na * nb)


class SpeakerService:
    def __init__(self, repo: SessionRepository, threshold: float = 0.6):
        self.repo = repo
        self.threshold = threshold

    def match(self, embeddings: dict[str, list[float]]) -> dict[str, str]:
        """Map diarization labels to known names.

        Greedy by score so one person is never assigned to two labels, and one
        label never gets two names.
        """
        profiles = self.repo.list_speakers()
        if not profiles or not embeddings:
            return {}
        scored = [
            (cosine(vec, p.embedding), label, p.name)
            for label, vec in embeddings.items()
            for p in profiles
        ]
        scored.sort(reverse=True)
        used_labels: set[str] = set()
        used_names: set[str] = set()
        mapping: dict[str, str] = {}
        for score, label, name in scored:
            if score < self.threshold:
                break
            if label in used_labels or name in used_names:
                continue
            mapping[label] = name
            used_labels.add(label)
            used_names.add(name)
            logger.info("Speaker %s matched profile %r (cos=%.3f)", label, name, score)
        return mapping

    def rename(self, session_id: str, label: str, name: str, enroll: bool = True) -> Session | None:
        """Relabel `label` -> `name` in the session; enroll the voice if we have it."""
        name = name.strip()
        if not name:
            raise ValueError("Name must not be empty")
        embeddings = self.repo.get_session_embeddings(session_id)
        session = self.repo.relabel_session_speaker(session_id, label, name)
        if session is None:
            return None
        if enroll and label in embeddings:
            self.repo.upsert_speaker_sample(name, embeddings[label])
        return session

    def enroll(self, name: str, embedding: list[float]) -> SpeakerProfile:
        return self.repo.upsert_speaker_sample(name, embedding)
