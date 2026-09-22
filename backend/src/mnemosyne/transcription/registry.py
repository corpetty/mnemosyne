"""Build a TranscriptionEngine from settings.

Heavy imports happen inside the builders so choosing `remote` + `none` never
imports torch.
"""

from __future__ import annotations

from ..config import Settings
from .composed import ComposedEngine
from .engine import Diarizer, Transcriber

TRANSCRIBERS = ("whisperx", "parakeet", "remote")
DIARIZERS = ("pyannote", "none")


def build_transcriber(settings: Settings, kind: str | None = None) -> Transcriber:
    kind = kind or settings.transcriber
    if kind == "whisperx":
        from .transcribers.whisperx import WhisperXTranscriber

        return WhisperXTranscriber(
            model_size=settings.whisper_model_size,
            compute_type=settings.whisper_compute_type,
            batch_size=settings.whisper_batch_size,
            vad_method=settings.whisper_vad,
        )
    if kind == "parakeet":
        from .transcribers.parakeet import ParakeetTranscriber

        return ParakeetTranscriber(
            model=settings.parakeet_model,
            provider=settings.onnx_provider,
            quantization=settings.parakeet_quantization or None,
        )
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
    return build_transcriber(settings, settings.live_transcriber)


def build_diarizer(settings: Settings) -> Diarizer | None:
    kind = settings.diarizer
    if kind == "none":
        return None
    if kind == "pyannote":
        from .diarizers.pyannote import PyannoteDiarizer

        return PyannoteDiarizer(model=settings.diarization_model, hf_token=settings.hf_token)
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
    "diarization_model",
    "hf_token",
    "language",
    "min_speakers",
    "max_speakers",
    "echo_dedup",
    "echo_similarity",
)

LIVE_SETTINGS = (
    "live_transcriber",
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
)
