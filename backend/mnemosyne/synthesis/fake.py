"""A synthesizer for tests and demo mode: a short tone per word, deterministic, no model.

It never plays anything (nothing here does: narrations are files, see CLAUDE.md on test audio).
The tone's pitch follows the prompt's text, so two voices sound different in the demo.
"""

from __future__ import annotations

import hashlib
import shutil
from pathlib import Path

import numpy as np

from .engine import RefClip, Speech, VoicePrompt, pick_reference

RATE = 24000
WORD_SECONDS = 0.18
GAP_SECONDS = 0.06


class FakeSynthesizer:
    name = "demo"
    space = "demo"
    model = "demo-tones"

    def __init__(self, watermarked: bool = True):
        self._loaded = False
        self.watermarked = watermarked
        self.spoken: list[str] = []  # tests read what was asked for

    def is_loaded(self) -> bool:
        return self._loaded

    async def load(self) -> None:
        self._loaded = True

    async def unload(self) -> None:
        self._loaded = False

    async def prepare(self, clips: list[RefClip], workdir: Path) -> VoicePrompt:
        chosen = pick_reference(clips) or clips[:1]
        workdir.mkdir(parents=True, exist_ok=True)
        dest = workdir / "prompt-demo.wav"
        if chosen:
            shutil.copyfile(chosen[0].path, dest)
        else:
            dest.write_bytes(b"")
        return VoicePrompt(self.space, dest, " ".join(c.text for c in chosen))

    async def speak(self, prompt: VoicePrompt, text: str) -> Speech:
        self.spoken.append(text)
        seed = int(hashlib.sha256(prompt.text.encode()).hexdigest()[:6], 16)
        base = 140.0 + seed % 120  # Hz
        parts = []
        t = np.arange(int(RATE * WORD_SECONDS)) / RATE
        envelope = np.hanning(t.size)
        for i, _ in enumerate(text.split()):
            tone = 0.2 * envelope * np.sin(2 * np.pi * (base + 15 * (i % 3)) * t)
            parts.extend([tone, np.zeros(int(RATE * GAP_SECONDS))])
        samples = np.concatenate(parts) if parts else np.zeros(int(RATE * 0.2))
        return Speech(samples.astype(np.float32), RATE, watermarked=self.watermarked)
