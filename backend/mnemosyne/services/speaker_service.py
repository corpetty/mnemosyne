"""Voice profiles: match diarized speakers to known people, enroll on rename."""

from __future__ import annotations

import logging
import math

from ..models.session import Session
from ..models.speaker import PYANNOTE_SPACE, SpeakerProfile
from ..storage.sqlite import SessionRepository

logger = logging.getLogger(__name__)


def has_synthetic(repo: SessionRepository, session_id: str) -> bool:
    """The meeting has audio marked as a narration (history `synthetic_audio`, written on import
    by api/routes/audio.py): its voices are not learned into profiles."""
    return any(e["kind"] == "synthetic_audio" for e in repo.events(session_id))


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

    def match(
        self,
        embeddings: dict[str, list[float]],
        space: str = PYANNOTE_SPACE,
        threshold: float | None = None,
    ) -> dict[str, str]:
        """Map diarization labels to known names. `embeddings` are in voice space `space`;
        only profiles with a voice in that space can match (a vector of another space is never
        compared). `threshold` overrides the service's (each space has its own cosine scale:
        transcription/speaker_embedders.thresholds).

        Greedy by score so one person is never assigned to two labels, and one
        label never gets two names.
        """
        threshold = self.threshold if threshold is None else threshold
        profiles = [p for p in self.repo.list_speakers() if space in p.voices]
        if not profiles or not embeddings:
            return {}
        scored = [
            (cosine(vec, p.voices[space]), label, p.name)
            for label, vec in embeddings.items()
            for p in profiles
        ]
        scored.sort(reverse=True)
        used_labels: set[str] = set()
        used_names: set[str] = set()
        mapping: dict[str, str] = {}
        for score, label, name in scored:
            if score < threshold:
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
        voices = self.repo.get_session_voices(session_id)
        session = self.repo.relabel_session_speaker(session_id, label, name)
        if session is None:
            return None
        if enroll and has_synthetic(self.repo, session_id):
            # A narration made in someone's voice must never teach their profile that voice.
            logger.info("Not enrolling %r from %s: it contains synthetic speech", name, session_id)
            enroll = False
        if enroll:
            # Every space the label has a stored voice in.
            for space, by_label in voices.items():
                if label in by_label:
                    self.repo.upsert_speaker_sample(name, by_label[label], space)
        return session

    def enroll(
        self, name: str, embedding: list[float], space: str = PYANNOTE_SPACE
    ) -> SpeakerProfile:
        return self.repo.upsert_speaker_sample(name, embedding, space)
