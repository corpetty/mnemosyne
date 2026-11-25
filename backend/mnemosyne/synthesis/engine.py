"""The synthesis stage: what an engine that speaks in an enrolled voice provides.

A narration voice is a handful of reference clips of one person, each with its exact transcript
(services/voices.py). An engine turns them into a `VoicePrompt` once (`prepare`: which audio and
text it conditions on) and then speaks any text with it (`speak`). Prompts are tagged with the
engine's `space`, like voice spaces of speaker embeddings: a prompt made by one engine is never
handed to another; the clips stay, so a new engine prepares again.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol, runtime_checkable

import numpy as np


class SynthesisError(RuntimeError):
    """An engine could not speak (not installed, crashed, refused the input)."""


@dataclass(frozen=True)
class RefClip:
    """One reference recording of the voice: a mono WAV (any rate) and what is said in it."""

    path: Path
    text: str
    seconds: float
    quality: float = 1.0  # 0..1 from the enrolment checks; better clips are preferred


@dataclass(frozen=True)
class VoicePrompt:
    """What an engine conditions on: usually one WAV of 10-30 s and its transcript. `extra` is
    the engine's own, kept as JSON with the prompt."""

    space: str
    path: Path
    text: str
    extra: dict = field(default_factory=dict)


@dataclass
class Speech:
    """Mono float32 samples in [-1, 1] at `sample_rate`, and whether the engine marked them as
    synthetic itself (an inaudible watermark)."""

    samples: np.ndarray
    sample_rate: int
    watermarked: bool = False


@runtime_checkable
class Synthesizer(Protocol):
    name: str
    space: str  # the engine family a prepared prompt belongs to
    model: str  # the model and weights in use, for the narration log

    def is_loaded(self) -> bool: ...

    async def load(self) -> None: ...

    async def unload(self) -> None: ...

    async def prepare(self, clips: list[RefClip], workdir: Path) -> VoicePrompt:
        """Choose and condition the reference audio. `workdir` is where the prompt's own files
        go (the voice's folder); the prompt's `path` must be inside it."""
        ...

    async def speak(self, prompt: VoicePrompt, text: str) -> Speech:
        """`text` in the voice: one chunk (a few sentences, chunking is the caller's)."""
        ...


def pick_reference(clips: list[RefClip], target: float = 20.0, most: float = 30.0) -> list[RefClip]:
    """The clips an engine should condition on: the best ones first until about `target`
    seconds, never more than `most` (most cloning models are trained on 5-30 s of reference and
    get worse, not better, with more). Clips without a transcript are skipped."""
    usable = [c for c in clips if c.text.strip() and c.seconds > 0]
    usable.sort(key=lambda c: (-c.quality, -c.seconds))
    chosen: list[RefClip] = []
    total = 0.0
    for clip in usable:
        if total >= target:
            break
        if total + clip.seconds > most and chosen:
            continue
        chosen.append(clip)
        total += clip.seconds
    return chosen
