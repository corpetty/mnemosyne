"""Which narration engine this machine runs (setting `synthesizer`).

auto: Qwen3-TTS 1.7B when it is installed (the default since the owner's listening test,
synthesis/qwen3.py), else KittenTTS 2 when that is, else none; qwen3, kitten: that one (none
when it is not installed); demo: the tone synthesizer of demo mode and the tests (fake.py);
none. Engines are built lazily and imported inside functions, so the API starts without them.
"""

from __future__ import annotations

from ..config import Settings
from .engine import Synthesizer

ORDER = ("qwen3", "kitten")  # what auto prefers


def resolve_synthesizer(settings: Settings) -> str:
    choice = (settings.synthesizer or "auto").strip().lower()
    if choice in ("demo", "none"):
        return choice
    from ..services import tts_install

    wanted = ORDER if choice == "auto" else (choice,)
    for name in wanted:
        spec = tts_install.ENGINES.get(name)
        if spec is not None and tts_install.installed(spec, settings.data_dir):
            return name
    return "none"


def build_synthesizer(settings: Settings) -> Synthesizer | None:
    kind = resolve_synthesizer(settings)
    if kind == "demo":
        from .fake import FakeSynthesizer

        return FakeSynthesizer()
    if kind == "kitten":
        from .kitten import KittenSynthesizer

        return KittenSynthesizer(settings.data_dir, settings.kitten_weights)
    if kind == "qwen3":
        from .qwen3 import Qwen3Synthesizer

        return Qwen3Synthesizer(settings.data_dir)
    return None
