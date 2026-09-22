"""Application settings.

Precedence, highest first:
1. Environment variables (including backend/.env, loaded below).
2. The user config file (TOML) at MNEMOSYNE_CONFIG_FILE or
   $XDG_CONFIG_HOME/mnemosyne/config.toml.
3. Defaults.

The Settings UI writes the config file. Anything also set in the environment
keeps winning until it is removed from the environment; `env_overrides()`
tells the UI which fields are in that state.
"""

from __future__ import annotations

import logging
import os
import sys
from pathlib import Path

import tomli_w
from dotenv import load_dotenv
from pydantic import AliasChoices, Field
from pydantic_settings import (
    BaseSettings,
    PydanticBaseSettingsSource,
    SettingsConfigDict,
    TomlConfigSettingsSource,
)

logger = logging.getLogger(__name__)


def backend_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parents[2]


# Load backend/.env so its variables are visible to the env settings source.
load_dotenv(backend_dir() / ".env")


def default_data_dir() -> Path:
    if getattr(sys, "frozen", False):
        # resources/backend/mnemosyne-backend -> resources/data
        return Path(sys.executable).resolve().parent.parent / "data"
    return backend_dir().parent / "data"


def config_file_path() -> Path:
    override = os.environ.get("MNEMOSYNE_CONFIG_FILE")
    if override:
        return Path(override).expanduser()
    xdg = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(xdg) / "mnemosyne" / "config.toml"


SECRET_FIELDS = frozenset({"hf_token", "openai_api_key", "anthropic_api_key", "remote_stt_api_key"})


class Settings(BaseSettings):
    model_config = SettingsConfigDict(extra="ignore", case_sensitive=False)

    # Paths
    data_dir: Path = Field(
        default_factory=default_data_dir,
        validation_alias=AliasChoices("MNEMOSYNE_DATA_DIR", "data_dir"),
    )

    # Transcription pipeline
    transcriber: str = "whisperx"  # whisperx | parakeet | remote
    diarizer: str = "pyannote"  # pyannote | none
    language: str = ""  # blank = auto-detect
    min_speakers: int | None = None
    max_speakers: int | None = 10
    auto_transcribe: bool = True
    # When mic and system audio are captured separately, the mic file is
    # labelled with this name instead of being diarized.
    local_speaker_name: str = "Me"
    remote_speaker_name: str = "Remote"  # live label for the system channel
    per_source_transcription: bool = True

    # Live transcription while recording (provisional; replaced by the final job)
    live_transcription: bool = True
    live_transcriber: str = "parakeet"  # parakeet | whisperx | remote
    live_interval_seconds: float = 5.0

    # whisperx transcriber
    hf_token: str = ""
    whisper_model_size: str = "medium.en"
    whisper_compute_type: str = "float16"
    whisper_batch_size: int = 8
    whisper_vad: str = "silero"  # silero | pyannote

    # parakeet transcriber (ONNX Runtime, no torch)
    parakeet_model: str = "nemo-parakeet-tdt-0.6b-v3"
    # "int8" (default: 4x smaller, single file) or "" (fp32, external-data file)
    parakeet_quantization: str = "int8"
    onnx_provider: str = "cpu"  # cpu | cuda

    # remote transcriber: any OpenAI-compatible /audio/transcriptions server
    remote_stt_url: str = ""  # e.g. http://127.0.0.1:8484/v1
    remote_stt_model: str = "whisper-1"
    remote_stt_api_key: str = ""

    # pyannote diarizer
    diarization_model: str = "pyannote/speaker-diarization-community-1"

    # LLM providers
    ollama_url: str = "http://localhost:11434"
    vllm_url: str = "http://localhost:8000"
    openai_api_key: str = ""
    anthropic_api_key: str = ""
    default_provider: str = "ollama"
    default_model: str = ""

    # Export
    obsidian_vault_path: str = ""
    obsidian_subfolder: str = "meetings/mnemosyne"

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        toml = TomlConfigSettingsSource(settings_cls, toml_file=config_file_path())
        return (init_settings, env_settings, toml)

    # ---- helpers -------------------------------------------------------

    @property
    def sessions_dir(self) -> Path:
        return self.data_dir / "sessions"

    @property
    def recordings_dir(self) -> Path:
        return self.data_dir / "recordings"

    @property
    def db_path(self) -> Path:
        return self.data_dir / "mnemosyne.db"

    def env_overrides(self) -> list[str]:
        """Field names whose value is being forced by the environment."""
        names = []
        for name in type(self).model_fields:
            if name == "data_dir":
                if os.environ.get("MNEMOSYNE_DATA_DIR"):
                    names.append(name)
                continue
            if os.environ.get(name.upper()):
                names.append(name)
        return names

    def secrets_set(self) -> dict[str, bool]:
        return {name: bool(getattr(self, name)) for name in SECRET_FIELDS}

    def public_dict(self) -> dict:
        """Serializable view with secrets blanked."""
        data = self.model_dump(mode="json")
        for name in SECRET_FIELDS:
            data[name] = ""
        return data


def load_settings() -> Settings:
    return Settings()


def save_settings(settings: Settings, path: Path | None = None) -> Path:
    """Persist all settings to the TOML config file (mode 0600)."""
    path = path or config_file_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    data = settings.model_dump(mode="json")
    data["data_dir"] = str(settings.data_dir)
    # TOML has no null; omitted keys fall back to defaults on load.
    data = {k: v for k, v in data.items() if v is not None}
    tmp = path.with_suffix(".toml.tmp")
    tmp.write_text(tomli_w.dumps(data))
    os.chmod(tmp, 0o600)
    tmp.replace(path)
    logger.info("Saved settings to %s", path)
    return path
