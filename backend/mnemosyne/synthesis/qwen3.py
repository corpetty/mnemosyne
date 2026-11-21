"""Qwen3-TTS 12Hz 1.7B Base (Alibaba Qwen team), zero-shot cloning from one reference clip.

The default engine since 2026-11-20: in the owner's listening test of the same sentences it
sounded most like him, ahead of KittenTTS 2, of Qwen3-TTS 0.6B and of both sizes fine-tuned on his
13 minutes; it also had the fewest word errors (1.2 % round trip, similarity 0.78 to his real
recordings of the same sentences). Apache-2.0, code and weights, so there is no licence to
accept. It runs in its own venv like KittenTTS (transformers 4.57, its own torch): CUDA on
NVIDIA, the GPU (MPS) on a Mac, else the CPU, which is slow for 1.7B (plan, "Qwen3-TTS").

Fine-tuning was tried and is not offered: the official script (finetuning/sft_12hz.py) makes the
model speak faster with every epoch (QwenLM/Qwen3-TTS#179) and broke at higher learning rates.
"""

from __future__ import annotations

from pathlib import Path

from .workers import EngineSpec, WorkerSynthesizer

SPEC = EngineSpec(
    name="qwen3",
    title="Qwen3-TTS 1.7B",
    space="qwen3-tts-1.7b",
    repo="Qwen/Qwen3-TTS-12Hz-1.7B-Base",
    # Pinned 2026-11-20 (the Hub's head then).
    revision="fd4b254389122332181a7c3db7f27e918eec64e3",
    license_name="Apache License 2.0",
    license_url="https://huggingface.co/Qwen/Qwen3-TTS-12Hz-1.7B-Base",
    open_license=True,
    attribution="",
    size_note="about 7 GB with PyTorch (more with CUDA)",
    reference="single",
)


class Qwen3Synthesizer(WorkerSynthesizer):
    def __init__(self, data_dir: Path):
        super().__init__(SPEC, data_dir)
