"""Which narration engine this machine runs (setting `synthesizer`).

auto: KittenTTS 2 when it is installed (services/tts_install.py), else none; demo: the tone
synthesizer of demo mode and the tests (fake.py); kitten; none. Engines are built lazily and
imported inside functions, so the API starts without any of them.
"""

from __future__ import annotations

from ..config import Settings
from .engine import Synthesizer


def resolve_synthesizer(settings: Settings) -> str:
    choice = (settings.synthesizer or "auto").strip().lower()
    if choice in ("demo", "none"):
        return choice
    from ..services import tts_install

    if tts_install.installed(settings.data_dir):
        return "kitten"
    return "none"


def build_synthesizer(settings: Settings) -> Synthesizer | None:
    kind = resolve_synthesizer(settings)
    if kind == "demo":
        from .fake import FakeSynthesizer

        return FakeSynthesizer()
    if kind == "kitten":
        from .kitten import KittenSynthesizer

        return KittenSynthesizer(settings.data_dir, settings.kitten_weights)
    return None
