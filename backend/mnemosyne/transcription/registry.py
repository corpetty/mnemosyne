"""Build a TranscriptionEngine from settings.

Heavy imports happen inside the builders so choosing `remote` + `none` never
imports torch.
"""

from __future__ import annotations

import functools

from ..config import Settings
from .composed import ComposedEngine
from .engine import Diarizer, Transcriber
from .glossary import initial_prompt, parse_glossary

TRANSCRIBERS = ("auto", "whisperx", "parakeet", "phonon", "remote")
DIARIZERS = ("auto", "nemotron", "pyannote", "onnx", "none")


def installed(*modules: str) -> bool:
    """These modules can be imported (without importing them)."""
    import importlib.util

    try:
        return all(importlib.util.find_spec(m) is not None for m in modules)
    except (ImportError, ValueError):
        return False


@functools.cache
def cuda_works() -> bool:
    """torch is installed and sees a CUDA GPU (imported once, when an engine is built)."""
    if not installed("torch"):
        return False
    import torch

    return torch.cuda.is_available()


def resolve_transcriber(settings: Settings, kind: str | None = None) -> str:
    """The transcriber this machine runs: `auto` is WhisperX with a working GPU, else Parakeet;
    a saved WhisperX whose packages are not installed (the GPU extra) runs Parakeet instead."""
    kind = kind or settings.transcriber
    if kind == "auto":
        return "whisperx" if installed("whisperx") and cuda_works() else "parakeet"
    if kind == "whisperx" and not installed("whisperx", "torch") and installed("onnx_asr"):
        return "parakeet"
    if kind == "phonon" and not phonon_available(settings) and installed("onnx_asr"):
        return "parakeet"  # chosen, then uninstalled: Parakeet is the same model, bigger
    return kind


def phonon_available(settings: Settings) -> bool:
    """Fermion's `phonon` program is installed (transcribers/phonon.py; not auto: English only)."""
    from .transcribers.phonon import find_phonon

    return find_phonon(settings.phonon_command) is not None


def build_transcriber(
    settings: Settings, kind: str | None = None, threads: int | None = None
) -> Transcriber:
    kind = resolve_transcriber(settings, kind)
    if kind == "whisperx":
        from .transcribers.whisperx import WhisperXTranscriber

        return WhisperXTranscriber(
            model_size=settings.whisper_model_size,
            compute_type=settings.whisper_compute_type,
            batch_size=settings.whisper_batch_size,
            vad_method=settings.whisper_vad,
            initial_prompt=initial_prompt(parse_glossary(settings.glossary)),
        )
    if kind == "parakeet":
        from .transcribers.parakeet import ParakeetTranscriber

        return ParakeetTranscriber(
            model=settings.parakeet_model,
            provider=settings.onnx_provider,
            quantization=settings.parakeet_quantization or None,
            threads=threads,
        )
    if kind == "phonon":
        from .transcribers.phonon import PhononTranscriber

        return PhononTranscriber(
            command=settings.phonon_command,
            log_path=settings.data_dir / "logs" / "phonon.log",
        )
    if kind == "demo":
        from ..demo import DemoTranscriber

        return DemoTranscriber()
    if kind == "remote":
        from .transcribers.remote import RemoteTranscriber

        if not settings.remote_stt_url:
            raise ValueError("transcriber=remote requires remote_stt_url")
        return RemoteTranscriber(
            base_url=settings.remote_stt_url,
            model=settings.remote_stt_model,
            api_key=settings.remote_stt_api_key,
        )
    raise ValueError(f"Unknown transcriber '{kind}'. Choose one of {TRANSCRIBERS}")


def build_live_transcriber(settings: Settings) -> Transcriber:
    """A separate transcriber instance for live use, so it never shares state
    with the engine running final jobs."""
    return build_transcriber(settings, settings.live_transcriber, threads=settings.live_threads)


def nemotron_available() -> bool:
    """NeMo is installed (the gpu extra) and torch sees a CUDA GPU."""
    import importlib.util

    try:
        if importlib.util.find_spec("nemo") is None:
            return False
        import torch
    except ImportError:
        return False
    return torch.cuda.is_available()


def resolve_diarizer(settings: Settings) -> str:
    """The diarizer this machine runs. `auto`: Nemotron with a GPU, else pyannote when it is
    installed and has its token, else the ONNX diarizer (CPU, no token), else none. A saved
    choice whose packages are not installed falls back the same way."""
    kind = settings.diarizer
    if kind not in DIARIZERS:
        return kind  # "demo", or a name build_diarizer rejects
    if kind in ("none", "onnx"):
        return kind if kind != "onnx" or installed("sherpa_onnx") else _cpu_fallback()
    if kind == "nemotron" and nemotron_available():
        return kind
    if kind == "pyannote" and installed("pyannote.audio", "torch"):
        return kind
    if kind == "auto" and nemotron_available():
        return "nemotron"
    if kind == "auto" and installed("pyannote.audio", "torch") and settings.hf_token:
        return "pyannote"
    return _cpu_fallback()


def _cpu_fallback() -> str:
    return "onnx" if installed("sherpa_onnx") else "none"


def build_live_embedder(settings: Settings):
    """pyannote speaker embedder (live labels; voice profiles under nemotron), or None when
    pyannote/torch are unavailable."""
    if settings.diarizer not in ("auto", "pyannote", "nemotron"):
        return None
    try:
        import pyannote.audio  # noqa: F401
        import torch  # noqa: F401
    except ImportError:
        return None
    from .live_speakers import PyannoteEmbedder

    return PyannoteEmbedder(settings.diarization_model, settings.hf_token)


def resolve_live_diarizer(settings: Settings) -> str:
    """ "streaming" or "clustering": where live speaker labels come from on this machine."""
    if settings.live_diarizer in ("streaming", "clustering"):
        return settings.live_diarizer
    if settings.diarizer in ("none", "demo"):
        return "clustering"
    return "streaming" if nemotron_available() else "clustering"


def build_live_stream_model(settings: Settings):
    """Nemotron streaming for live speaker labels (diarizers/nemotron_stream.py), or None.
    Never the engine's diarizer: the streaming settings live on the model."""
    if not settings.live_diarization or resolve_live_diarizer(settings) != "streaming":
        return None
    import importlib.util

    if importlib.util.find_spec("nemo") is None:
        return None
    from .diarizers.nemotron_stream import NemotronStreamModel

    return NemotronStreamModel()


def build_live_rediarizer(settings: Settings):
    """The diarizer that re-diarizes live recordings (live_rediarize.py), or None."""
    if not settings.live_diarization or settings.live_rediarize == "off":
        return None
    if resolve_live_diarizer(settings) == "streaming":
        return None  # streamed sources have their speakers already
    if settings.live_rediarize == "auto" and resolve_diarizer(settings) != "nemotron":
        return None
    try:
        import nemo  # noqa: F401
    except ImportError:
        return None
    from .diarizers.nemotron import NemotronDiarizer

    return NemotronDiarizer(embedder=build_live_embedder(settings))


def build_diarizer(settings: Settings) -> Diarizer | None:
    kind = resolve_diarizer(settings)
    if kind == "none":
        return None
    if kind == "demo":
        from ..demo import DemoDiarizer

        return DemoDiarizer()
    if kind == "pyannote":
        from .diarizers.pyannote import PyannoteDiarizer

        return PyannoteDiarizer(model=settings.diarization_model, hf_token=settings.hf_token)
    if kind == "nemotron":
        from .diarizers.nemotron import NemotronDiarizer

        # Nemotron has no speaker embeddings; voice profiles use pyannote's embedder.
        return NemotronDiarizer(embedder=build_live_embedder(settings))
    if kind == "onnx":
        from .diarizers.onnx import OnnxDiarizer

        return OnnxDiarizer(models_dir=settings.models_dir / "diarization")
    raise ValueError(f"Unknown diarizer '{kind}'. Choose one of {DIARIZERS}")


def build_engine(settings: Settings) -> ComposedEngine:
    return ComposedEngine(
        transcriber=build_transcriber(settings),
        diarizer=build_diarizer(settings),
        language=settings.language or None,
        min_speakers=settings.min_speakers,
        max_speakers=settings.max_speakers,
        echo_dedup=settings.echo_dedup,
        echo_similarity=settings.echo_similarity,
    )


ENGINE_SETTINGS = (
    "transcriber",
    "diarizer",
    "whisper_model_size",
    "whisper_compute_type",
    "whisper_batch_size",
    "whisper_vad",
    "parakeet_model",
    "parakeet_quantization",
    "onnx_provider",
    "remote_stt_url",
    "remote_stt_model",
    "remote_stt_api_key",
    "phonon_command",
    "diarization_model",
    "hf_token",
    "language",
    "min_speakers",
    "max_speakers",
    "echo_dedup",
    "echo_similarity",
    "glossary",
)

LIVE_SETTINGS = (
    "live_transcriber",
    "live_rediarize",
    "live_diarizer",
    "live_diarization",
    "diarizer",
    "diarization_model",
    "hf_token",
    "parakeet_model",
    "parakeet_quantization",
    "onnx_provider",
    "whisper_model_size",
    "whisper_compute_type",
    "whisper_batch_size",
    "whisper_vad",
    "remote_stt_url",
    "remote_stt_model",
    "remote_stt_api_key",
    "phonon_command",
)
