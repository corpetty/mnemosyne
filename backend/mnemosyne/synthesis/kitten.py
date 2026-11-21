"""KittenTTS 2 (KittenML, 1.7B ternary, zero-shot cloning from 5-30 s plus its transcript).

Evaluated first, at Corey's ask on the plan's PR (2026-11-20; Task 0 in the plan): good words and
similarity, PyTorch on Linux, MLX on Apple silicon. Its weights are under the Stellon Labs
Community License, not an open-source licence: free under US$1M in yearly revenue and under US$1M
in funding, commercial use must register with Stellon Labs, the AUP is incorporated by reference,
outputs may not train other models, and "Powered by KittenML" must be shown. So the install asks
the person to accept that licence first, and the Voices settings show the attribution. Qwen3-TTS
(qwen3.py) is the default since the owner's listening test; this stays as a choice.
"""

from __future__ import annotations

import sys
from pathlib import Path

from .workers import EngineSpec, WorkerSynthesizer

SPEC = EngineSpec(
    name="kitten",
    title="KittenTTS 2",
    space="kitten-tts-2",
    repo="KittenML/kitten-tts-2",
    # Pinned 2026-11-20 (the Hub's head of each repository then).
    revision="c93cb7c271baa51d9ae8204d5277988906f235a2",
    extra_revision="749d1c1a46eb10492095d68fbcf55691ccf137cd",  # ResembleAI/chatterbox-turbo, MIT
    license_name="Stellon Labs Community License",
    license_url="https://huggingface.co/KittenML/kitten-tts-2/blob/main/LICENSE.md",
    open_license=False,
    attribution="Powered by KittenML",
    size_note="about 3.5 GB with PyTorch",
    reference="joined",
)


class KittenSynthesizer(WorkerSynthesizer):
    def __init__(self, data_dir: Path, weights: str = ""):
        extra = ["--backend", "mlx"] if sys.platform == "darwin" else []
        if weights:  # "" = the lossless default, "emb4" = half the size
            extra += ["--weights", weights]
        super().__init__(SPEC, data_dir, extra)
