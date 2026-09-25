"""NVIDIA Nemotron 3 Diarization (Streaming Sortformer, up to 8 speakers) via NeMo.

Trial implementation: needs NeMo from git main (PyPI 3.0.0 lacks the model's RoPE
attention), which is not in any extra yet. The model gives no speaker embeddings, so
voice profiles are served by embedding each speaker's longest clean turns with the
pyannote embedding model (`embedder`); without one, `embeddings` stays empty.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import numpy as np

from ...audio.mixer import decode_audio
from ..engine import DiarizationResult, SpeakerTurn

logger = logging.getLogger(__name__)

DEFAULT_MODEL = "nvidia/Nemotron-3-Diarization"
MAX_SPEAKERS = 8
RATE = 16000

# Streaming parameters in 80 ms frames: the model card's "offline" (30.4 s) setting.
OFFLINE = {
    "spkcache_len": 264,
    "fifo_len": 40,
    "chunk_len": 340,
    "chunk_right_context": 40,
    "spkcache_update_period": 300,
}

# Per speaker, embed up to this much of their longest non-overlapping turns.
EMBED_MIN_TURN = 1.0
EMBED_MAX_TURNS = 8
EMBED_MAX_SECONDS = 60.0


class NemotronDiarizer:
    name = "nemotron"

    def __init__(self, model: str = DEFAULT_MODEL, device: str = "cuda", embedder: Any = None):
        self.model = model
        self.device = device
        self.embedder = embedder
        self._model: Any = None

    def is_loaded(self) -> bool:
        return self._model is not None

    async def load(self) -> None:
        if self._model is not None:
            return
        logger.info("Loading diarization model %s", self.model)

        def _load():
            import torch

            try:
                from nemo.collections.asr.models import SortformerEncLabelModel
            except ImportError as e:
                raise RuntimeError(
                    "diarizer=nemotron needs NVIDIA NeMo (nemo_toolkit[asr] from git main)"
                ) from e
            device = "cuda" if self.device == "cuda" and torch.cuda.is_available() else "cpu"
            model = SortformerEncLabelModel.from_pretrained(self.model, map_location=device)
            model.eval()
            for key, value in OFFLINE.items():
                setattr(model.sortformer_modules, key, value)
            model._check_streaming_parameters()
            return model

        self._model = await asyncio.to_thread(_load)
        if self.embedder is not None:
            try:
                await self.embedder.load()
            except Exception:
                logger.warning(
                    "Speaker embedder unavailable; voice profiles will not match", exc_info=True
                )
                self.embedder = None

    async def unload(self) -> None:
        self._model = None
        if self.embedder is not None:
            await self.embedder.unload()
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
        if max_speakers is not None and max_speakers > MAX_SPEAKERS:
            logger.debug("Nemotron tracks at most %d speakers", MAX_SPEAKERS)

        pcm = decode_audio(audio_path, sample_rate=RATE).astype(np.float32)

        def _run():
            import torch

            with torch.inference_mode():
                segments = self._model.diarize(
                    audio=[pcm], sample_rate=RATE, batch_size=1, verbose=False
                )
            return parse_segments(segments[0])

        turns = await asyncio.to_thread(_run)
        if progress is not None:
            progress(0.9)
        embeddings = await self._embed_speakers(pcm, turns) if self.embedder else {}
        if progress is not None:
            progress(1.0)
        return DiarizationResult(turns=turns, embeddings=embeddings)

    async def _embed_speakers(self, pcm: np.ndarray, turns: list[SpeakerTurn]):
        out: dict[str, list[float]] = {}
        for speaker, spans in embedding_spans(turns).items():
            vecs = []
            for start, end in spans:
                vec = await self.embedder.embed(pcm[int(start * RATE) : int(end * RATE)], RATE)
                if vec is not None:
                    vecs.append(np.asarray(vec) / (np.linalg.norm(vec) or 1.0))
            if vecs:
                out[speaker] = np.mean(vecs, axis=0).tolist()
        return out


def parse_segments(lines: list[str]) -> list[SpeakerTurn]:
    """NeMo's "begin end speaker_N" strings -> turns labelled like pyannote's."""
    turns = []
    for line in lines:
        start, end, speaker = line.split()
        index = int(speaker.rsplit("_", 1)[-1])
        turns.append(
            SpeakerTurn(start=float(start), end=float(end), speaker=f"SPEAKER_{index:02d}")
        )
    turns.sort(key=lambda t: t.start)
    return turns


def embedding_spans(turns: list[SpeakerTurn]) -> dict[str, list[tuple[float, float]]]:
    """Per speaker, their longest turns that overlap no other speaker, capped in count
    and total length, so each embedding is one clean voice."""
    clean: dict[str, list[tuple[float, float]]] = {}
    for t in turns:
        if t.end - t.start < EMBED_MIN_TURN:
            continue
        overlapped = any(
            o.speaker != t.speaker and o.start < t.end and t.start < o.end for o in turns
        )
        if not overlapped:
            clean.setdefault(t.speaker, []).append((t.start, t.end))
    out = {}
    for speaker, spans in clean.items():
        spans.sort(key=lambda s: s[0] - s[1])  # longest first
        picked, total = [], 0.0
        for start, end in spans[:EMBED_MAX_TURNS]:
            end = min(end, start + EMBED_MAX_SECONDS - total)
            picked.append((start, end))
            total += end - start
            if total >= EMBED_MAX_SECONDS:
                break
        out[speaker] = picked
    return out
